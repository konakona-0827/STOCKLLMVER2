from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .data import download, export_adapters, read_json, write_json
from .experiment import DEFAULT_PROTOCOL, evaluate, snapshot


def main():
    parser = argparse.ArgumentParser(description="台股離線選股實驗；獨立資料與產出，不載入正式交易設定")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("download", help="明確下載 FinMind 公開資料；可讀取程序的 FINMIND_TOKEN")
    fetch.add_argument("--start", required=True)
    fetch.add_argument("--end", required=True)
    fetch.add_argument("--symbols", nargs="+")
    fetch.add_argument("--output", required=True)
    for name in ("export", "evaluate", "snapshot", "qlib-check", "backtesting-check"):
        command = sub.add_parser(name)
        command.add_argument("--dataset", required=True)
        command.add_argument("--output", required=True)
        if name == "snapshot":
            command.add_argument("--as-of", required=True)
            command.add_argument("--scores-folder")
    review = sub.add_parser("review", help="透過明確指定的既有 Codex CLI transport 做 2×2 研究，不呼叫 API")
    review.add_argument("--packet", required=True)
    review.add_argument("--transport-file", required=True)
    review.add_argument("--model", default="gpt-6-sol")
    review.add_argument("--output", required=True)
    review.add_argument("--timeout", type=int, default=180)
    review.add_argument("--rankings", nargs="+", default=["momentum", "composite"])
    costs = sub.add_parser("costs", help="依已保存 CLI 用量試算 API 等值預算；不呼叫模型")
    costs.add_argument("--pilot", required=True)
    costs.add_argument("--output", required=True)
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    args = parser.parse_args()
    protocol = read_json(args.protocol)
    if args.command == "download":
        result = download(args.output, args.symbols or protocol["symbols"], args.start, args.end)
        result = {k: result[k] for k in ("rows", "rejected_rows", "symbols", "start", "end")}
    elif args.command == "export":
        result = dict(output=str(export_adapters(args.dataset, args.output)))
    elif args.command == "snapshot":
        packet = snapshot(args.dataset, args.as_of, protocol, args.scores_folder)
        path = Path(args.output)
        # Immutable packet capture; explicit review never silently refreshes it.
        with path.open("x", encoding="utf-8") as stream:
            json.dump(packet, stream, ensure_ascii=False, indent=2, allow_nan=False)
        result = dict(packet_id=packet["packet_id"], eligible=len(packet["eligible_candidates"]), excluded=packet["excluded"])
    elif args.command == "evaluate":
        result = evaluate(args.dataset, args.output, protocol)
    elif args.command in ("qlib-check", "backtesting-check"):
        from .external import qlib_check, backtesting_check
        fn = qlib_check if args.command == "qlib-check" else backtesting_check
        result = fn(args.dataset, args.output, protocol)
    elif args.command == "costs":
        from .costs import cost_report
        result = cost_report(args.pilot, args.output)
    else:
        from .review import review_factorial
        result = review_factorial(args.packet, args.transport_file, args.output, args.model, args.timeout, args.rankings)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    if args.command == "review" and result["valid_calls"] != 4:
        raise SystemExit(2)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
