"""Read every tracked source/config/Skill/test; preserve hashes and test logs."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

from .experiment import digest, save, git_environment


def audit_repository(repo, output, run_tests=True):
    repo, out = Path(repo).resolve(), Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    git = lambda *args: subprocess.check_output(["git", "-C", str(repo), *args], text=True, env=git_environment()).strip()
    tracked = git("ls-files").splitlines()
    inventory, imports, source_read = [], set(), []
    for filename in tracked:
        path = repo / filename
        raw = path.read_bytes()  # Includes fixture bytes, not just README.
        record = dict(path=filename, bytes=len(raw), sha256=digest(raw))
        if path.suffix in (".py", ".json", ".md") and "fixtures" not in path.parts:
            source = raw.decode("utf-8-sig")
            record["lines"] = len(source.splitlines())
            record["urls"] = sorted(set(re.findall(r'https?://[^\s<>"\x27)]+', source)))
            source_read.append("\n\n===== " + filename + " =====\n" + source)
            if path.suffix == ".py":
                tree = ast.parse(source)
                found = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        found.update(alias.name.split(".")[0] for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        found.add(node.module.split(".")[0])
                imports.update(found)
                record["imports"] = sorted(found)
                record["functions"] = [dict(name=n.name, line=n.lineno, end_line=n.end_lineno,
                                             doc=ast.get_docstring(n), arguments=ast.unparse(n.args))
                                       for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                record["test_methods"] = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
                record["effects"] = [dict(line=n.lineno, call=ast.unparse(n.func)) for n in ast.walk(tree) if isinstance(n, ast.Call)
                                     and any(x in ast.unparse(n.func).lower() for x in ("open", "write", "url", "subprocess", "exec", "order"))]
            elif path.suffix == ".json":
                record["json_valid"] = isinstance(json.loads(source), (dict, list))
            else:
                record["headings"] = re.findall(r"^#+ .+$", source, re.M)
        inventory.append(record)
    license_text = (repo / "LICENSE").read_text(encoding="utf-8")
    standard = set(sys.stdlib_module_names)
    dependencies = sorted(imports - standard - {"_load"})
    result = dict(repository=git("remote", "get-url", "origin"), commit=git("rev-parse", "HEAD"),
                  clean=not bool(git("status", "--porcelain", "--untracked-files=no")), license="MIT" if "MIT License" in license_text else "UNKNOWN",
                  dependency_imports=sorted(imports), non_stdlib_or_local_imports=dependencies,
                  python=platform.python_version(), platform=platform.platform(), files=inventory,
                  skills=sum(Path(x).name == "SKILL.md" for x in tracked), python_files=sum(x.endswith(".py") for x in tracked),
                  dependency_manifests=[x for x in tracked if Path(x).name in ("pyproject.toml", "setup.py", "requirements.txt", "Pipfile")])
    if run_tests:
        started = time.monotonic()
        completed = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                                   cwd=repo, capture_output=True, text=True, timeout=180)
        log = completed.stdout + completed.stderr
        (out / "unittest.txt").write_text(log, encoding="utf-8")
        match = re.search(r"Ran (\d+) tests?", log)
        result["tests"] = dict(command="python -m unittest discover -s tests -v", exit_code=completed.returncode,
                               count=int(match[1]) if match else None, seconds=time.monotonic() - started,
                               log_sha256=digest(log.encode()), scope="Fixture and mocked-network suite; not live endpoint or broker certification")
    save(out / "inventory.json", result)
    (out / "LICENSE.txt").write_text(license_text, encoding="utf-8")
    (out / "source-review.txt").write_text("".join(source_read), encoding="utf-8")
    return result
