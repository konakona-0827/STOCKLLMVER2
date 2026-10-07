"""Offline tests: synthetic fixtures stay in temporary databases."""
import contextlib
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from config import TAIPEI, current_slot, next_slot, load_env, now, redact
from ledger import Ledger
from llm import validate
from market import parse_quote, quality, tick
from strategy import run_cycle, context, sizing
from main import single_instance
from capital_check import parse_inventory, unpack, error_info
from capital import CapitalMarket, normalize_stock
from types import SimpleNamespace


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'trading.sqlite3'
        self.ledger = Ledger(self.path)
        self.at = datetime(2026, 10, 7, 10, tzinfo=TAIPEI)
        self.clock_patch = patch('strategy.now', return_value=self.at)
        self.clock_patch.start()

    def tearDown(self):
        self.clock_patch.stop()
        self.ledger.close()
        self.tmp.cleanup()

    def decision(self, cycle='one', action='BUY', qty=100):
        self.ledger.claim_cycle(cycle)
        return self.ledger.decision(cycle, dict(symbol='1234', action=action, qty=qty, reason='test'), '{}')

    def quotes(self, price=2):
        start = self.at - timedelta(seconds=360)
        qs = []
        for i in range(12):
            stamp = start + timedelta(seconds=i * 32)
            qs.append(dict(symbol='1234', name='TEST', exchange_time=stamp.isoformat(),
                           fetched_at=stamp.isoformat(), price=price, bid=price, ask=price,
                           volume_shares=100, source='CAPITAL_SKCOM', quote_basis='capital_intraday_odd_lot',
                           quality='FRESH', evidence_id=str(i)))
        self.ledger.store_quotes(qs)

    def provider(self, action='BUY', qty=100):
        return lambda payload: (json.dumps(dict(summary='test', actions=[dict(
            symbol='1234', action=action, qty=qty, reason='test')])), {'model': 'offline_fixture'})

    def cycle(self, cycle, provider=None, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_cycle(self.ledger, cycle, ['1234'], provider or self.provider(), **kwargs)

    def test_capital_and_fees(self):
        did = self.decision()
        self.assertEqual(self.ledger.paper_fill(did, '1234', 'BUY', 1000, 10)[0], 'CAPITAL_LIMIT_REJECTED')
        self.assertEqual(self.ledger.portfolio()['committed'], 0)

    def test_pending_and_unknown_reserve(self):
        did = self.decision()
        for idx, status in enumerate(['CREATED', 'SUBMITTED', 'UNKNOWN']):
            self.ledger.db.execute('''INSERT INTO orders(local_order_id,symbol,side,qty,price,estimated_value,status)
                                  VALUES(?,?,?,?,?,?,?)''', (str(idx), '1234', 'BUY', 1000, 3, 300000, status))
        self.ledger.db.commit()
        self.assertEqual(self.ledger.portfolio()['committed'], 9000)
        self.assertEqual(self.ledger.paper_fill(did, '1234', 'BUY', 1000, 2)[0], 'CAPITAL_LIMIT_REJECTED')

    def test_sell_ownership(self):
        did = self.decision(action='SELL')
        self.assertEqual(self.ledger.paper_fill(did, '1234', 'SELL', 1000, 2)[0], 'SELL_REJECTED_LOCAL_OWNERSHIP')

    def test_buy_sell_cost_release(self):
        did = self.decision()
        self.ledger.paper_fill(did, '1234', 'BUY', 1000, 2)
        self.assertEqual(self.ledger.portfolio()['committed'], 2020)
        sell = self.decision('two', 'SELL')
        self.ledger.paper_fill(sell, '1234', 'SELL', 1000, 3)
        p = self.ledger.portfolio()
        self.assertEqual(p['committed'], 0)
        self.assertEqual(p['positions'], {})
        self.assertEqual(p['cash_twd'], 10951)

    def test_repeat_transport_not_filled_twice(self):
        did = self.decision()
        self.ledger.paper_fill(did, '1234', 'BUY', 1000, 2)
        self.assertEqual(self.ledger.paper_fill(did, '1234', 'BUY', 1000, 2)[0], 'ALREADY_SIMULATED')
        self.assertEqual(self.ledger.portfolio()['committed'], 2020)

    def test_restart_slot_claim(self):
        self.assertTrue(self.ledger.claim_cycle('2026-10-07T10:00+08:00'))
        second = Ledger(self.path)
        try:
            self.assertFalse(second.claim_cycle('2026-10-07T10:00+08:00'))
        finally:
            second.close()

    def test_different_cycles_same_symbol_can_buy(self):
        self.quotes(price=20)
        for slot in ['09:00', '09:30']:
            result = self.cycle(slot)
            self.assertTrue(result['decisions'][0]['simulated_order_sent'])
        self.assertEqual(self.ledger.portfolio()['positions']['1234']['qty'], 200)

    def test_same_cycle_one_llm_call(self):
        self.quotes()
        calls = []
        def provider(payload):
            calls.append(payload)
            return self.provider('HOLD', 0)(payload)
        self.cycle('same', provider)
        self.cycle('same', provider)
        self.assertEqual(len(calls), 1)

    def test_invalid_json_raw_saved_and_next_cycle_continues(self):
        self.quotes()
        bad = self.cycle('bad', lambda _: ('not json', {}))
        self.assertEqual(bad['error'], 'LLM_JSON_INVALID')
        self.assertEqual(self.ledger.db.execute('SELECT raw_llm_response FROM strategy_cycles WHERE cycle_id=?', ('bad',)).fetchone()[0], 'not json')
        self.assertTrue(self.cycle('next')['decisions'][0]['simulated_order_sent'])

    def test_llm_timeout_continues(self):
        self.quotes()
        def fail(_):
            raise TimeoutError('fixture')
        self.assertIn('error', self.cycle('fail', fail))
        self.assertTrue(self.cycle('next')['decisions'][0]['simulated_order_sent'])

    def test_stale_skips_paid_call(self):
        self.at -= timedelta(days=1)
        self.quotes()
        def fail(_):
            raise AssertionError('Must not call LLM')
        self.assertIsNone(self.cycle('stale', fail))
        self.assertEqual(self.ledger.db.execute('SELECT status FROM strategy_cycles').fetchone()[0], 'SKIPPED')

    def test_probe_uses_llm_without_fills(self):
        self.quotes()
        result = self.cycle('probe', probe=True)
        self.assertTrue(result['decisions'][0]['llm_wants_order'])
        self.assertFalse(result['decisions'][0]['simulated_order_sent'])
        self.assertEqual(self.ledger.portfolio()['committed'], 0)

    def test_stop_after_api_before_order(self):
        self.quotes()
        stopped = [False]
        def provider(payload):
            stopped[0] = True
            return self.provider()(payload)
        self.assertIsNone(self.cycle('stop', provider, stopped=lambda: stopped[0]))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)

    def test_stop_before_api(self):
        self.assertIsNone(self.cycle('stop', stopped=lambda: True))
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM strategy_cycles').fetchone()[0], 0)

    def test_prompt_risk_exit(self):
        self.ledger.paper_fill(self.decision(qty=37), '1234', 'BUY', 37, 2)
        self.quotes(price=3)
        result = self.cycle('exit', self.provider('HOLD', 0))
        self.assertEqual(result['decisions'][0]['source'], 'PROMPT_RISK_RULE')
        self.assertEqual(self.ledger.portfolio()['positions'], {})

    def test_slots_taipei_and_weekend(self):
        at = datetime(2026, 10, 7, 9, 29, tzinfo=TAIPEI)
        self.assertEqual(current_slot(at), '2026-10-07T09:00+08:00')
        self.assertEqual(next_slot(at), '2026-10-07T09:30+08:00')
        self.assertIsNone(current_slot(at.replace(hour=13, minute=30)))
        self.assertIsNone(current_slot(at.replace(day=10)))

    def test_quote_missing_last_price_not_fabricated(self):
        item = dict(c='1234', d='20261007', t='10:00:00', z='-', b='2.0_1.9_', a='2.1_2.2_', v='10')
        q = parse_quote(item, datetime(2026, 10, 7, 10, tzinfo=TAIPEI))
        self.assertIsNone(q['price'])
        self.assertEqual(q['bid'], 2)
        self.assertEqual(q['quality'], 'FRESH')

    def test_quote_future_and_expired(self):
        self.assertEqual(quality((self.at + timedelta(minutes=1)).isoformat(), self.at), 'STALE')
        self.assertEqual(quality((self.at - timedelta(minutes=3)).isoformat(), self.at), 'STALE')

    def test_bad_llm_quantities_symbols(self):
        for qty, symbol in [(True, '1234'), (-1, '1234'), (1.5, '1234'), (1000, '1234'), (1, '9999')]:
            with self.assertRaises(ValueError):
                validate(dict(summary='test', actions=[dict(action='BUY', symbol=symbol, qty=qty, reason='x')]), {'1234'})

    def test_env_continuation_and_redaction(self):
        path = Path(self.tmp.name) / '.env'
        path.write_text('OPENAI_API_KEY=\nsk-fixture-secret\n', encoding='utf-8')
        with patch.dict(os.environ, {}, clear=True):
            load_env(path)
            self.assertEqual(os.environ['OPENAI_API_KEY'], 'sk-fixture-secret')
            self.assertEqual(redact('sk-fixture-secret'), '[REDACTED]')

    def test_tick_threshold(self):
        self.assertEqual(tick(50, 'SELL'), .05)
        self.assertEqual(tick(50, 'BUY'), .1)

    def test_single_process_lock_released(self):
        path = Path(self.tmp.name) / 'runtime.lock'
        with single_instance(path):
            with self.assertRaises(RuntimeError):
                with single_instance(path):
                    pass
        with single_instance(path):
            pass

    def test_inventory_parse_omits_identifiers(self):
        raw = '2330,T,0,0,0,0,100,10,5,3,2,101,0,0,101,X,0,PRIVATE_LOGIN,PRIVATE_ACCOUNT'
        result = parse_inventory(raw)
        self.assertEqual(result['current_qty'], 101)
        self.assertEqual(result['sellable_qty'], 101)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(parse_inventory('##'), {'end': True})
        with self.assertRaises(ValueError):
            parse_inventory('invalid')

    def test_com_result_and_error_code(self):
        self.assertEqual(unpack((1, 0)), 0)
        self.assertEqual(unpack(3031), 3031)
        exc = OSError('test')
        exc.winerror = -2147221164
        self.assertEqual(error_info(exc)['hresult'], '0x80040154')

    def test_capital_diagnostic_contains_no_mutating_broker_calls(self):
        import ast
        tree = ast.parse(Path(__file__).with_name('capital_check.py').read_text(encoding='utf-8'))
        methods = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(any(m.startswith(('SendStock', 'SendFuture', 'SendOption', 'CancelOrder', 'DecreaseOrder')) for m in methods))

    def test_one_share_buy_is_not_one_thousand(self):
        self.quotes(price=2500)
        result = self.cycle('one-share', self.provider('BUY', 1))
        self.assertTrue(result['decisions'][0]['simulated_order_sent'])
        self.assertEqual(self.ledger.portfolio()['positions']['1234']['qty'], 1)
        self.assertEqual(self.ledger.portfolio()['committed'], 2525)
        self.assertEqual(self.ledger.db.execute('SELECT qty FROM decisions').fetchone()[0], 1)

    def test_partial_odd_lot_sell(self):
        self.ledger.paper_fill(self.decision(qty=37), '1234', 'BUY', 37, 100)
        self.quotes(price=100)
        result = self.cycle('partial-sell', self.provider('SELL', 7))
        self.assertEqual(result['decisions'][0]['qty'], 7)
        self.assertTrue(result['decisions'][0]['simulated_order_sent'])
        self.assertEqual(self.ledger.portfolio()['positions']['1234']['qty'], 30)

    def test_odd_lot_oversell_rejected(self):
        self.ledger.paper_fill(self.decision(qty=37), '1234', 'BUY', 37, 100)
        self.quotes(price=100)
        result = self.cycle('oversell', self.provider('SELL', 38))
        self.assertEqual(result['decisions'][0]['outcome'], 'SELL_REJECTED_LOCAL_OWNERSHIP')

    def test_sizing_uses_fee_and_exposure_budget(self):
        self.quotes(price=2500)
        data = context(self.ledger, ['1234'], self.at)
        s = data['candidates'][0]['sizing']
        self.assertEqual(s['max_buy_qty'], 2)
        self.assertEqual(s['max_buy_total_twd'], 5030)
        self.assertEqual(s['max_sell_qty'], 0)
        self.assertEqual(data['limits']['quantity_unit'], 'shares')

    def test_sizing_reserved_cash_and_owned_stock(self):
        p = dict(cash_twd=100, reserved_capital=50, committed=9950,
                 positions={'1234': {'qty': 37, 'cost_cents': 10000}})
        s = sizing(p, dict(symbol='1234', bid=10, ask=10))
        self.assertEqual(s['max_buy_qty'], 0)
        self.assertEqual(s['max_sell_qty'], 37)

    def test_sizing_fee_boundary(self):
        p = dict(cash_twd=30.05, reserved_capital=0, committed=0, positions={})
        q = dict(symbol='1234', bid=10, ask=10)
        self.assertEqual(sizing(p, q)['max_buy_qty'], 1)
        p['cash_twd'] = 30.04
        self.assertEqual(sizing(p, q)['max_buy_qty'], 0)

    def stock(self, **overrides):
        fields = dict(nTradingLotFlag=1, nTradingDay=20261007, nDealTime=100000,
                      sDecimal=2, bstrStockNo='1234', bstrStockName='TEST',
                      nClose=2000, nBid=2000, nAsk=2005, nRef=2000, nTQty=123,
                      nSimulate=0)
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_capital_normalization_units_and_exchange_time(self):
        q = normalize_stock(self.stock(), self.at)
        self.assertEqual(q['source'], 'CAPITAL_SKCOM')
        self.assertEqual(q['ask'], 20.05)
        self.assertEqual(q['volume_shares'], 123)
        self.assertEqual(q['exchange_time'], self.at.isoformat())
        self.assertEqual(q['quote_basis'], 'capital_intraday_odd_lot')

    def test_capital_rejects_regular_board_and_invalid_time(self):
        for stock in [self.stock(nTradingLotFlag=0), self.stock(nTradingDay=0), self.stock(nDealTime=250000)]:
            with self.assertRaises(ValueError):
                normalize_stock(stock, self.at)

    def test_capital_trial_does_not_fill(self):
        self.quotes(price=20)
        trial = normalize_stock(self.stock(nSimulate=1), self.at)
        self.ledger.store_quotes([trial])
        result = self.cycle('trial')
        self.assertEqual(result['decisions'][0]['outcome'], 'TRIAL_QUOTE_NO_FILL')

    def test_old_public_quote_never_reaches_llm(self):
        q = normalize_stock(self.stock(), self.at)
        q['source'] = 'TWSE_MIS'
        self.ledger.store_quotes([q])
        def fail(_):
            raise AssertionError('Public source cannot trigger an API call')
        self.assertIsNone(self.cycle('no-capital', fail, probe=True))

    def test_capital_quote_replaces_public_same_timestamp(self):
        q = normalize_stock(self.stock(), self.at)
        self.ledger.store_quotes([{**q, 'source': 'TWSE_MIS'}])
        self.ledger.store_quotes([q])
        self.assertEqual(self.ledger.latest('1234', 'CAPITAL_SKCOM')['source'], 'CAPITAL_SKCOM')

    def test_broker_inventory_is_observation_not_sell_ownership(self):
        self.quotes(price=20)
        broker = dict(status='COMPLETE', positions=[dict(symbol='1234', current_qty=999)])
        result = self.cycle('broker-owned', self.provider('SELL', 5), broker_inventory=broker)
        self.assertEqual(result['decisions'][0]['outcome'], 'SELL_REJECTED_LOCAL_OWNERSHIP')
        payload = json.loads(self.ledger.db.execute('SELECT input_json FROM strategy_cycles').fetchone()[0])
        self.assertEqual(payload['broker_inventory_observation']['positions'][0]['current_qty'], 999)
        self.assertEqual(payload['portfolio']['positions'], {})

    def test_capital_fetch_subscribes_market_five_and_reads_callback(self):
        calls = []
        stock = self.stock()
        q = SimpleNamespace(SKQuoteLib_RequestStocksWithMarketNo=lambda *a: calls.append(a) or (1, 0),
                            SKQuoteLib_GetStockByIndexLONG=lambda *a: (stock, 0))
        market = CapitalMarket(self.ledger)
        market.connected = True
        market.objects = {'SKQuoteLib': q}
        market.sk = SimpleNamespace(SKSTOCKLONG=lambda: SimpleNamespace())
        market.client = SimpleNamespace(PumpEvents=lambda _: None)
        market.pending_indices = {(5, 42)}
        market.wait = lambda p, seconds=15: p()
        with patch('capital.now', return_value=self.at):
            quotes, issues = market.fetch(['1234'])
        self.assertEqual(calls, [(1, 5, '1234')])
        self.assertEqual(quotes[0]['source'], 'CAPITAL_SKCOM')
        self.assertEqual(quotes[0]['quality'], 'FRESH')
        self.assertEqual(market.inventory()['status'], 'ACCOUNT_UNAVAILABLE_OR_AMBIGUOUS')

    def test_capital_failed_connect_retries_slowly(self):
        market = CapitalMarket(self.ledger)
        with patch.object(market, 'initialize', side_effect=OSError('unregistered')) as init:
            with self.assertRaises(OSError):
                market.connect()
            with self.assertRaisesRegex(RuntimeError, 'RETRY_INTERVAL'):
                market.connect()
            self.assertEqual(init.call_count, 1)

    def test_capital_implementation_never_calls_order_mutations(self):
        import ast
        tree = ast.parse(Path(__file__).with_name('capital.py').read_text(encoding='utf-8'))
        methods = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(any(m.startswith(('Send', 'Cancel', 'Decrease')) for m in methods))


if __name__ == '__main__':
    unittest.main()
