"""Generate auditable entry scenarios before spending model tokens. Never send orders."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json

from config import CODE_ROOT
from pretrade_policy import buy_block, MIN_NET_RETURN, MIN_REWARD_RISK, MAX_POSITION_RISK
from trade_economics import evaluate_buy,estimate_sell
from entry_structure import completed_recovery, resistance_levels, target_evidence, stop_evidence, VERSION as STRUCTURE_VERSION


def policy():
    p = json.loads((CODE_ROOT/'config/opportunity_search.json').read_text('utf-8'))
    fractions = p['allocation_capital_fractions']
    if (type(p['enabled']) is not bool or set(fractions) != {'Early','Confirmed','Strong'}
            or not 0 < fractions['Early'] <= fractions['Confirmed'] <= fractions['Strong'] <= .50
            or not 1 <= p['max_research_candidates'] <= 5
            or not .5 <= p['minimum_stop_atr'] <= 2
            or not 0 < p['maximum_pullback_fraction'] <= .05
            or not 0 < p['maximum_pullback_atr'] <= 2
            or not 1 <= p['breakout_volume_ratio'] <= 3
            or not 1.5 <= p['breakout_target_r_multiple'] <= 3
            or not 2 <= p['maximum_modeled_target_atr'] <= 6):
        raise ValueError('INVALID_OPPORTUNITY_POLICY')
    return p


def _number(value):
    d = Decimal(str(value))
    if not d.is_finite() or d <= 0:
        raise ValueError('INVALID_OPPORTUNITY_PRICE')
    return d


def _round_price(value, asset, upward=False):
    from execution.planned_entry import validate_price
    # Round to a legal tick without assuming the tick on the other side of a
    # band boundary. Generated targets are scenarios, never observed prices.
    value = Decimal(str(value))
    unit = Decimal('.01')
    value = value.quantize(unit, rounding='ROUND_CEILING' if upward else ROUND_FLOOR)
    for _ in range(1000):
        try:
            validate_price(value, asset)
            return value
        except ValueError:
            value += unit if upward else -unit
    raise ValueError('CANNOT_ROUND_OPPORTUNITY_PRICE')


def allocation_limit(candidate, account, p, tier='Strong'):
    """Per-symbol exposure uses fixed strategy capital, bounded by actual cash."""
    capital = _number(account['capital_limit_twd'])
    cash = max(Decimal(0), Decimal(account['available_cash_cents'])/100)
    held = int((candidate.get('paper_position') or {}).get('qty') or 0)
    basis = candidate.get('ai_cost_basis') or {}
    reference = max(_number(candidate['ask']), _number(candidate['last_price']))
    if basis.get('available'):
        reference = max(reference, _number(basis['average_unit_cost_twd']))
    remaining = capital * Decimal(str(p['allocation_capital_fractions'][tier])) - reference * held
    return max(Decimal(0), min(cash, remaining))


def existing_position_risk(candidate, stop, rules):
    held=int((candidate.get('paper_position') or {}).get('qty') or 0)
    if held<=0:return Decimal(0)
    stops=[Decimal(str(stop))]
    for lot in (candidate.get('buy_plans') or {}).get('lots',[]):
        old=(lot.get('plan') or {}).get('invalidation_price')
        if old is not None:stops.append(_number(old))
    spread=_number(candidate['ask'])-_number(candidate['bid'])
    exit_price=min(stops)-spread
    economics=estimate_sell({**candidate,'bid':float(exit_price)},held,
                            minimum_commission_twd=rules.economic_minimum_commission_twd)
    pnl=economics['estimated_realized_pnl_twd']
    if pnl is None:raise ValueError('EXISTING_POSITION_RISK_BASIS_UNKNOWN')
    # Reconstructed broker basis excludes buy fees; include a conservative
    # estimate so repeated scans cannot reset the same symbol's risk budget.
    buy_fee=Decimal(0)
    if (candidate.get('ai_cost_basis') or {}).get('available'):
        cost=_number(candidate['ai_cost_basis']['average_unit_cost_twd'])*held
        buy_fee=max(rules.economic_minimum_commission_twd,
                    (cost*Decimal('.001425')).to_integral_value(rounding=ROUND_FLOOR))
    return max(Decimal(0),-Decimal(str(pnl))+buy_fee)


def refresh(candidate, account, rules, p=None, *, research_proxy=False):
    """Refresh scenarios from original full history and current account/quote.

    Swing levels use a completed bar and its two completed neighbours; no
    future bar or intraday volume is substituted for completed daily volume.
    """
    p = deepcopy(p or candidate.get('opportunity_policy') or policy())
    candidate['opportunity_policy'] = p
    candidate['trade_setups'] = []
    candidate['research_only'] = False
    candidate.pop('entry_structure',None)
    candidate.pop('recovery_watch', None)
    candidate.pop('entry_watch',None)
    candidate['pending_entry_setups'] = []
    candidate.pop('target_reachability',None)
    candidate['opportunity_status'] = dict(blockers=[], signal=None)
    status = candidate['opportunity_status']
    try:
        if candidate.get('pending_execution'):
            raise ValueError('EXISTING_PENDING_ORDER_REQUIRES_RECONCILIATION')
        if candidate.get('opportunity_buy_eligible') is False:
            raise ValueError('POSITION_OUTSIDE_BUY_RESEARCH_UNIVERSE')
        proxy = (research_proxy and candidate.get('quote_status') == 'HISTORICAL_DAILY_PROXY'
                 and candidate.get('execution_permitted') is False)
        if (candidate.get('quote_status') != 'LIVE' and not proxy) or candidate.get('is_trial'):
            raise ValueError('LIVE_NON_TRIAL_QUOTE_REQUIRED')
        if candidate.get('asset_type') not in ('STOCK','ETF'):
            raise ValueError('ASSET_TYPE_UNKNOWN')
        if account.get('budget_status', 'READY') != 'READY':
            raise ValueError('BUY_BUDGET_NOT_READY')
        block = buy_block(candidate)
        if block:
            raise ValueError(block)
        stamp = datetime.fromisoformat(candidate['quote_timestamp'])
        if stamp.tzinfo is None:raise ValueError('QUOTE_TIMEZONE_REQUIRED')
        session = stamp.date().isoformat()
        bars = sorted((b for b in candidate.get('daily_bars', []) if b.get('complete') is True
            and b.get('source') in ('TWSE_DAILY','BROKER_DAILY') and b['date'] < session), key=lambda b:b['date'])
        if len(bars) < 21 or (candidate.get('daily_history_status') or {}).get('missing_20d_trading_dates'):
            raise ValueError('COMPLETE_DAILY_STRUCTURE_REQUIRED')
        # Verify actual dates too: legacy snapshots may lack missing-date metadata.
        from market_calendar import previous_trading_day
        history_status=candidate.get('daily_history_status') or {}
        verified_dates=history_status.get('verified_trading_dates')
        if ((history_status.get('market_calendar') or {}).get('verified') is True
                and verified_dates and all(d<session for d in verified_dates)):
            required=verified_dates[-21:]
        else:
            cursor = datetime.fromisoformat(session).date()
            required=[]
            for _ in range(21):
                cursor=previous_trading_day(cursor); required.append(cursor.isoformat())
        if not set(required).issubset({b['date'] for b in bars}):
            raise ValueError('COMPLETE_DAILY_STRUCTURE_REQUIRED')
        full_history=bars
        bars=bars[-22:]
        if len({b['date'] for b in bars}) != len(bars):
            raise ValueError('DUPLICATE_DAILY_STRUCTURE')
        for b in bars:
            if not _number(b['low']) <= _number(b['close']) <= _number(b['high']):
                raise ValueError('INVALID_DAILY_STRUCTURE')
        ask,bid,last=(_number(candidate[k]) for k in ('ask','bid','last_price'))
        if bid>ask:
            raise ValueError('INVALID_BOOK')
        closes=[_number(b['close']) for b in bars]
        ma5=sum(closes[-5:])/5; ma20=sum(closes[-20:])/20
        tr=[max(_number(bars[i]['high'])-_number(bars[i]['low']),
                abs(_number(bars[i]['high'])-closes[i-1]),
                abs(_number(bars[i]['low'])-closes[i-1])) for i in range(1,len(bars))]
        atr=sum(tr[-14:])/len(tr[-14:])
        if atr<=0:
            raise ValueError('ATR_UNAVAILABLE')
        vol_ratio=Decimal(str(bars[-1]['volume']))/(sum(Decimal(str(b['volume'])) for b in bars[-6:-1])/5)
        trend=ma5>ma20 and closes[-1]>=ma5 and closes[-1]>=closes[-6]
        daily_breakout=(closes[-1]>max(_number(b['high']) for b in bars[-21:-1])
                        and vol_ratio>=Decimal(str(p['breakout_volume_ratio'])))
        breakout_level=max(_number(b['high']) for b in bars[-20:])
        # Today's live price can break out before a completed breakout bar
        # exists. It is an Early trend scenario, never daily volume confirmation.
        first_day_breakout=(trend and not daily_breakout and last>breakout_level
                            and last>_number(candidate['open']) and last>=ma5)
        recovery=(last>_number(candidate['open']) and
                  (last-_number(candidate['low']))/max(Decimal('.01'),_number(candidate['high'])-_number(candidate['low']))>=Decimal('.6'))
        recovery_plan = completed_recovery(bars[-2], bars[-1], closes[-2] - closes[-3])
        retest = False
        if recovery_plan and 'intraday_recovery' in candidate.get('eligible_strategies', []):
            reclaimed = _number(recovery_plan['reclaimed_high'])
            low = _number(candidate['low'])
            retest = not trend and reclaimed <= low <= reclaimed + (ask-bid) and last > low and bid >= reclaimed
            candidate['recovery_watch'] = dict(
                recovery_plan, trigger_kind='HOLD_RECLAIMED_LEVEL', condition_price=float(reclaimed),
                expires_session=session,
                status=('INVALIDATED_THIS_SESSION' if low < reclaimed else
                        'RESEARCH_CONDITION_MET' if retest else 'WAITING_FOR_CONDITION'))
        if not trend and not recovery and not retest:
            raise ValueError('NO_TREND_OR_RECOVERY_SETUP')
        tier='Strong' if trend and last>=ma5 and vol_ratio>=Decimal(str(p['breakout_volume_ratio'])) else ('Confirmed' if trend else 'Early')
        if first_day_breakout:tier='Early'
        strategy='multi_day_trend' if trend else 'intraday_recovery'
        if strategy not in candidate.get('eligible_strategies',[]):
            raise ValueError('SETUP_STRATEGY_NOT_ELIGIBLE')
        family = ('COMPLETED_REBOUND_RETEST' if retest else 'FIRST_DAY_BREAKOUT' if first_day_breakout else
                  'TREND_PULLBACK' if trend else 'MULTI_SESSION_RECOVERY')
        from target_reachability import profile as reachability_profile
        reachability=reachability_profile(full_history,as_of=session,holding_days=10 if trend else 2,trend=trend,
            session_dates=(candidate.get('daily_history_status') or {}).get('verified_trading_dates'),
            completed_breakout=bool(daily_breakout),breakout_volume_ratio=p['breakout_volume_ratio'])
        candidate['target_reachability']=reachability
        supports=[]; extra=[]
        for i in range(1,len(bars)-1):
            b=bars[i]
            if b['low']<=bars[i-1]['low'] and b['low']<bars[i+1]['low']:
                supports.append(_number(b['low']))
                extra.append(dict(id='swing_low_'+b['date'],price=b['low'],basis=b['date']))
            if b['high']>=bars[i-1]['high'] and b['high']>bars[i+1]['high']:
                extra.append(dict(id='swing_high_'+b['date'],price=b['high'],basis=b['date']))
        levels=[r for r in candidate.get('price_levels',[]) if not r['id'].startswith('swing_') and r['id']!='last_completed_low']
        for row in levels:
            if row['id'].startswith('low_') or row['id']=='today_low':supports.append(_number(row['price']))
        candidate['price_levels']=levels+extra
        barriers=resistance_levels(bars,candidate['price_levels'],recovery=not trend)
        if retest and _number(candidate['high']) > last:
            barriers.append(dict(price=candidate['high'], kind='OBSERVED_INTRADAY_HIGH_FALLBACK',
                                 source='LIVE_INTRADAY_HIGH', date=session))
            barriers.sort(key=lambda row: row['price'])
        candidate['entry_structure']=dict(version=STRUCTURE_VERSION,
            setup_family=family,
            resistance_levels=barriers,
            confirmation_scope='COMPLETED_HISTORY_AND_CURRENT_LIVE_QUOTE',
            requires_today_completed_bar=False)
        # The completed breakout/recovery bar's actual low is a concrete
        # invalidation reference; it is not moved to improve the ratio.
        supports.append(_number(bars[-1]['low']))
        candidate['price_levels'].append(dict(id='last_completed_low',price=bars[-1]['low'],basis=bars[-1]['date']))
        entries={ask:'CURRENT_ASK'}
        if candidate.get('pricing_policy')=='PLANNED_LIMIT':
            for support in supports:
                if support<ask:entries[support]='OBSERVED_SUPPORT_PULLBACK'
            # A moving average is an explicitly derived entry reference. Stops
            # remain observed supports and every order still needs fresh review.
            if trend and ma5<ask:entries[_round_price(ma5,candidate['asset_type'])]='MA5_PULLBACK_REFERENCE'
            for r in levels:
                if r['id'] in ('previous_close','today_open') and _number(r['price'])<ask:
                    entries[_number(r['price'])]=r['id']
            if retest and reclaimed < ask:
                entries[reclaimed] = 'COMPLETED_REBOUND_RETEST_LIMIT'
        allocation=allocation_limit(candidate,account,p,tier)
        risk=_number(account['capital_limit_twd'])*MAX_POSITION_RISK
        if allocation<=rules.min_buy_fee_reserve_twd:
            raise ValueError('PER_SYMBOL_ALLOCATION_EXHAUSTED')
        candidates=[];rejections=set();near_misses=[]
        for entry,entry_basis in entries.items():
            if retest and entry < reclaimed:
                rejections.add('ENTRY_BELOW_RECLAIMED_LEVEL');continue
            if first_day_breakout and entry<breakout_level:
                rejections.add('ENTRY_BELOW_BREAKOUT_REFERENCE');continue
            if ask-entry>min(atr*Decimal(str(p['maximum_pullback_atr'])),ask*Decimal(str(p['maximum_pullback_fraction']))):
                rejections.add('ENTRY_OUTSIDE_REACH')
                continue
            _round_price(entry,candidate['asset_type'])  # Reject non-legal entries below via evaluate_buy.
            stops=[s for s in supports if 0<s<entry and s<last and entry-s>=max(
                atr*Decimal(str(p['minimum_stop_atr'])),(ask-bid)*2)
                and not stop_evidence(bars,s,candidate['low'],session).get('broken_on')]
            if not stops:
                rejections.add('NO_OBSERVED_STOP_WITH_MINIMUM_ATR_DISTANCE');continue
            stop=max(stops)
            existing_risk=existing_position_risk(candidate,stop,rules)
            barrier=target_evidence(barriers,entry)
            kind='OBSERVED_SWING_RESISTANCE'
            if barrier:
                target=_number(barrier['price']);kind=barrier['kind']
            elif not retest and (daily_breakout or first_day_breakout):
                if reachability['status'] != 'AVAILABLE':
                    rejections.add('TARGET_REACHABILITY_HISTORY_REQUIRED');continue
                if reachability['empirical_target_enabled']:
                    if reachability['price_ceiling']<=float(entry):
                        rejections.add('NO_REMAINING_UPSIDE_WITHIN_HORIZON');continue
                    target=_round_price(min(_number(reachability['price_ceiling']),
                        closes[-1]+atr*Decimal(str(p['maximum_modeled_target_atr']))),candidate['asset_type'])
                    kind='EMPIRICAL_HORIZON_RANGE_NOT_OBSERVED_PRICE'
                    if target-entry>atr*Decimal(str(p['maximum_modeled_target_atr'])):
                        rejections.add('MODELED_TARGET_DISTANCE_EXCEEDS_ATR_LIMIT');continue
                else:
                    # Retain the original payoff requirement as an explicitly
                    # unproven research scenario. It is not a price forecast.
                    # The median-cap experiment failed untouched cohorts and
                    # must not silently veto all potential breakout targets.
                    target=_round_price(entry+min((entry-stop)*Decimal(str(p['breakout_target_r_multiple'])),
                        atr*Decimal(str(p['maximum_modeled_target_atr']))),candidate['asset_type'])
                    kind='REQUIRED_PAYOFF_NOT_OBSERVED_PRICE'
            elif not trend and _number(candidate['high'])>entry:
                target=_number(candidate['high'])
                kind='OBSERVED_INTRADAY_HIGH_FALLBACK'
            else:
                rejections.add('NO_OBSERVED_TARGET_OR_CONFIRMED_BREAKOUT');continue
            if retest and last >= target:
                rejections.add('RECOVERY_TARGET_ALREADY_REACHED')
                continue
            # An empirical point estimate constrains an estimated target.
            # A real observed barrier retains its distinct evidence; the past
            # median is not a physical ceiling that deletes every higher swing.
            if reachability['status']=='AVAILABLE' and kind=='EMPIRICAL_HORIZON_RANGE_NOT_OBSERVED_PRICE':
                ceiling=Decimal(str(reachability['price_ceiling']))
                if ceiling<=entry:
                    rejections.add('NO_REMAINING_UPSIDE_WITHIN_HORIZON');continue
                bounded=_round_price(ceiling,candidate['asset_type'])
                if bounded<target:
                    target=bounded;kind='EMPIRICAL_HORIZON_RANGE_NOT_OBSERVED_PRICE'
            if target<=entry:
                rejections.add('NO_REMAINING_UPSIDE_WITHIN_HORIZON');continue
            qty_max=min(999,int(rules.max_buy_qty or 999),int(allocation/entry))
            if qty_max<1:rejections.add('ONE_SHARE_EXCEEDS_ALLOCATION')
            for qty in range(qty_max,0,-1):
                gross=entry*qty
                reserve=max(rules.min_buy_fee_reserve_twd,gross*rules.buy_cash_buffer_rate,
                            (gross*Decimal('.001425')).to_integral_value(rounding=ROUND_FLOOR))
                if gross+reserve>allocation or (rules.max_order_twd is not None and gross>rules.max_order_twd):
                    if qty==1:rejections.add('ONE_SHARE_FUNDING_EXCEEDS_ALLOCATION')
                    continue
                economics=evaluate_buy(candidate,qty=qty,limit_price=float(entry),target_price=float(target),
                    invalidation_price=float(stop),minimum_commission_twd=rules.economic_minimum_commission_twd)
                combined=Decimal(str(economics['estimated_net_downside_twd']))+existing_risk
                if combined>risk:
                    if qty==1:rejections.add('ONE_SHARE_STOP_RISK_EXCEEDS_BUDGET')
                    continue
                net=Decimal(str(economics['estimated_net_upside_twd']))/Decimal(str(economics['estimated_buy_cost_twd']))
                rejection=None
                if net<MIN_NET_RETURN:rejection='NET_RETURN_BELOW_MINIMUM'
                elif (economics['reward_risk_ratio'] or 0)<MIN_REWARD_RISK:rejection='REWARD_RISK_BELOW_MINIMUM'
                hurdle=max(p['minimum_target_net_profit_twd'],
                           p['analysis_cost_budget_twd']*p['minimum_target_profit_to_analysis_cost'])
                if rejection is None and economics['estimated_net_upside_twd']<hurdle:
                    rejection='TARGET_NET_PROFIT_BELOW_RESEARCH_HURDLE'
                if rejection:
                    rejections.add(rejection)
                    near_misses.append(dict(entry=float(entry),target=float(target),invalidation=float(stop),qty=qty,
                        net_upside_twd=economics['estimated_net_upside_twd'],combined_stop_risk_twd=float(combined),
                        net_return=float(net),reward_risk_ratio=economics['reward_risk_ratio'],rejection=rejection,
                        purpose='DIAGNOSTIC_ONLY_NOT_ORDER_APPROVAL'))
                    break
                setup=dict(strategy_id=strategy,tier=tier,entry=float(entry),entry_basis=entry_basis,
                    target=float(target),target_kind=kind,invalidation=float(stop),holding_days=10 if trend else 2,
                    max_qty=qty,allocation_limit_twd=float(allocation),funding_required_twd=float(gross+reserve),
                    net_return=float(net),atr14=float(atr),completed_volume_ratio=float(vol_ratio),
                    daily_breakout_confirmed=bool(daily_breakout),**economics)
                setup['setup_family'] = family
                if retest:
                    setup['recovery_retest'] = dict(recovery_plan, observed_low=float(low), session=session)
                setup['breakout_confirmation']=('COMPLETED_DAILY_PRICE_AND_VOLUME' if daily_breakout else
                    'LIVE_PRICE_ONLY_NO_COMPLETED_VOLUME_CONFIRMATION' if first_day_breakout else None)
                setup['breakout_reference_price']=float(breakout_level) if first_day_breakout else None
                from target_reachability import view as reachability_view
                setup['target_evidence']=(deepcopy(barrier) if barrier and kind==barrier['kind'] else dict(
                    kind=kind,source='PAST_HORIZON_RANGE' if 'EMPIRICAL' in kind else 'LIVE_INTRADAY_HIGH',
                    date=session,nearest_observed_resistance=deepcopy(barrier)))
                setup['target_evidence']['reachability']=reachability_view(reachability)
                from target_reachability import assess_target
                setup['target_evidence']['payoff_assessment']=assess_target(reachability,
                    entry=float(entry),target=float(target),invalidation=float(stop))
                if kind=='REQUIRED_PAYOFF_NOT_OBSERVED_PRICE':
                    setup['target_evidence']['source']='REQUIRED_R_PAYOFF_NOT_PRICE_FORECAST'
                setup['existing_modeled_risk_twd']=float(existing_risk)
                setup['combined_modeled_risk_twd']=float(existing_risk+Decimal(str(economics['estimated_net_downside_twd'])))
                setup['analysis_cost_budget_twd']=p['analysis_cost_budget_twd']
                setup['target_profit_to_analysis_budget']=economics['estimated_net_upside_twd']/p['analysis_cost_budget_twd']
                setup['setup_id']=hashlib.sha256(json.dumps({k:setup[k] for k in
                    ('strategy_id','tier','entry','target','invalidation','max_qty')},sort_keys=True).encode()).hexdigest()[:12]
                setup['priority_score']=round(min(economics['reward_risk_ratio'],5)*10+float(net)*100+
                    {'Early':0,'Confirmed':10,'Strong':15}[tier]-float((ask-entry)/atr)*5,4)
                candidates.append(setup);break
        # A veto of the top routes must not erase a lower-ranked valid route.
        # Bound the final payload only after checking the feasible alternatives.
        plans=sorted(candidates,key=lambda s:-s['priority_score'])
        rejected=[]
        from entry_decision import policy as entry_policy,review as review_entry
        decision_policy=entry_policy()
        candidate['entry_decision_policy']=decision_policy
        status['entry_decision_policy']=decision_policy
        for setup in plans:
            # A historical floor below which the last completed session closed
            # is not intact support merely because price recovered intraday.
            # Today's observed low is separate, newly formed evidence.
            stop=Decimal(str(setup['invalidation']))
            fresh_low=stop==_number(candidate['low'])
            if closes[-1]<stop and not fresh_low:
                rejected.append(dict(setup_id=setup['setup_id'],
                    reason='HISTORICAL_STOP_BROKEN_ON_LAST_COMPLETED_CLOSE',
                    invalidation=setup['invalidation'],last_completed_close=float(closes[-1]),
                    last_completed_date=bars[-1]['date']))
                continue
            if decision_policy['enabled']:
                evidence=review_entry(candidate,setup,bars,decision_policy)
                setup['decision_evidence']=evidence
                if not evidence['approved']:
                    rejected.append(dict(setup_id=setup['setup_id'],reason='ENTRY_DECISION_EVIDENCE_NOT_CONFIRMED',
                        blockers=evidence['blockers'],evidence=evidence))
                    candidate['pending_entry_setups'].append(deepcopy(setup))
                    continue
            setup['entry_evidence']=dict(last_completed_date=bars[-1]['date'],
                last_completed_close=float(closes[-1]),today_open=float(candidate['open']),
                today_low=float(candidate['low']),stop_basis='LIVE_TODAY_LOW' if fresh_low else 'COMPLETED_SUPPORT',
                distance_from_open_atr=round(float((Decimal(str(setup['entry']))-_number(candidate['open']))/atr),4),
                distance_from_previous_close_atr=round(float((Decimal(str(setup['entry']))-closes[-1])/atr),4),
                target_probability=None,
                stop_origin=stop_evidence(bars,stop,candidate['low'],session),
                confirmation_scope='CURRENT_SETUP_NOT_EXPIRED_HISTORICAL_SUPPORT',
                requires_today_completed_bar=False)
        candidate['trade_setups']=[s for s in plans if s['setup_id'] not in {r['setup_id'] for r in rejected}][:3]
        candidate['pending_entry_setups']=candidate['pending_entry_setups'][:3]
        status['rejected_setups']=rejected
        status.update(signal=tier,atr14=float(atr),allocation_basis='STRATEGY_CAPITAL_LESS_EXISTING_SYMBOL_EXPOSURE',
                      allocation_limit_twd=float(allocation),completed_volume_ratio=float(vol_ratio))
        status['rejection_reasons']=sorted(rejections)
        status['near_misses']=sorted(near_misses,key=lambda r:-r['net_upside_twd'])[:3]
        if not candidate['trade_setups']:
            status['blockers']=sorted({r['reason'] for r in rejected}) if rejected else ['NO_COST_AND_RISK_QUALIFIED_ENTRY_IN_REACH']
    except (KeyError,ValueError,TypeError,ArithmeticError) as exc:
        status['blockers']=[str(exc)]
    if candidate['pending_entry_setups'] and not candidate['trade_setups']:
        reference=candidate['pending_entry_setups'][0].get('breakout_reference_price') or candidate['last_price']
        candidate['entry_watch']=dict(symbol=candidate['symbol'],trigger_kind='ENTRY_CONFIRMATION_PENDING',
            condition_price=reference,expires_session=candidate['quote_timestamp'][:10],
            status='RESEARCH_CONDITION_MET',purpose='RESEARCH_ONLY_NOT_ORDER_APPROVAL')
        candidate['research_only']=True
    if candidate.get('market_scout'):
        from opportunity_watch import observe
        candidate['market_scout']=observe(candidate['market_scout'],candidate)
    watch = active_research_watch(candidate)
    candidate['research_only'] = candidate['research_only'] or bool(watch and not candidate['trade_setups'] and status['blockers']
        and set(status['blockers']).issubset({'NO_TREND_OR_RECOVERY_SETUP',
                                             'NO_COST_AND_RISK_QUALIFIED_ENTRY_IN_REACH'}))
    return candidate


def active_research_watch(candidate):
    for key in ('entry_watch', 'recovery_watch', 'market_scout'):
        watch = candidate.get(key) or {}
        if watch.get('status') == 'RESEARCH_CONDITION_MET':
            return watch
    return None


def match_setup(candidate, decision):
    if any(decision.get(k) is None for k in ('suggested_price','target_price','invalidation_price')):
        return None
    for setup in candidate.get('trade_setups',[]):
        if (decision.get('strategy_id')==setup['strategy_id'] and decision.get('holding_days')==setup['holding_days']
                and all(Decimal(str(decision.get(dk)))==Decimal(str(setup[sk])) for dk,sk in
                        (('suggested_price','entry'),('target_price','target'),('invalidation_price','invalidation')))
                and type(decision.get('suggested_qty')) is int and 0<decision['suggested_qty']<=setup['max_qty']):
            return setup
    return None


def select(candidates, account, rules, p, *, research_proxy=False):
    for c in candidates:refresh(c,account,rules,p,research_proxy=research_proxy)
    opportunities=sorted((c for c in candidates if c['trade_setups']),
        key=lambda c:(-c['trade_setups'][0]['priority_score'],c.get('quant_rank') or 999))
    selected=opportunities[:p['max_research_candidates']]
    # A live watch touch can request research before an executable plan exists.
    # Missing data, pending orders and account/ownership blocks remain excluded.
    watch_candidates=[c for c in candidates if c['research_only']]
    watch_candidates.sort(key=lambda c:(c.get('quant_rank') or 999,c['symbol']))
    # Triggered research has its own lane. It cannot authorize an order, and
    # must not wait hours behind numerically attractive plans later vetoed.
    selected += watch_candidates[:p['max_research_candidates']]
    selected_symbols={c['symbol'] for c in selected}
    selected += [c for c in candidates if int((c.get('paper_position') or {}).get('qty') or 0)>0
                 and c['symbol'] not in selected_symbols]
    for c in selected:
        held=int((c.get('paper_position') or {}).get('qty') or 0)>0
        c['analysis_source']='OPPORTUNITY+POSITION' if c['trade_setups'] and held else (
            'OPPORTUNITY_SEARCH' if c['trade_setups'] else
            'WATCH_TRIGGER_RESEARCH' if c['research_only'] else 'POSITION_RISK_REVIEW')
    return selected,dict(version=p['version'],pool_count=len(candidates),
        qualified_symbols=[c['symbol'] for c in opportunities],research_symbols=[c['symbol'] for c in selected],
        watch_research_symbols=[c['symbol'] for c in selected if c['research_only']],
        rejection_reasons={c['symbol']:c['opportunity_status'].get('rejection_reasons',[]) for c in candidates if not c['trade_setups']},
        near_misses={c['symbol']:c['opportunity_status'].get('near_misses',[]) for c in candidates if not c['trade_setups']},
        blockers={c['symbol']:c['opportunity_status']['blockers'] for c in candidates if not c['trade_setups']})
