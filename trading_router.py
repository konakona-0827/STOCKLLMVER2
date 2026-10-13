"""Route the current LLM/Agent decision mode through the same account and executor."""
import argparse
import json
import os
from pathlib import Path

from config import ROOT, load_env, trading_mode
import paper_portfolio


def main():
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description='使用目前帳戶與執行引擎，切換 LLM / Agent 決策模式')
    parser.add_argument('--mode', choices=('llm','agent'))
    parser.add_argument('--trigger', choices=('MANUAL','AUTO'), default='MANUAL')
    parser.add_argument('--data-dir', type=Path, default=ROOT/'data'/'analysis')
    parser.add_argument('--capital', type=str, help='設定／增加資金總上限（TWD）；承接既有帳戶時不用指定')
    parser.add_argument('--read-only', action='store_true', help='Agent 連線測試：允許盤外查詢工具，禁止成交')
    args = parser.parse_args()
    load_env()
    if args.mode:
        os.environ['TRADING_MODE'] = args.mode
    mode = trading_mode()
    if mode != 'agent' and args.read_only:
        parser.error('--read-only only applies to Agent mode')
    from analysis_service import AnalysisService
    service = AnalysisService(args.data_dir)
    if args.capital:
        paper_portfolio.set_capital_limit(service.db_path, args.capital)
    try:
        result = service.run(args.trigger, read_only=args.read_only)
        print(json.dumps(dict(mode=mode, manifest=result['manifest'],
                              execution=result.get('execution_report')), ensure_ascii=False, indent=2))
        return 1 if result['manifest']['status']=='ERROR' else 0
    except Exception as exc:
        from config import redact
        print(redact(f'{type(exc).__name__}: {exc}'))
        return 1
    finally:
        from execution.broker_runtime import shutdown_broker_runtime
        shutdown_broker_runtime(ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
