import argparse

from .registry import load_vessel


def main() -> None:
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="command")
    show=sub.add_parser("show"); show.add_argument("manifest")
    reference=sub.add_parser("validate-reference"); reference.add_argument("--binary",required=True); reference.add_argument("--model",required=True); reference.add_argument("--output",default="reference-run")
    args=p.parse_args()
    if args.command == "validate-reference":
        from .reference import run
        print(run(args.binary,args.model,args.output).model_dump_json(indent=2))
    else: print(load_vessel(args.manifest).model_dump_json(indent=2))
if __name__ == "__main__": main()
