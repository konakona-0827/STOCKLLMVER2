"""One expendable model process; no broker credentials or tool transport."""
import argparse
import importlib.util
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument("--transport",required=True);p.add_argument("--request",required=True);p.add_argument("--result",required=True);p.add_argument("--timeout",type=int,required=True)
    args=p.parse_args()
    spec=importlib.util.spec_from_file_location("bounded_paper_cli",args.transport)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    if module.metadata().get("api_fallback") is not False:raise ValueError("NO_API_FALLBACK_REQUIRED")
    request=json.loads(Path(args.request).read_text(encoding="utf-8"))
    if request.get("tools"):raise ValueError("NO_MODEL_TOOLS")
    response=module.CodexClient(timeout=args.timeout).responses.create(**request)
    value=dict(model=response.model,raw=response.output_text,usage=response.usage.model_dump(),response_id=response.id)
    output=Path(args.result);temp=output.with_suffix(".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False),encoding="utf-8");temp.replace(output)


if __name__=="__main__":main()
