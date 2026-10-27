from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import DataError, Rules
from .demo import demo_bundle
from .pipeline import render_markdown, run


def read_json(path: Path):
    def reject_constant(value):
        raise DataError("NON_FINITE_JSON_NUMBER:" + value)
    return json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="台股支撐候選研究：完整日線 → 固定規則 → agent 任務包／審查")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="合成資料離線示範；不呼叫模型或行情 API")
    scan = sub.add_parser("scan", help="掃描符合合約的 JSON 日線快照")
    scan.add_argument("input", type=Path)
    scan.add_argument("--as-of", help="含時區的截止時間；回放須另有當時可見快照")
    scan.add_argument("--reviews", type=Path, help="已完成的雙 agent 審查 JSON，格式 {reviews:[...]}")
    scan.add_argument("--rules", type=Path, help="Rules 欄位覆寫 JSON；實際全套參數保存於報告")
    for command in (demo, scan):
        command.add_argument("--out", type=Path, default=Path("output/latest"))
    args = parser.parse_args(argv)
    try:
        bundle = demo_bundle() if args.command == "demo" else read_json(args.input)
        rules = Rules(**read_json(args.rules)) if args.command == "scan" and args.rules else Rules()
        reviews = read_json(args.reviews)["reviews"] if args.command == "scan" and args.reviews else None
        report = run(bundle, as_of=getattr(args, "as_of", None), rules=rules, reviews=reviews)
        args.out.mkdir(parents=True, exist_ok=True)
        for filename, value in (("report.json", report), ("agent_tasks.json", report["agent_packets"])):
            (args.out / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (args.out / "report.md").write_text(render_markdown(report), encoding="utf-8")
        if args.command == "demo":
            (args.out / "input.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(render_markdown(report))
        print("輸出：" + str(args.out.resolve()))
        return 2 if all(s["data_status"] == "REJECTED" for s in report["securities"]) else 0
    except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
        print("研究未執行：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
