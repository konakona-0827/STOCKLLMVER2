"""Local, explicitly selected configuration. Keys never enter persisted reports."""
from __future__ import annotations

import os
from pathlib import Path

from .contracts import DataError, strict_json_loads

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS = ROOT / "local_settings.json"


def load_settings(path: Path = DEFAULT_SETTINGS) -> dict:
    if not path.exists():
        return {}
    value = strict_json_loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or set(value) - {"openai_env_file", "model", "reviewer_model", "run_agents"}:
        raise DataError("INVALID_LOCAL_SETTINGS")
    for key in ("openai_env_file", "model", "reviewer_model"):
        if key in value and (not isinstance(value[key], str) or not value[key].strip()):
            raise DataError("INVALID_LOCAL_SETTINGS:" + key)
    if "run_agents" in value and type(value["run_agents"]) is not bool:
        raise DataError("INVALID_LOCAL_SETTINGS:run_agents")
    return value


def openai_config(settings: dict, *, model: str | None = None) -> dict:
    """Only these OpenAI fields are read from the user-authorized .env path."""
    inherited = {}
    if settings.get("openai_env_file"):
        source = Path(settings["openai_env_file"]).expanduser().resolve()
        pending_key = False
        for line in source.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if pending_key and line.startswith("sk-") and "=" not in line:
                inherited["OPENAI_API_KEY"] = line
                pending_key = False
                continue
            pending_key = False
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            key = key.strip()
            if key not in {"OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_AGENT_MODEL"}:
                continue
            value = value.strip()
            pending_key = key == "OPENAI_API_KEY" and not value
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            inherited[key] = value
    api_key = os.environ.get("OPENAI_API_KEY") or inherited.get("OPENAI_API_KEY")
    chosen = model or settings.get("model") or os.environ.get("SUPPORT_DESK_MODEL") or os.environ.get("OPENAI_MODEL") or inherited.get("OPENAI_AGENT_MODEL") or inherited.get("OPENAI_MODEL")
    # The existing provider reads the key only from the process environment.
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key
    return {"model": chosen, "reviewer_model": settings.get("reviewer_model") or chosen,
            "key_configured": bool(api_key), "ready": bool(api_key and chosen)}


def doctor(settings_path: Path = DEFAULT_SETTINGS) -> dict:
    import sqlite3
    import sys
    import tempfile
    from contextlib import closing
    settings = load_settings(settings_path)
    errors = []
    try:
        config = openai_config(settings)
    except (OSError, ValueError):
        config = {"ready": False, "key_configured": False, "model": None, "reviewer_model": None}
        errors.append("AUTHORIZED_OPENAI_SETTINGS_UNREADABLE")
    try:
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            with closing(sqlite3.connect(str(Path(folder) / "check.sqlite3"))) as db:
                db.execute("CREATE TABLE health (id INTEGER)")
    except (OSError, sqlite3.Error):
        errors.append("LOCAL_STORAGE_UNWRITABLE")
    return {"python": sys.version.split()[0], "observation_ready": sys.version_info >= (3, 11) and not errors,
        "openai_ready": config["ready"], "key_configured": config["key_configured"],
        "model": config["model"], "reviewer_model": config["reviewer_model"],
        "errors": errors, "execution_enabled": False,
        "scope": "CONFIGURABLE_FIXED_POOL_THREE_AGENT_DECISION",
        "note": "預設20檔研究池與尋標／研究／反證三角色；doctor只查設定，不呼叫模型或券商。"}
