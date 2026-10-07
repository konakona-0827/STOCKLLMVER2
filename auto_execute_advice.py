from __future__ import annotations

import argparse
import json
from pathlib import Path

from execution.pipeline_hook import execute_after_advice_written


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute a stable advice_interface JSON through the REAL automatic pipeline."
    )
    parser.add_argument(
        "--advice",
        default=r"data\analysis\advice_latest.json",
    )
    parser.add_argument(
        "--config",
        default=r"config\execution_live.json",
    )
    parser.add_argument("--env")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    advice = Path(args.advice)
    if not advice.is_absolute():
        advice = root / advice
    config = Path(args.config)
    if not config.is_absolute():
        config = root / config

    report = execute_after_advice_written(
        advice,
        project_root=root,
        config_path=config,
        env_path=args.env,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
