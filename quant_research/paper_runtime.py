"""Read-only market data -> independent planned Paper cycles. No live dispatch."""
from datetime import timedelta
from pathlib import Path
import json
import uuid
import hashlib
import subprocess

from config import ROOT, CODE_ROOT, now, load_env, redact, TAIPEI
from main import Audit, save_json
from market_calendar import twse_closed_reason
from .core import Policy
from .limit_orders import LimitOrderBook
from .planned_cycle import cycle


def market_open(at):
    local = at.astimezone(TAIPEI)
    return (not twse_closed_reason(local.date()) and
            '09:00:00' <= local.strftime('%H:%M:%S') < '13:30:00')


def next_open(at):
    local = at.astimezone(TAIPEI)
    candidate = local.replace(hour=9, minute=0, second=0, microsecond=0)
    if local >= candidate:
        candidate += timedelta(days=1)
    while twse_closed_reason(candidate.date()):
        candidate += timedelta(days=1)
    return candidate.isoformat()


def future_sessions(at):
    # The project has an explicitly sourced 2026 exchange calendar only.
    # Do not manufacture a next-year settlement date from weekdays alone.
    if at.year != 2026:
        return []
    return [(at.date()+timedelta(days=i)).isoformat() for i in range(1, 15)
            if (at.date()+timedelta(days=i)).year == 2026
            and not twse_closed_reason(at.date()+timedelta(days=i))]


class BrokerQuotes:
    """One STA owner and one read-only monitor for the fixed <=20 stock set."""
    def __init__(self, audit_path):
        self.audit_path = Path(audit_path)
        self.market = None
        self.com_initialized = False

    def fetch(self, symbols):
        from capital_multi_quote import MultiCapitalMarket, multi_snapshot
        if not 1 <= len(symbols) <= 20:
            raise ValueError('PAPER_QUOTE_CAPACITY_20')
        if self.market is None:
            load_env()
            import comtypes
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
            self.com_initialized = True
            self.market = MultiCapitalMarket(Audit(self.audit_path))
        self.market.connect()
        # Intentionally keep this monitor subscribed to the same fixed group.
        # No repeated login/LeaveMonitor on a 30-second polling loop.
        started = now().isoformat()
        raw, errors = self.market._fetch_one_batch(tuple(symbols), timeout=12, batch_index=1, batch_count=1)
        completed = now().isoformat()
        batch = multi_snapshot(symbols, raw, errors, started, completed)
        for q in batch['quotes']:
            item = raw.get(q['symbol'], {})
            for key in ('bid_size', 'ask_size', 'lot_type', 'book_size_unit'):
                q[key] = item.get(key)
        return batch

    def close(self):
        if self.market:
            self.market.close()
            self.market = None
        if self.com_initialized:
            import comtypes
            comtypes.CoUninitialize()
            self.com_initialized = False


class PaperRuntime:
    def __init__(self, *, root=ROOT, config_path=None, quote_reader=None, history_reader=None, stop_requested=lambda:False):
        self.root = Path(root).resolve()
        cfg = json.loads(Path(config_path or CODE_ROOT/'config/planned_paper.json').read_text(encoding='utf-8-sig'))
        if cfg.get('mode') != 'PAPER_ONLY':
            raise ValueError('ONLY_PAPER_MODE_ALLOWED')
        self.quote_interval = int(cfg['quote_interval_seconds'])
        self.ranking_interval = int(cfg['ranking_interval_seconds'])
        self.history_bars = int(cfg['history_bars'])
        if not 15 <= self.quote_interval <= 300 or not self.quote_interval <= self.ranking_interval <= 3600:
            raise ValueError('INVALID_SCAN_INTERVALS')
        if not 61 <= self.history_bars <= 250:
            raise ValueError('INVALID_HISTORY_WINDOW')
        self.directory = (self.root/cfg['experiment_directory']).resolve()
        if not self.directory.is_relative_to((self.root/'data/planned_paper').resolve()):
            raise ValueError('INDEPENDENT_PAPER_DIRECTORY_REQUIRED')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory/'runtime.json'
        self.run_id = uuid.uuid4().hex
        self.auto_enabled = False
        self.in_flight = False
        self.stop_requested = stop_requested
        self.data, self.history_day = {}, None
        self.last_rank = None
        self.last_history_attempt = None
        self.policies = [Policy('A'), Policy('B', target=.06, stop=.03, max_days=20)]
        self.books = {p.strategy: LimitOrderBook(self.directory/(p.strategy+'.sqlite3'), p,
                      initial=cfg['initial_twd_per_strategy']) for p in self.policies}
        universe = json.loads((self.root/'config/universe_tw.json').read_text(encoding='utf-8-sig'))
        self.symbols = [s for s in universe['symbols'] if len(s) == 4 and s.isdigit() and not s.startswith('00')]
        if not 1 <= len(self.symbols) <= 20:
            raise ValueError('INITIAL_FIXED_UNIVERSE_MUST_BE_1_TO_20_STOCKS')
        self.provider = BrokerQuotes(self.directory/'broker') if quote_reader is None else None
        self.quote_reader = quote_reader or self.provider.fetch
        self.history_reader = history_reader
        self.state = dict(mode='PAPER_ONLY', branch='codex/planned-limit-orders',
            live_enabled=False, llm_calls=0, run_id=self.run_id, pid=__import__('os').getpid(),
            experiment_directory=str(self.directory), symbol_count=len(self.symbols),
            quote_interval_seconds=self.quote_interval, ranking_interval_seconds=self.ranking_interval,
            status='STOPPED', auto_enabled=False, market_data_status='NOT_CHECKED',
            cycles=0, last_scan_at=None, errors=[], accounts=self.accounts())
        sources = ('capital.py', 'daily_history.py', 'quant_research/paper_runtime.py',
                   'quant_research/paper_dashboard.py', 'quant_research/planned_cycle.py',
                   'quant_research/limit_orders.py', 'quant_research/core.py', 'execution/risk_guard.py')
        self.state['implementation_sha256'] = hashlib.sha256(b''.join((CODE_ROOT/f).read_bytes() for f in sources)).hexdigest()
        try:
            self.state['git_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                cwd=CODE_ROOT, text=True, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip()
        except (OSError, subprocess.CalledProcessError):
            self.state['git_commit'] = 'UNKNOWN'
        self.publish()

    def accounts(self):
        return {name: book.snapshot() for name, book in self.books.items()}

    def publish(self):
        self.state.update(auto_enabled=self.auto_enabled, updated_at=now().isoformat())
        save_json(self.state_path, self.state)
        return dict(self.state)

    def prepare(self, at=None):
        at = at or now()
        self.state['status'] = 'PREPARING_HISTORY'
        self.publish()
        if self.history_reader:
            history = self.history_reader(self.symbols, at.date())
        else:
            import daily_history
            history = daily_history.load_many(self.symbols, at.date(), self.root/'data/analysis/daily_history',
                max_bars=self.history_bars, require_turnover=True)
        self.data = {s: item['bars'] for s, item in history.items()}
        self.history_day = at.date()
        self.last_history_attempt = at
        self.state['history'] = {s: item.get('status', {}) for s, item in history.items()}
        self.state['history_bar_counts'] = {s: len(b) for s, b in self.data.items()}
        self.state['status'] = 'WAIT_SESSION' if not market_open(at) else 'READY'
        self.state['next_market_open_taipei'] = next_open(at)
        return self.publish()

    def stale_symbols(self):
        return [symbol for symbol, s in self.state.get('history', {}).items()
                if not s.get('latest_date') or s.get('latest_date', '') < s.get('expected_latest_date', '')]

    def refresh_latest(self, at):
        import daily_history
        symbols = self.stale_symbols()
        self.last_history_attempt = at
        updates = (self.history_reader(symbols, at.date()) if self.history_reader else
                   daily_history.load_many(symbols, at.date(), self.root/'data/analysis/daily_history',
                                           max_bars=61, require_turnover=True))
        for symbol, item in updates.items():
            by_date = {b['date']: b for b in self.data.get(symbol, [])}
            by_date.update({b['date']: b for b in item['bars']})
            self.data[symbol] = [by_date[d] for d in sorted(by_date)[-self.history_bars:]]
            self.state['history'][symbol] = item['status']
        self.state['history_bar_counts'] = {s: len(rows) for s, rows in self.data.items()}
        self.last_rank = None

    def set_auto(self, enabled):
        self.auto_enabled = bool(enabled)
        self.state['status'] = 'AUTO_ENABLED' if enabled else 'STOPPED'
        return self.publish()

    def step(self, at=None):
        at = at or now()
        if not self.auto_enabled:
            return self.publish()
        if self.in_flight:
            raise ValueError('SCAN_ALREADY_RUNNING')
        self.in_flight = True
        try:
            if not market_open(at):
                if self.provider:
                    self.provider.close()
                # Expire Paper ROD remainders and settle due receivables while
                # keeping broker connections off during weekends/after hours.
                for p in self.policies:
                    self.books[p.strategy].process(self.run_id+':closed:'+at.isoformat(), at.isoformat(), {},
                                                  sessions=future_sessions(at))
                self.state.update(status='WAIT_SESSION', market_data_status='MARKET_CLOSED',
                                  next_market_open_taipei=next_open(at), accounts=self.accounts())
                return self.publish()
            if self.history_day != at.date():
                self.prepare(at)
            if self.stale_symbols() and (self.last_history_attempt is None or
                    (at-self.last_history_attempt).total_seconds() >= 900):
                self.refresh_latest(at)
            batch = self.quote_reader(self.symbols)
            if self.stop_requested():
                self.auto_enabled = False
                self.state['status'] = 'STOPPED'
                return self.publish()
            # Timestamp after the broker response; never turn retrieval time
            # into a fabricated exchange timestamp.
            completed = now() if self.provider is not None else at
            quotes = batch['quotes']
            live = []
            for q in quotes:
                try:
                    self.books['A']._quote(q['symbol'], {q['symbol']: q}, completed.isoformat())
                    live.append(q)
                except (ValueError, KeyError, TypeError, ArithmeticError):
                    pass
            save_json(self.directory/'latest_quotes.json', batch)
            rank_due = self.last_rank is None or (completed-self.last_rank).total_seconds() >= self.ranking_interval
            sessions = sorted({b['date'] for bars in self.data.values() for b in bars} | set(future_sessions(completed)))
            bundle = dict(at=completed.isoformat(), tick_id=self.run_id+':'+completed.isoformat(),
                          quotes=quotes, sessions=sessions)
            summary = {}
            for policy in self.policies:
                result = cycle(self.directory/(policy.strategy+'.sqlite3'), self.data if rank_due else {}, bundle, policy)
                save_json(self.directory/(policy.strategy+'_latest.json'), result)
                reasons = {}
                for c in result['candidates']:
                    reason = c.get('reason', 'UNKNOWN')
                    reasons[reason] = reasons.get(reason, 0)+1
                summary[policy.strategy] = dict(plans=len(result['plans']), fills=len(result['fills']),
                    rejected=result['rejected'], funnel=reasons, ranking_updated=rank_due)
            if rank_due:
                self.last_rank = completed
                self.state['last_ranking_summary'] = summary
            self.state.update(status='RUNNING' if live else 'WAIT_VALID_QUOTES',
                market_data_status='LIVE' if live else 'NO_FRESH_TRADABLE_QUOTES',
                last_scan_at=completed.isoformat(), live_quote_count=len(live),
                cycles=self.state['cycles']+1, summary=summary, accounts=self.accounts(), errors=[])
        except Exception as exc:
            self.state.update(status='ERROR_RETRY_PENDING', errors=[redact(f'{type(exc).__name__}: {exc}')])
        finally:
            self.in_flight = False
        return self.publish()

    def close(self):
        self.auto_enabled = False
        if self.provider:
            self.provider.close()
        self.state['status'] = 'CLOSED'
        self.publish()
