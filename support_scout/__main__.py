from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contracts import DataError, Rules, strict_json_loads, digest
from .demo import demo_bundle
from .desk import DeskConfig, run_desk
from .pipeline import render_markdown, run
from .providers import OfflineProvider, OpenAIProvider
from .settings import DEFAULT_SETTINGS, doctor


def read_json(path: Path):
    return strict_json_loads(path.read_text(encoding="utf-8-sig"))


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Support Desk：台股日線 → 支撐掃描 → 獨立雙 agent → 研究名單")
    sub = parser.add_subparsers(dest="command", required=True)
    health = sub.add_parser("doctor", help="檢查本地儲存與OpenAI設定，不呼叫模型")
    health.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    health.add_argument("--require-agents", action="store_true")
    daily = sub.add_parser("observe", help="官方台股收盤資料 → 每日觀察報告 → 固定支撐跨日追蹤")
    daily.add_argument("--symbols", help="逗號分隔的上市普通股，省略用固定19檔觀察池")
    daily.add_argument("--months", type=int, default=8)
    daily.add_argument("--timeout", type=float, default=15)
    daily.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    daily.add_argument("--model")
    daily.add_argument("--max-packets", type=int, default=5)
    daily.add_argument("--max-calls", type=int, default=10)
    daily.add_argument("--out", type=Path, help="觀察報告根目錄，每輪使用唯一子目錄")
    daily.add_argument("--data-root", type=Path, help="本地快取與SQLite觀察帳本資料夾")
    choices = daily.add_mutually_exclusive_group()
    choices.add_argument("--agents", action="store_true", dest="agents")
    choices.add_argument("--no-agents", action="store_false", dest="agents")
    daily.set_defaults(agents=None)
    demo = sub.add_parser("demo", help="合成資料離線示範；不呼叫模型或行情 API")
    scan = sub.add_parser("scan", help="掃描符合合約的 JSON 日線快照")
    scan.add_argument("input", type=Path)
    scan.add_argument("--as-of", help="含時區的截止時間；回放須另有當時可見快照")
    scan.add_argument("--reviews", type=Path, help="已完成的雙 agent 審查 JSON，格式 {reviews:[...]}")
    scan.add_argument("--rules", type=Path, help="Rules 欄位覆寫 JSON；實際全套參數保存於報告")
    desk = sub.add_parser("desk", help="Head 分派兩個獨立研究回合；預設離線示範")
    desk.add_argument("input", type=Path)
    desk.add_argument("--as-of")
    desk.add_argument("--rules", type=Path)
    desk.add_argument("--provider", choices=("offline", "openai"), default="offline")
    desk.add_argument("--model", help="OpenAI 模型 ID，必須明確指定")
    desk.add_argument("--reviewer-model", help="反證方模型 ID；省略沿用 --model，各自使用獨立 context")
    desk.add_argument("--timeout", type=float, default=45, help="每次 API 的 I/O timeout 秒數，1–60")
    desk.add_argument("--max-output-tokens", type=int, default=2000)
    desk_demo = sub.add_parser("desk-demo", help="合成資料＋離線代理，驗證分派；永遠不升為 RESEARCH")
    for command in (desk, desk_demo):
        command.add_argument("--max-packets", type=int, default=5)
        command.add_argument("--max-calls", type=int, default=10)
    for command in (demo, scan, desk, desk_demo):
        command.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor(args.settings)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["observation_ready"] and (not args.require_agents or result["openai_ready"]) else 2
        if args.command == "observe":
            from .observation import observe
            def progress(url):
                print("讀取公開資料：" + url, flush=True)
            report, target, code = observe(symbols=args.symbols.split(",") if args.symbols else None,
                months=args.months, timeout=args.timeout, output_root=args.out,
                cache_dir=args.data_root / "twse-cache" if args.data_root else None,
                db_path=args.data_root / "observations.sqlite3" if args.data_root else None,
                settings_path=args.settings, agents=args.agents, model=args.model,
                max_packets=args.max_packets, max_calls=args.max_calls, progress=progress)
            print(render_markdown(report))
            print("觀察狀態：" + report["observation"]["status"] + "；模型：" + report["observation"]["agent_status"])
            print("報告：" + str((target / "report.html").resolve()))
            return code
        bundle = demo_bundle() if args.command in {"demo", "desk-demo"} else read_json(args.input)
        rules = Rules(**read_json(args.rules)) if getattr(args, "rules", None) else Rules()
        reviews = read_json(args.reviews)["reviews"] if args.command == "scan" and args.reviews else None
        if args.command in {"desk", "desk-demo"}:
            researcher = reviewer = OfflineProvider()
            if getattr(args, "provider", None) == "openai":
                researcher = OpenAIProvider(args.model, timeout=args.timeout, max_output_tokens=args.max_output_tokens)
                reviewer = OpenAIProvider(args.reviewer_model or args.model,
                    timeout=args.timeout, max_output_tokens=args.max_output_tokens)
            report = run_desk(bundle, researcher=researcher, reviewer=reviewer, rules=rules,
                as_of=getattr(args, "as_of", None), config=DeskConfig(args.max_packets, args.max_calls))
        else:
            report = run(bundle, as_of=getattr(args, "as_of", None), rules=rules, reviews=reviews)
        if args.out is None:
            args.out = Path("output/desk") / report["desk"]["run_id"] if "desk" in report else Path("output/latest")
        args.out.mkdir(parents=True, exist_ok=True)
        for filename, value in (("report.json", report), ("agent_tasks.json", report["agent_packets"])):
            (args.out / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        (args.out / "report.md").write_text(render_markdown(report), encoding="utf-8")
        if "desk" in report:
            for filename, value in (("reviews.json", {"reviews": report["reviews"]}), ("desk_audit.json", report["desk"])):
                (args.out / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        # Persist exactly the input for replay (including explicit synthetic provenance).
        (args.out / "input.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        effective = {**bundle, "as_of": report["as_of"]}
        for filename, value in (("effective_input.json", effective), ("rules.json", report["rules"]),
            ("manifest.json", {"effective_as_of": report["as_of"], "rules": report["rules"],
                "input_digest": digest(bundle), "snapshot_id": report["snapshot_id"],
                "universe_version": report["universe_version"]})):
            (args.out / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(render_markdown(report))
        print("輸出：" + str(args.out.resolve()))
        if all(s["data_status"] == "REJECTED" for s in report["securities"]):
            return 2
        return 3 if report.get("desk", {}).get("error_count") else 0
    except (DataError, OSError, KeyError, TypeError, ValueError) as exc:
        print("研究未執行：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
