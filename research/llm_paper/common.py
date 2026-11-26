from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path

from research.trading_skills.market import Market, Costs, decimal, instant

ROOT = Path(__file__).resolve().parents[2]
ARMS = ("formal_baseline", "formal_with_skill")
PRODUCTION_FILES=("position_analyzer.py","candidate_analyzer.py","pretrade_policy.py","trade_economics.py",
    "paper_portfolio.py","market_calendar.py","strategy_evidence.py","config.py","web_research.py",
    "quant_scanner.py","daily_history.py","capital_check.py","runtime_paths.py","state_lock.py","bounded_cli_capture.py",
    "opportunity_engine.py","entry_structure.py")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def output_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to(ROOT / "research-runs"):
        raise ValueError("ISOLATED_RESEARCH_OUTPUT_REQUIRED")
    return path


def protocol(path=None):
    p = read(path or Path(__file__).with_name("protocol.json"))
    if p["mode"] != "PAPER_ONLY" or any(decimal(p[k]) <= 0 for k in ("initial_cash_twd", "max_daily_buy_twd")):
        raise ValueError("PAPER_PROTOCOL_REQUIRED")
    for k in ("max_positions", "max_order_quantity", "maximum_model_calls_replay", "maximum_model_calls_per_forward_day"):
        if type(p[k]) is not int or p[k] <= 0:
            raise ValueError("INVALID_PROTOCOL")
    if p.get('funding_mode')=='LATEST_BRANCH_CUMULATIVE':
        if any(decimal(p[k])<0 for k in ('strategy_cap_twd','min_available_to_buy_twd','buy_cash_buffer_rate','min_buy_fee_reserve_twd','max_order_twd')):
            raise ValueError('INVALID_FUNDING_PROFILE')
        if (decimal(p['strategy_cap_twd'])<=0 or decimal(p['max_order_twd'])<=0 or decimal(p['buy_cash_buffer_rate'])<decimal(p['commission_rate'])
            or decimal(p['min_buy_fee_reserve_twd'])<decimal(p['minimum_commission_twd'])):
            raise ValueError('INVALID_FUNDING_PROFILE')
    return p


def costs(p, instrument="STOCK"):
    return Costs(dict(commission_rate=p["commission_rate"], minimum_commission_twd=p["minimum_commission_twd"],
                      sell_tax_rate=p["sell_tax_etf"] if instrument == "ETF" else p["sell_tax_stock"], slippage_bps=p["slippage_bps"]))


def round_price(price, day, instrument="STOCK", up=False):
    price = decimal(price)
    if instrument != "ETF":
        return Market().round_price(price, day, up)
    # Domestic equity ETF tick rule, not the common-stock price bands.
    step = Decimal("0.01") if price < 50 else Decimal("0.05")
    return (price / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def fill_price(price, side, day, p, instrument="STOCK"):
    slip = decimal(p["slippage_bps"]) / 10000
    return round_price(decimal(price) * (1 + slip if side == "BUY" else 1 - slip), day, instrument, side == "BUY")


def freeze_sources(output, strategy):
    output = Path(output)
    paths = list(Path(__file__).parent.glob("*")) + [ROOT/name for name in PRODUCTION_FILES] + [
        ROOT / name for name in ("config/universe_tw.json", "config/scan_rules.json","config/opportunity_search.json","codex_cli.py",
                                "research/trading_skills/market.py", "research/trading_skills/market_rules.json",
                                "skills/tw-paper-decision/SKILL.md", "skills/tw-trading-decision/SKILL.md")]
    paths.append(Path(strategy))
    manifest = {}
    for path in paths:
        if not path.is_file():
            continue
        name = path.resolve().relative_to(ROOT).as_posix()
        raw = path.read_bytes()
        dest = output / "source" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as f:
            f.write(raw)
        manifest[name] = digest(raw)
    save(output / "source_manifest.json", manifest)
    return manifest


def verify_sources(manifest):
    if any(not (ROOT / name).is_file() or digest((ROOT / name).read_bytes()) != sha for name, sha in manifest.items()):
        raise ValueError("SOURCE_VERSION_CHANGED")
