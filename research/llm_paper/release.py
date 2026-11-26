"""Export credential-free, immutable runtime code, separate from production."""
from pathlib import Path

from .common import ROOT, digest, save, read,PRODUCTION_FILES


def export(output):
    out=Path(output).resolve()
    if not out.is_relative_to(ROOT/"research-runs"):
        raise ValueError("ISOLATED_RELEASE_REQUIRED")
    # A fresh checkout shares the existing account state by binding; it does
    # not contain a copied data/ tree or a sibling transport checkout.
    from runtime_paths import resolve_state_root
    state_root=resolve_state_root(ROOT)
    selection=read(state_root/"data/analysis/live_strategy.json")
    selected=(ROOT/selection["path"]).resolve()
    if not selected.is_relative_to(ROOT):
        raise ValueError("STRATEGY_OUTSIDE_PROJECT")
    # A paper runtime needs no copied live entry points or order test scripts.
    files=[ROOT/name for name in PRODUCTION_FILES]
    for folder in ("research/llm_paper","research/trading_skills","skills/tw-paper-decision","skills/tw-trading-decision"):
        files += [p for p in (ROOT/folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    files += [ROOT/"research/__init__.py",ROOT/"config/universe_tw.json",ROOT/"config/scan_rules.json",ROOT/"config/opportunity_search.json",selected]
    manifest={p.relative_to(ROOT).as_posix():digest(p.read_bytes()) for p in files}
    transport=ROOT/"codex_cli.py"
    manifest["codex_cli.py"]=digest(transport.read_bytes())
    version=digest(manifest)[:20]
    out.mkdir(parents=True,exist_ok=False)
    for p in files:
        target=out/p.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True)
        with target.open("xb") as f:f.write(p.read_bytes())
    (out/"codex_cli.py").write_bytes(transport.read_bytes())
    record=dict(version=version,source_manifest=manifest,strategy=selected.relative_to(ROOT).as_posix(),
                parent_project=str(ROOT),credentials_copied=False,execution_package_copied=False)
    save(out/"release.json",record)
    return record
