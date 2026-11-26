"""All outputs isolated from production; nothing submits broker orders."""
import argparse
import json
import sys
from pathlib import Path

from .audit import audit_repository
from .experiment import run, load_protocol
from .forward import ForwardStore
from .review import review, packet_from_run


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--repo", required=True)
    audit.add_argument("--output", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--dataset", required=True)
    replay.add_argument("--output", required=True)
    replay.add_argument("--protocol")
    pilot = commands.add_parser("review")
    pilot.add_argument("--run", required=True)
    pilot.add_argument("--transport", required=True)
    pilot.add_argument("--output", required=True)
    pilot.add_argument("--model", default="gpt-6-sol")
    init = commands.add_parser("forward-init")
    init.add_argument("--store", required=True)
    init.add_argument("--protocol")
    forward = commands.add_parser("forward")
    forward.add_argument("--store", required=True)
    forward.add_argument("--version", required=True)
    forward.add_argument("--snapshot", required=True)
    control = commands.add_parser("control")
    control.add_argument("--store", required=True)
    control.add_argument("--version", required=True)
    control.add_argument("--state", choices=("active", "inactive"), required=True)
    args = parser.parse_args()
    if args.command == "audit":
        result = audit_repository(args.repo, args.output)
        result = {k: v for k, v in result.items() if k not in ("files", "dependency_imports")}
    elif args.command == "replay":
        result = run(args.dataset, args.output, load_protocol(args.protocol))
    elif args.command == "review":
        result = review(packet_from_run(args.run), args.transport, args.output, args.model)
    elif args.command == "forward-init":
        result = dict(version=ForwardStore(args.store).register(load_protocol(args.protocol)), mode="PAPER_ONLY")
    elif args.command == "forward":
        result = ForwardStore(args.store).cycle(args.version, json.loads(Path(args.snapshot).read_text(encoding="utf-8-sig")))
    else:
        ForwardStore(args.store).set_active(args.version, args.state == "active")
        result = dict(version=args.version, active=args.state == "active")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
