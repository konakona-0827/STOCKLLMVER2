"""Isolated, forward-only parallel paper trading. No broker account/order calls."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sqlite3
import uuid

from config import ROOT, load_env, now, redact
from main import Audit, save_json
from capital_multi_quote import multi_snapshot
from universe import load_universe, load_rules
from quant_scanner import scan_and_rank, scan_quote
from analysis_service import build_union, should_call_openai
from execution.auto_advice_executor import LiveExecutionConfig
import paper_portfolio
import position_analyzer
import strategy_catalog

DB_PATH = ROOT/'data'/'simulation'/'experiments.sqlite3'
RUNS = ROOT/'data'/'simulation'/'runs'


def cents(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError('資金須為有限數值')
    return int((amount*100).quantize(Decimal('1')))


class ExperimentStore:
    def __init__(self, path=DB_PATH, runs_path=None):
        self.path = Path(path)
        self.runs_path = Path(runs_path) if runs_path is not None else self.path.parent/'runs'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), initial_cents INTEGER NOT NULL,
                    slippage_bps TEXT NOT NULL, input_usd_per_million TEXT,
                    output_usd_per_million TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS strategies (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
                    source_path TEXT NOT NULL, version TEXT NOT NULL, sha256 TEXT NOT NULL,
                    prompt TEXT NOT NULL, model TEXT, started_at TEXT NOT NULL,
                    active INTEGER NOT NULL, cash_cents INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS positions (
                    strategy_id TEXT NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL,
                    cost_cents INTEGER NOT NULL, PRIMARY KEY(strategy_id,symbol));
                CREATE TABLE IF NOT EXISTS cycles (
                    run_id TEXT NOT NULL, strategy_id TEXT NOT NULL, at TEXT NOT NULL,
                    validation TEXT NOT NULL, market_view TEXT NOT NULL, decision_json TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    api_cost_usd TEXT, error TEXT, PRIMARY KEY(run_id,strategy_id));
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    at TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                    proposed_qty INTEGER NOT NULL, filled_qty INTEGER NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
                    at TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL,
                    side TEXT NOT NULL, qty INTEGER NOT NULL, price_cents INTEGER NOT NULL,
                    gross_cents INTEGER NOT NULL, fee_cents INTEGER NOT NULL,
                    reserved_cents INTEGER NOT NULL, cost_basis_cents INTEGER NOT NULL,
                    realized_cents INTEGER NOT NULL, reason TEXT NOT NULL,
                    strategy_version TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS marks (
                    symbol TEXT PRIMARY KEY, price_cents INTEGER NOT NULL, at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS equity (
                    run_id TEXT NOT NULL, strategy_id TEXT NOT NULL, at TEXT NOT NULL,
                    equity_cents INTEGER, unpriced_symbols TEXT NOT NULL,
                    PRIMARY KEY(run_id,strategy_id));
                CREATE TABLE IF NOT EXISTS capital_events (
                    id INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL, at TEXT NOT NULL,
                    event_type TEXT NOT NULL, delta_cents INTEGER NOT NULL,
                    capital_limit_cents INTEGER NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO settings VALUES(1,10000000,\'0\',NULL,NULL,?)',
                       (now().isoformat(),))
            strategy_columns = {row['name'] for row in db.execute('PRAGMA table_info(strategies)')}
            if 'initial_cents' not in strategy_columns:
                db.execute('ALTER TABLE strategies ADD COLUMN initial_cents INTEGER')
            if 'capital_limit_cents' not in strategy_columns:
                db.execute('ALTER TABLE strategies ADD COLUMN capital_limit_cents INTEGER')
            default_capital = db.execute('SELECT initial_cents FROM settings WHERE id=1').fetchone()[0]
            db.execute('UPDATE strategies SET initial_cents=COALESCE(initial_cents,?), '
                       'capital_limit_cents=COALESCE(capital_limit_cents,?)',
                       (default_capital, default_capital))
            equity_columns = {row['name'] for row in db.execute('PRAGMA table_info(equity)')}
            if 'capital_cents' not in equity_columns:
                db.execute('ALTER TABLE equity ADD COLUMN capital_cents INTEGER')
            db.execute('''UPDATE equity SET capital_cents=(SELECT initial_cents FROM strategies
                          WHERE strategies.id=equity.strategy_id) WHERE capital_cents IS NULL''')
            db.execute('''INSERT INTO capital_events(strategy_id,at,event_type,delta_cents,capital_limit_cents)
                          SELECT id,started_at,'INITIAL',initial_cents,initial_cents FROM strategies s
                          WHERE NOT EXISTS(SELECT 1 FROM capital_events e WHERE e.strategy_id=s.id)''')
            # Legacy column remains for existing databases. New fills use the
            # observed ask/bid directly, with no extra user-entered adjustment.
            db.execute("UPDATE settings SET slippage_bps='0' WHERE id=1 AND NOT EXISTS(SELECT 1 FROM cycles)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            return dict(db.execute('SELECT * FROM settings WHERE id=1').fetchone())

    def configure(self, initial_twd, input_rate=None, output_rate=None):
        initial = cents(initial_twd)
        if initial <= 0:
            raise ValueError('資金須大於 0')
        rates = []
        for raw in (input_rate, output_rate):
            if raw in (None, ''):
                rates.append(None)
            else:
                val = Decimal(str(raw))
                if not val.is_finite() or val < 0:
                    raise ValueError('API 單價須為非負數')
                rates.append(str(val))
        with self.connect() as db:
            db.execute('UPDATE settings SET initial_cents=?,slippage_bps=?,input_usd_per_million=?,output_usd_per_million=? WHERE id=1',
                       (initial, '0', *rates))
        return self.settings()

    def add_strategy(self, path, kind='AI', name=None, model=None, initial_twd=None):
        if kind not in ('AI', 'RULE'):
            raise ValueError('INVALID_STRATEGY_KIND')
        source = strategy_catalog.read_strategy(path)
        if kind == 'RULE' and Path(source['path']) != (ROOT/'strategies'/'fixed_rules.md').resolve():
            raise ValueError('固定規則基準只接受內建 fixed_rules.md')
        sid = uuid.uuid4().hex[:12]
        with self.connect() as db:
            if db.execute('SELECT 1 FROM strategies WHERE source_path=? AND sha256=? AND kind=?',
                          (source['path'], source['sha256'], kind)).fetchone():
                raise ValueError('相同檔案版本已在比較中；停用／啟用不會重置帳戶')
            settings = db.execute('SELECT initial_cents FROM settings WHERE id=1').fetchone()
            initial = cents(initial_twd) if initial_twd is not None else settings['initial_cents']
            if initial <= 0:
                raise ValueError('策略初始資金須大於 0')
            started = now().isoformat()
            db.execute('''INSERT INTO strategies
                          (id,name,kind,source_path,version,sha256,prompt,model,started_at,
                           active,cash_cents,initial_cents,capital_limit_cents)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (sid, (name or source['name']).strip(), kind, source['path'],
                        source['version'], source['sha256'], source['prompt'],
                        model or None, started, 1, initial, initial, initial))
            db.execute('''INSERT INTO capital_events(strategy_id,at,event_type,delta_cents,capital_limit_cents)
                          VALUES(?,?,?,?,?)''',(sid,started,'INITIAL',initial,initial))
        return sid

    def increase_capital(self, strategy_id, new_limit_twd):
        new_limit = cents(new_limit_twd)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT capital_limit_cents FROM strategies WHERE id=?',
                             (strategy_id,)).fetchone()
            if row is None:
                raise ValueError('策略不存在')
            old_limit = row['capital_limit_cents']
            if new_limit < old_limit:
                raise ValueError('資金上限只能維持或增加')
            delta = new_limit-old_limit
            if delta:
                db.execute('''UPDATE strategies SET capital_limit_cents=?,cash_cents=cash_cents+?
                              WHERE id=?''',(new_limit,delta,strategy_id))
                db.execute('''INSERT INTO capital_events(strategy_id,at,event_type,delta_cents,capital_limit_cents)
                              VALUES(?,?,?,?,?)''',
                           (strategy_id,now().isoformat(),'INCREASE',delta,new_limit))
        return self.summary(strategy_id)

    def capital_history(self, strategy_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT at,event_type,delta_cents,capital_limit_cents FROM capital_events '
                'WHERE strategy_id=? ORDER BY id',(strategy_id,))]

    def delete_strategy(self, strategy_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            strategy = db.execute('SELECT id,name FROM strategies WHERE id=?',(strategy_id,)).fetchone()
            if strategy is None:
                raise ValueError('策略不存在')
            run_ids = {row[0] for row in db.execute(
                'SELECT run_id FROM cycles WHERE strategy_id=? UNION '
                'SELECT run_id FROM equity WHERE strategy_id=?',(strategy_id,strategy_id))}
            for table in ('positions','cycles','orders','trades','equity','capital_events'):
                db.execute(f'DELETE FROM {table} WHERE strategy_id=?',(strategy_id,))
            db.execute('DELETE FROM strategies WHERE id=?',(strategy_id,))
        for run_id in run_ids:
            run_dir = self.runs_path/run_id
            if not run_dir.is_dir() or run_dir.resolve().parent != self.runs_path.resolve():
                continue
            (run_dir/f'{strategy_id}.json').unlink(missing_ok=True)
            manifest_path = run_dir/'run.json'
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                manifest.get('strategy_results',{}).pop(strategy_id,None)
                manifest.get('assumptions',{}).get('strategy_capital',{}).pop(strategy_id,None)
                if manifest.get('strategy_results'):
                    save_json(manifest_path,manifest)
                else:
                    shutil.rmtree(run_dir)
        return dict(strategy_id=strategy_id,name=strategy['name'])

    def set_active(self, strategy_id, active):
        with self.connect() as db:
            if db.execute('UPDATE strategies SET active=? WHERE id=?',
                          (int(bool(active)), strategy_id)).rowcount != 1:
                raise ValueError('策略不存在')

    def strategies(self, active_only=False):
        with self.connect() as db:
            sql = ('SELECT id,name,kind,source_path,version,sha256,prompt,model,started_at,active,'
                   'cash_cents,initial_cents,capital_limit_cents FROM strategies')
            if active_only:
                sql += ' WHERE active=1'
            return [dict(row) for row in db.execute(sql+' ORDER BY started_at,id')]

    def positions(self, strategy_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT symbol,qty,cost_cents FROM positions WHERE strategy_id=? ORDER BY symbol',
                (strategy_id,))]

    def account_input(self, strategy, rules):
        positions = self.positions(strategy['id'])
        committed = sum(p['cost_cents'] for p in positions)
        with self.connect() as db:
            day = now().date().isoformat()
            daily = db.execute('''SELECT COALESCE(SUM(reserved_cents),0) FROM trades
                                  WHERE strategy_id=? AND side='BUY' AND substr(at,1,10)=?''',
                               (strategy['id'], day)).fetchone()[0]
        available = max(0, min(strategy['cash_cents']-cents(rules.min_available_to_buy_twd),
                               strategy['capital_limit_cents']-committed,
                               cents(rules.max_daily_buy_twd)-daily))
        return dict(source='SIMULATION_MATCHED_LIVE_RULES',
                    capital_limit_twd=strategy['capital_limit_cents']/100,
                    available_cash_twd=available/100, available_cash_cents=available,
                    wallet_cash_twd=strategy['cash_cents']/100,
                    daily_buy_remaining_twd=max(0,cents(rules.max_daily_buy_twd)-daily)/100,
                    minimum_balance_reserve_twd=float(rules.min_available_to_buy_twd),
                    positions=positions, budget_status='READY' if available > 0 else 'BLOCKED')

    def record(self, run_id, strategy, candidates, decision, response, rules, fill_allowed,
               fill_at=None):
        settings = self.settings()
        usage = (response.get('api') or {}).get('usage') or {}
        input_tokens = int(usage.get('input_tokens') or 0)
        output_tokens = int(usage.get('output_tokens') or 0)
        cost = None
        if (settings['input_usd_per_million'] is not None and
                settings['output_usd_per_million'] is not None):
            cost = str((Decimal(input_tokens)*Decimal(settings['input_usd_per_million']) +
                        Decimal(output_tokens)*Decimal(settings['output_usd_per_million']))/1000000)
        fill_at = fill_at or now()
        at = fill_at.isoformat()
        by_symbol = {c['symbol']:c for c in candidates}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM cycles WHERE run_id=? AND strategy_id=?',
                          (run_id, strategy['id'])).fetchone():
                raise ValueError('模擬回合已處理，不能重複成交')
            row = db.execute('SELECT cash_cents,capital_limit_cents FROM strategies WHERE id=?',
                             (strategy['id'],)).fetchone()
            if row is None:
                raise ValueError('策略已刪除')
            cash = row['cash_cents']
            committed = db.execute('SELECT COALESCE(SUM(cost_cents),0) FROM positions WHERE strategy_id=?',
                                   (strategy['id'],)).fetchone()[0]
            daily = db.execute('''SELECT COALESCE(SUM(reserved_cents),0) FROM trades
                                  WHERE strategy_id=? AND side='BUY' AND substr(at,1,10)=?''',
                               (strategy['id'], at[:10])).fetchone()[0]
            for item in decision.get('decisions', []):
                side = item.get('decision')
                if side not in ('BUY', 'SELL'):
                    continue
                symbol = item['symbol']
                candidate = by_symbol.get(symbol, {})
                proposed = item.get('suggested_qty') or 0
                status, qty = 'REJECTED', 0
                reason = item.get('reason') or ''
                stamp = candidate.get('quote_timestamp')
                try:
                    fresh = (candidate.get('quote_status') == 'LIVE' and not candidate.get('is_trial')
                             and stamp and 0 <= (fill_at-datetime.fromisoformat(stamp)).total_seconds()
                             <= float(rules.max_quote_age_seconds))
                except (ValueError, TypeError):
                    fresh = False
                quote_price = candidate.get('ask') if side == 'BUY' else candidate.get('bid')
                confidence = item.get('confidence')
                confidence_ok = (type(confidence) in (int,float) and
                                 Decimal(str(confidence))*100 >= rules.min_confidence_pct)
                if (fill_allowed and fresh and quote_price and confidence_ok and
                        type(proposed) is int and proposed > 0):
                    price = cents(quote_price)
                    position = db.execute('SELECT qty,cost_cents FROM positions WHERE strategy_id=? AND symbol=?',
                                          (strategy['id'], symbol)).fetchone()
                    held = position['qty'] if position else 0
                    if side == 'BUY':
                        budget = max(0, min(cash-cents(rules.min_available_to_buy_twd),
                                            row['capital_limit_cents']-committed,
                                            cents(rules.max_daily_buy_twd)-daily))
                        for trial_qty in range(min(proposed, rules.max_buy_qty or 999, 999), 0, -1):
                            gross = price*trial_qty
                            fee = paper_portfolio.fee_twd(gross)*100
                            reserve = max(cents(rules.min_buy_fee_reserve_twd),
                                          int((Decimal(gross)*rules.buy_cash_buffer_rate).to_integral_value()), fee)
                            if (gross+reserve <= budget and gross+fee <= cash and
                                    (rules.max_order_twd is None or gross <= cents(rules.max_order_twd))):
                                qty = trial_qty
                                break
                        if qty:
                            total = gross+fee
                            cash -= total
                            committed += total
                            daily += gross+reserve
                            new_qty, new_cost = held+qty, (position['cost_cents'] if position else 0)+total
                            cost_basis, realized = total, 0
                    else:
                        qty = min(proposed, held, 999)
                        if rules.max_order_twd is not None:
                            qty = min(qty, cents(rules.max_order_twd)//price)
                        if qty:
                            gross = price*qty
                            fee = paper_portfolio.fee_twd(gross)*100
                            reserve = 0
                            cost_basis = position['cost_cents'] if qty == held else position['cost_cents']*qty//held
                            realized = gross-fee-cost_basis
                            cash += gross-fee
                            committed -= cost_basis
                            new_qty, new_cost = held-qty, position['cost_cents']-cost_basis
                    if qty:
                        if new_qty:
                            db.execute('INSERT OR REPLACE INTO positions VALUES(?,?,?,?)',
                                       (strategy['id'],symbol,new_qty,new_cost))
                        else:
                            db.execute('DELETE FROM positions WHERE strategy_id=? AND symbol=?',
                                       (strategy['id'],symbol))
                        db.execute('''INSERT INTO trades(run_id,strategy_id,at,symbol,name,side,qty,price_cents,
                                      gross_cents,fee_cents,reserved_cents,cost_basis_cents,realized_cents,reason,strategy_version)
                                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                   (run_id,strategy['id'],at,symbol,candidate.get('name') or '',side,qty,price,gross,
                                    fee,gross+reserve if side=='BUY' else 0,cost_basis,realized,reason,strategy['version']))
                        status = 'FILLED'
                db.execute('''INSERT INTO orders(run_id,strategy_id,at,symbol,side,proposed_qty,filled_qty,status,reason)
                              VALUES(?,?,?,?,?,?,?,?,?)''',
                           (run_id,strategy['id'],at,symbol,side,proposed,qty,status,reason))
            db.execute('UPDATE strategies SET cash_cents=? WHERE id=?', (cash,strategy['id']))
            db.execute('''INSERT INTO cycles VALUES(?,?,?,?,?,?,?,?,?,?)''',
                       (run_id,strategy['id'],at,response.get('validation','NOT_CALLED'),
                        decision.get('market_view',''),json.dumps(decision,ensure_ascii=False),
                        input_tokens,output_tokens,cost,str((response.get('api') or {}).get('error') or '') or None))
        return self.summary(strategy['id'])

    def mark_equity(self, run_id, strategy_ids, quotes):
        at = now().isoformat()
        fresh_marks = {q['symbol']:cents(q['last_price']) for q in quotes
                       if q.get('quote_status') == 'LIVE' and q.get('last_price')}
        with self.connect() as db:
            for symbol, price in fresh_marks.items():
                db.execute('INSERT OR REPLACE INTO marks VALUES(?,?,?)',(symbol,price,at))
            for sid in strategy_ids:
                cash = db.execute('SELECT cash_cents FROM strategies WHERE id=?',(sid,)).fetchone()['cash_cents']
                equity, unpriced = cash, []
                for p in db.execute('SELECT symbol,qty FROM positions WHERE strategy_id=?',(sid,)):
                    if p['symbol'] in fresh_marks:
                        equity += p['qty']*fresh_marks[p['symbol']]
                    else:
                        unpriced.append(p['symbol'])
                capital = db.execute('SELECT capital_limit_cents FROM strategies WHERE id=?',
                                     (sid,)).fetchone()['capital_limit_cents']
                db.execute('''INSERT OR REPLACE INTO equity
                              (run_id,strategy_id,at,equity_cents,unpriced_symbols,capital_cents)
                              VALUES(?,?,?,?,?,?)''',
                           (run_id,sid,at,None if unpriced else equity,json.dumps(unpriced),capital))

    def summary(self, strategy_id):
        settings = self.settings()
        with self.connect() as db:
            strategy = dict(db.execute('SELECT * FROM strategies WHERE id=?',(strategy_id,)).fetchone())
            positions = [dict(r) for r in db.execute('SELECT * FROM positions WHERE strategy_id=?',(strategy_id,))]
            latest = db.execute('SELECT equity_cents,unpriced_symbols,capital_cents FROM equity WHERE strategy_id=? ORDER BY rowid DESC LIMIT 1',
                                (strategy_id,)).fetchone()
            unpriced = json.loads(latest['unpriced_symbols']) if latest else [p['symbol'] for p in positions]
            equity = (latest['equity_cents']+strategy['capital_limit_cents']-latest['capital_cents']
                      if latest and latest['equity_cents'] is not None else
                      None if latest else strategy['cash_cents'])
            sells = [r['realized_cents'] for r in db.execute(
                "SELECT realized_cents FROM trades WHERE strategy_id=? AND side='SELL'",(strategy_id,))]
            sequence = [(r['equity_cents'],r['capital_cents']) for r in db.execute(
                'SELECT equity_cents,capital_cents FROM equity WHERE strategy_id=? ORDER BY at,run_id',
                (strategy_id,)) if r['equity_cents'] is not None]
            nav, peak, drawdown = 1.0, 1.0, 0.0
            previous_equity, previous_capital = strategy['initial_cents'],strategy['initial_cents']
            for value, capital in sequence+([(equity,strategy['capital_limit_cents'])]
                                             if equity is not None else []):
                contribution = capital-previous_capital
                if previous_equity > 0:
                    nav *= (value-contribution)/previous_equity
                peak = max(peak,nav)
                drawdown = max(drawdown,(peak-nav)/peak)
                previous_equity,previous_capital = value,capital
            usage = db.execute('''SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0)
                                  FROM cycles WHERE strategy_id=?''',(strategy_id,)).fetchone()
            estimated_cost = None
            if (settings['input_usd_per_million'] is not None and
                    settings['output_usd_per_million'] is not None):
                estimated_cost = ((Decimal(usage[0])*Decimal(settings['input_usd_per_million']) +
                                   Decimal(usage[1])*Decimal(settings['output_usd_per_million']))/1000000)
            buys = db.execute("SELECT COUNT(*) FROM trades WHERE strategy_id=? AND side='BUY'",(strategy_id,)).fetchone()[0]
            profit = sum(max(0,x) for x in sells)
            loss = -sum(min(0,x) for x in sells)
            return dict(id=strategy_id,name=strategy['name'],kind=strategy['kind'],version=strategy['version'],
                        started_at=strategy['started_at'],active=bool(strategy['active']),
                        cash_twd=strategy['cash_cents']/100,
                        initial_capital_twd=strategy['initial_cents']/100,
                        capital_limit_twd=strategy['capital_limit_cents']/100,
                        equity_twd=None if unpriced or equity is None else equity/100,
                        return_pct=None if unpriced or equity is None else (nav-1)*100,
                        unpriced=unpriced,positions=positions,trade_count=buys+len(sells),
                        win_rate=None if not sells else sum(x>0 for x in sells)/len(sells),
                        max_drawdown=drawdown if sequence else None,
                        profit_factor=None if not loss else profit/loss,
                        profit_factor_infinite=bool(sells and profit and not loss),
                        input_tokens=usage[0],output_tokens=usage[1],
                        api_cost_usd=estimated_cost)

    def recent_trades(self, strategy_id, limit=100):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM trades WHERE strategy_id=? ORDER BY id DESC LIMIT ?', (strategy_id,limit))]

    def recent_orders(self, strategy_id, limit=100):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM orders WHERE strategy_id=? ORDER BY id DESC LIMIT ?', (strategy_id,limit))]

    def latest_decision(self, strategy_id):
        with self.connect() as db:
            row = db.execute('SELECT decision_json FROM cycles WHERE strategy_id=? ORDER BY rowid DESC LIMIT 1',
                             (strategy_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def equity_series(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT run_id,strategy_id,at,equity_cents FROM equity ORDER BY at,run_id')]


def fixed_rule_decision(candidates, account):
    """Transparent benchmark: top positive score buys; +/-10/5 percent exits."""
    best = max((c for c in candidates if c.get('analysis_source','').startswith('TOP10')
                and c.get('quote_status')=='LIVE' and not c.get('is_trial')
                and c.get('eligible_strategies')
                and (c.get('quant_score') or 0)>0 and not c['paper_position']['qty']),
               key=lambda c:c.get('quant_score') or 0, default=None)
    decisions = []
    for c in candidates:
        side, qty, ratio, reason = 'WAIT',0,0,'固定規則：未達買入或賣出條件。'
        held = c['paper_position']['qty']
        price = c.get('last_price')
        if held and price and c.get('quote_status')=='LIVE':
            avg = c['paper_position']['cost_cents']/100/held
            if held <= 999 and (price >= avg*1.10 or price <= avg*.95):
                side,qty,ratio = 'SELL',held,1
                reason = '固定規則：相對含費成本達 +10% 停利或 -5% 停損。'
        elif c is best:
            qty = min(int((account['available_cash_twd']*.20)//(c.get('ask') or price)),c.get('max_buy_qty') or 0)
            if qty > 0:
                side,reason = 'BUY','固定規則：量化排名最高且分數為正，配置不超過本輪預算 20%。'
        strategy_id = (c.get('eligible_strategies') or ['NONE'])[0] if side == 'BUY' else 'NONE'
        decisions.append(dict(symbol=c['symbol'],decision=side,confidence=1,action_ratio=ratio,
                              suggested_qty=qty,reference_price=price,
                              suggested_price=(c.get('ask') or price) if side=='BUY' else
                                              (c.get('bid') or price) if side=='SELL' else None,
                              strategy_id=strategy_id,
                              target_price=round(price * 1.10, 2) if side=='BUY' else None,
                              invalidation_price=round(price * .95, 2) if side=='BUY' else None,
                              holding_days=10 if side=='BUY' else None,
                              reason=reason,data_quality=c['quote_status'],
                              warnings=['預算：遵守共同資金限制'] if side=='BUY' else
                                       ['不是即時行情'] if c['quote_status']=='LAST_KNOWN' else []))
    return dict(market_view='固定規則基準：量化最高正分買入，+10%／-5% 出場。',decisions=decisions)


class ExperimentService:
    def __init__(self, store=None, research_db=None, quote_provider=None, ai_provider=None):
        self.store = store or ExperimentStore()
        self.research_db = Path(research_db or ROOT/'data'/'analysis'/'market_history.sqlite3')
        if not self.research_db.is_file():
            self.research_db = self.store.path
        self.quote_provider = quote_provider
        self.ai_provider = ai_provider

    def run(self, trigger='MANUAL', phase=lambda _:None):
        if trigger not in ('MANUAL','AUTO'):
            raise ValueError('INVALID_TRIGGER')
        load_env()
        strategies = self.store.strategies(active_only=True)
        if not strategies:
            raise ValueError('請先加入並啟用至少一個模擬策略')
        rules = LiveExecutionConfig.load(ROOT/'config'/'execution_live.json')
        run_id = now().strftime('%Y%m%dT%H%M%S')+'-sim-'+uuid.uuid4().hex[:8]
        directory = self.store.runs_path/run_id
        directory.mkdir(parents=True)
        universe = load_universe(ROOT/'config'/'universe_tw.json')
        scan_rules = load_rules(ROOT/'config'/'scan_rules.json')
        scan_rules['candidate_top_n'] = 10
        symbols = list(dict.fromkeys(universe['symbols']+[p['symbol'] for s in self.store.strategies()
                                 for p in self.store.positions(s['id'])]))
        phase('取得共同行情')
        started = now().isoformat()
        if self.quote_provider:
            raw, errors = self.quote_provider(symbols)
        else:
            from execution.broker_runtime import get_broker_runtime
            raw, errors = get_broker_runtime(ROOT).fetch_quotes(symbols,Audit(directory),lambda:False,timeout=12)
        batch = multi_snapshot(symbols,raw,errors,started,now().isoformat())
        save_json(directory/'market_snapshot.json',batch)
        phase('共同量化排名')
        at = now()
        scanned, ranked, _ = scan_and_rank([q for q in batch['quotes'] if q['symbol'] in universe['symbols']],scan_rules,at)
        import scan_store
        import strategy_evidence
        quote_map = {q['symbol']:q for q in batch['quotes']}
        top = [r for r in ranked if strategy_evidence.assess(
            quote_map[r['symbol']], scan_store.recent_history(self.research_db, r['symbol'], started)
        )['eligible_strategies']][:scan_rules.get('candidate_top_n', 10)]
        scanned += [scan_quote(q,scan_rules,at) for q in batch['quotes'] if q['symbol'] not in universe['symbols']]
        save_json(directory/'ranking.json',dict(top=top,ranked=ranked,scan_rules=scan_rules))
        jobs = {}
        for s in strategies:
            account = self.store.account_input(s,rules)
            candidates = build_union(top,ranked,batch['quotes'],[],self.research_db,started,scanned,
                                     universe.get('etf_symbols',[]),self.store.positions(s['id']),live_managed=True)
            for c in candidates:
                c['budget_price'] = c.get('ask') or c.get('last_price')
                c['max_buy_qty'] = 0
                if c.get('quote_status')=='LIVE' and not c.get('is_trial') and c['budget_price']:
                    c['max_buy_qty'] = min(paper_portfolio.max_affordable_qty(account['available_cash_cents'],c['budget_price']),
                                           rules.max_buy_qty or 999)
                    while c['max_buy_qty']:
                        gross = cents(c['budget_price'])*c['max_buy_qty']
                        reserve = max(cents(rules.min_buy_fee_reserve_twd),
                                      int((Decimal(gross)*rules.buy_cash_buffer_rate).to_integral_value()))
                        if (gross+reserve <= account['available_cash_cents'] and
                                (rules.max_order_twd is None or gross <= cents(rules.max_order_twd))):
                            break
                        c['max_buy_qty'] -= 1
            request = position_analyzer.build_request(candidates,account,
                                                       strategy_prompt=s['prompt'],model=s['model']) if s['kind']=='AI' else None
            jobs[s['id']] = (s,account,candidates,request)
        results = {}
        phase('各策略獨立決策')
        def decide(job):
            s, account, candidates, request = job
            if not should_call_openai(trigger,candidates,False,now().timestamp()):
                return (dict(market_view='非有效盤中即時行情；本輪不交易。',decisions=[]),
                        dict(validation='NOT_CALLED',api={}))
            if s['kind']=='RULE':
                result = fixed_rule_decision(candidates,account)
                position_analyzer.validate(result,candidates,account['available_cash_cents'],
                                           rules.min_buy_fee_reserve_twd,rules.buy_cash_buffer_rate)
                return result,dict(validation='VALID',api={})
            return position_analyzer.analyze(candidates,request,provider=self.ai_provider) if self.ai_provider else position_analyzer.analyze(candidates,request)
        with ThreadPoolExecutor(max_workers=min(4,len(jobs))) as pool:
            futures = {pool.submit(decide,job):sid for sid,job in jobs.items()}
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    results[sid] = future.result()
                except Exception as exc:
                    results[sid] = (dict(market_view='策略分析失敗，本輪不交易。',decisions=[]),
                                    dict(validation='ERROR',api={'error':redact(exc)}))
        phase('記錄模擬成交')
        cycle_result = {}
        # Every strategy uses the same captured quotes and the same fill deadline.
        fill_at = now()
        fill_allowed = should_call_openai(trigger,batch['quotes'],False,fill_at.timestamp())
        for sid,(s,account,candidates,request) in jobs.items():
            decision,response = results[sid]
            save_json(directory/f'{sid}.json',dict(strategy={k:s[k] for k in ('id','name','version','started_at')},
                                                  account_before=account,candidates=candidates,request=request,
                                                  decision=decision,response=response))
            self.store.record(run_id,s,candidates,decision,response,rules,
                              fill_allowed and response['validation']=='VALID',fill_at=fill_at)
            cycle_result[sid] = dict(validation=response['validation'],error=(response.get('api') or {}).get('error'))
        self.store.mark_equity(run_id,[s['id'] for s in self.store.strategies()],batch['quotes'])
        summaries = [self.store.summary(s['id']) for s in self.store.strategies()]
        assumptions = {k:v for k,v in self.store.settings().items()
                       if k not in ('slippage_bps','initial_cents')}
        assumptions['fill_price'] = 'observed_ask_for_buy_bid_for_sell'
        assumptions['strategy_capital'] = {s['id']:s['capital_limit_cents']/100 for s in strategies}
        manifest = dict(run_id=run_id,mode='SIMULATION',started_at=started,finished_at=now().isoformat(),
                        shared_market_source='CAPITAL_SKCOM',strategy_results=cycle_result,
                        assumptions=assumptions,live_rules=json.loads((ROOT/'config'/'execution_live.json').read_text(encoding='utf-8')),
                        broker_order_sent=False)
        save_json(directory/'run.json',manifest)
        return dict(manifest=manifest,batch=batch,top=top,summaries=summaries,
                    decisions={sid:results[sid][0] for sid in jobs})
