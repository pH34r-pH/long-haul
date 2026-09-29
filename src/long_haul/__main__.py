import argparse
import json
import os
from pathlib import Path

from .registry import load_vessel


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")

    show = sub.add_parser("show")
    show.add_argument("manifest")

    discover = sub.add_parser("discover")
    discover.add_argument("--vessel-id", required=True)
    discover.add_argument("--name")
    discover.add_argument("--class-name", default="station", choices=["station", "ship", "light_craft"])
    discover.add_argument("--resource-prefix")
    discover.add_argument("--output")

    reference = sub.add_parser("validate-reference")
    reference.add_argument("--binary", required=True)
    reference.add_argument("--model", required=True)
    reference.add_argument("--output", default="reference-run")
    reference.add_argument("--timeout", type=float, default=900)

    benchmark = sub.add_parser("benchmark-matrix")
    benchmark.add_argument("--binary", required=True)
    benchmark.add_argument("--bench-binary")
    benchmark.add_argument("--model", required=True)
    benchmark.add_argument("--manifest", required=True)
    benchmark.add_argument("--output", default="benchmark-matrix")
    benchmark.add_argument("--timeout", type=float, default=900)
    benchmark.add_argument("--profile", action="append", dest="profiles")
    return parser


def _discover(args: argparse.Namespace) -> None:
    name = args.name or args.vessel_id
    if os.name == "nt":
        from .discovery.windows import GenericWindowsDiscovery

        vessel = GenericWindowsDiscovery(
            args.vessel_id,
            name,
            args.class_name,
            resource_prefix=args.resource_prefix,
        ).discover()
    else:
        from .discovery.linux import GenericLinuxDiscovery

        vessel = GenericLinuxDiscovery(args.vessel_id, name, args.class_name).discover()
    rendered = vessel.model_dump_json(indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    print(rendered)


def _benchmark(args: argparse.Namespace) -> None:
    from .benchmark_matrix import run

    report = run(
        args.binary,
        args.model,
        args.manifest,
        args.output,
        args.timeout,
        args.bench_binary,
        set(args.profiles) if args.profiles else None,
    )
    print(json.dumps(report, indent=2))


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.command == "show":
        print(load_vessel(args.manifest).model_dump_json(indent=2))
        return
    if args.command == "discover":
        _discover(args)
        return
    if args.command == "validate-reference":
        from .reference import run

        print(run(args.binary, args.model, args.output, args.timeout).model_dump_json(indent=2))
        return
    if args.command == "benchmark-matrix":
        _benchmark(args)
        return
    parser.error("a command is required")


if __name__ == "__main__":
    main()
