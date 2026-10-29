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
    parser = argparse.ArgumentParser(description="Japan Support Desk：Yahoo 日線 → 尋標／研究／反證三Agent → 人工決策（無下單）")
    sub = parser.add_subparsers(dest="command", required=True)
    health = sub.add_parser("doctor", help="檢查本地儲存與OpenAI設定，不呼叫模型")
    health.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    health.add_argument("--require-agents", action="store_true")
    local = sub.add_parser("serve", help="本機頁面：手動成交／持倉及固定名單分析，沒有下單能力")
    local.add_argument("--port", type=int, default=8765)
    mail = sub.add_parser("notify-configure", help="設定同一Gmail寄件／收件地址；不接受命令列密碼")
    mail.add_argument("email")
    mail.add_argument("--env-file", type=Path, help="只讀明確指定ENV中的DEVICEPW，不複製整份ENV")
    sub.add_parser("notify-status", help="通知佇列與寄信設定狀態，不輸出憑證")
    sub.add_parser("notify-flush", help="寄送待寄決策事件，未設定Gmail憑證則保留佇列")
    real_case = sub.add_parser("advantest-case", help="真實Advantest近兩週前綴重算，含歷史時點限制")
    real_case.add_argument("--agents", action="store_true")
    real_case.add_argument("--diagnostic-review", action="store_true", help="門檻未通過時仍做雙方診斷，不冒稱尋標命中")
    real_case.add_argument("--active-manual", action=argparse.BooleanOptionalAction, default=True,
        help="啟用獨立H-002積極人工機會評估，保留原正式交易資格")
    real_case.add_argument("--checkpoints", help="例如d20261026,d20261028_after_ir,d20261029")
    real_case.add_argument("--max-total-calls", type=int, default=6)
    real_case.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    real_case.add_argument("--out", type=Path)
    real_replay = sub.add_parser("advantest-case-replay", help="離線核對真實案例保存結果")
    real_replay.add_argument("directory", type=Path)
    case = sub.add_parser("pattern-case", help="H-001 Advantest合成參數截點驗證；與真實行情和帳本隔離")
    case.add_argument("--case", type=Path, help="合成參數紀錄JSON；省略用Advantest H-001案例")
    case.add_argument("--agents", action="store_true", help="實際呼叫尋標、研究與反證模型")
    case.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    case.add_argument("--checkpoints", help="逗號分隔截點ID，例如early,support,catalyst")
    case.add_argument("--max-total-calls", type=int, default=12)
    case.add_argument("--out", type=Path)
    case_replay = sub.add_parser("pattern-case-replay", help="完全離線重播合成參數與已保存的AI裁決")
    case_replay.add_argument("directory", type=Path)
    daily = sub.add_parser("observe", help="Yahoo 日股收盤資料 → 每日決策報告 → 固定支撐跨日追蹤")
    daily.add_argument("--market", choices=("JP", "TW"), default="JP", help="預設日本；TW 保留原版回歸入口")
    daily.add_argument("--symbols", help="手動診斷名單，例如6857,8035；省略時由SEARCH在固定可配置研究池選股")
    sourcing = daily.add_mutually_exclusive_group()
    sourcing.add_argument("--discover", action="store_true", dest="discover", help="可配置研究池 → SEARCH → 研究／反證")
    sourcing.add_argument("--fixed-universe", action="store_true", dest="discover", help="固定可配置母池內仍由SEARCH選股")
    daily.set_defaults(discover=None)
    daily.add_argument("--scan-limit", type=int, default=20, help="預設固定20檔；未來母池可調，最多100")
    daily.add_argument("--universe", type=Path, help="可替換的固定母池JSON，日後可放PayPay可交易名單")
    daily.add_argument("--universe-source", choices=("fixed", "jpx"), default="fixed", help="預設固定母池，JPX廣域輪巡需明確指定")
    daily.add_argument("--months", type=int, default=8)
    daily.add_argument("--timeout", type=float, default=15)
    daily.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    daily.add_argument("--model")
    daily.add_argument("--max-packets", type=int, default=5)
    daily.add_argument("--max-calls", type=int, default=10, help="整輪模型呼叫上限，包含 SEARCH 與雙方研究")
    daily.add_argument("--public-information", action=argparse.BooleanOptionalAction, default=True,
        help="日股實際觀察預設檢索財報與新聞；shared_once共用來源事實，工具額度見config/public_information.json")
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
    japan_demo = sub.add_parser("japan-demo", help="日股合成情境＋雙離線代理＋決策報告，不呼叫外部 API")
    for command in (desk, desk_demo, japan_demo):
        command.add_argument("--max-packets", type=int, default=5)
        command.add_argument("--max-calls", type=int, default=10)
    for command in (demo, scan, desk, desk_demo, japan_demo):
        command.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .dashboard import serve
            serve(args.port)
            return 0
        if args.command in {"notify-configure", "notify-status", "notify-flush"}:
            from .notifications import configure, email_status, flush
            if args.command == "notify-configure":
                configure(args.email, secret_env_file=args.env_file)
            result = flush() if args.command == "notify-flush" else email_status()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "advantest-case-replay":
            from .advantest_case import replay_real_case
            print(json.dumps(replay_real_case(args.directory), ensure_ascii=False, indent=2))
            return 0
        if args.command == "advantest-case":
            from .advantest_case import run_real_case
            from .opportunity import load_policy as load_opportunity
            result, target = run_real_case(agents=args.agents, diagnostic_review=args.diagnostic_review,
                checkpoint_ids=args.checkpoints.split(",") if args.checkpoints else None,
                max_total_calls=args.max_total_calls, settings_path=args.settings, output_root=args.out,
                opportunity_policy=load_opportunity() if args.active_manual else None)
            print(json.dumps({"model_call_count": result["model_call_count"],
                "results": [{**{k: r.get(k) for k in ("checkpoint", "pattern_stage", "search_selected", "trade_conclusion", "status")},
                    "opportunity_action": (r.get("opportunity_decision") or {}).get("action")} for r in result["results"]]}, ensure_ascii=False, indent=2))
            print("真實案例報告：" + str((target / "comparison.html").resolve()))
            return 3 if any("DEGRADED" in r["status"] or "DEFERRED" in r["status"] or r["status"] == "SEARCH_FAILED" for r in result["results"]) else 0
        if args.command == "pattern-case-replay":
            from .pattern_case import replay_case
            print(json.dumps(replay_case(args.directory), ensure_ascii=False, indent=2))
            return 0
        if args.command == "pattern-case":
            from .pattern_case import DEFAULT_CASE, run_case
            comparison, target = run_case(case_path=args.case or DEFAULT_CASE, agents=args.agents,
                settings_path=args.settings, output_root=args.out,
                checkpoint_ids=args.checkpoints.split(",") if args.checkpoints else None,
                max_total_calls=args.max_total_calls)
            for result in comparison["results"]:
                print(result["checkpoint"] + "：" + ", ".join(s["label"] for s in result["stages"]) +
                      "；" + result["status"] + "；交易研究=" + result["trade_conclusion"])
            print("模型呼叫：" + str(comparison["model_call_count"]))
            print("合成案例比較：" + str((target / "comparison.html").resolve()))
            return 3 if any(r["status"] in {"DEGRADED", "SEARCH_FAILED", "BUDGET_DEFERRED"} for r in comparison["results"]) else 0
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
                cache_dir=args.data_root / ("yahoo-cache" if args.market == "JP" else "twse-cache") if args.data_root else None,
                db_path=args.data_root / "observations.sqlite3" if args.data_root else None,
                settings_path=args.settings, agents=args.agents, model=args.model,
                max_packets=args.max_packets, max_calls=args.max_calls, progress=progress, market=args.market,
                discover=args.discover, scan_limit=args.scan_limit,
                universe_path=args.universe, universe_source=args.universe_source,
                public_information=args.public_information if args.market == "JP" else None,
                cursor_path=args.data_root / "discovery_cursor.json" if args.data_root else None)
            print(render_markdown(report, include_decisions=False))
            if "decision_summary" in report:
                print("決策統計：" + json.dumps(report["decision_summary"]["counts"], ensure_ascii=False))
            if "opportunity_summary" in report:
                print("積極人工評估：" + json.dumps(report["opportunity_summary"]["counts"], ensure_ascii=False))
            print("觀察狀態：" + report["observation"]["status"] + "；模型：" + report["observation"]["agent_status"])
            print("報告：" + str((target / "report.html").resolve()))
            return code
        if args.command == "japan-demo":
            from .japan_demo import demo_bundle as japan_demo_bundle
            bundle = japan_demo_bundle()
        else:
            bundle = demo_bundle() if args.command in {"demo", "desk-demo"} else read_json(args.input)
        rules = Rules(**read_json(args.rules)) if getattr(args, "rules", None) else Rules()
        reviews = read_json(args.reviews)["reviews"] if args.command == "scan" and args.reviews else None
        if args.command in {"desk", "desk-demo", "japan-demo"}:
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
        if report.get("market") == "JP":
            from .reporting import render_html
            (args.out / "report.html").write_text(render_html(report, bundle), encoding="utf-8")
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
