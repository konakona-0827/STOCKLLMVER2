import argparse
import json
import sys

from .common import read, protocol


def main():
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(description="Formal LLM paired PAPER experiments; no broker orders")
    commands=parser.add_subparsers(dest="command",required=True)
    p=commands.add_parser("prepare");p.add_argument("--archive",required=True);p.add_argument("--output",required=True)
    p=commands.add_parser("export");p.add_argument("--output",required=True)
    p=commands.add_parser('profile');p.add_argument('--code-root',required=True);p.add_argument('--output',required=True)
    p=commands.add_parser('report');p.add_argument('--paper-dir',required=True);p.add_argument('--day',required=True);p.add_argument('--output',required=True)
    p=commands.add_parser("replay");p.add_argument("--cohort",required=True);p.add_argument("--output",required=True);p.add_argument("--strategy",required=True);p.add_argument("--transport",required=True);p.add_argument("--max-frames",type=int)
    p=commands.add_parser("capital-probe");p.add_argument("--output",required=True);p.add_argument("--universe",required=True)
    p=commands.add_parser("forward");p.add_argument("--output",required=True);p.add_argument("--strategy",required=True);p.add_argument("--transport",required=True);p.add_argument("--sources",required=True);p.add_argument("--credentials-source",required=True);p.add_argument('--protocol');p.add_argument("--once",action="store_true")
    args=parser.parse_args()
    if args.command=="prepare":
        from .data import prepare
        r=prepare(args.archive,args.output);result={k:v for k,v in r.items() if k!="frames"}
    elif args.command=="export":
        from .release import export
        result=export(args.output)
    elif args.command=='profile':
        from .funding import profile
        result=profile(args.code_root,args.output)
    elif args.command=='report':
        from .reporting import generate
        result=generate(args.paper_dir,args.day,args.output)
    elif args.command=="capital-probe":
        from .capital_feed import probe
        result=probe(args.output,args.universe)
    elif args.command=="forward":
        from .runtime import run
        result=run(args.output,args.strategy,args.transport,args.sources,args.credentials_source,once=args.once,protocol_file=args.protocol)
    else:
        from .replay import run
        result=run(args.cohort,args.output,args.strategy,args.transport,max_frames=args.max_frames)
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=="__main__":main()
