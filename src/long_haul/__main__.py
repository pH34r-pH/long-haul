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
    reference.add_argument("--vessel-id")
    reference.add_argument("--vessel-name")
    reference.add_argument("--resource-prefix")

    benchmark = sub.add_parser("benchmark-matrix")
    benchmark.add_argument("--binary", required=True)
    benchmark.add_argument("--model", required=True)
    benchmark.add_argument("--manifest", required=True)
    benchmark.add_argument("--output", default="benchmark-matrix")
    benchmark.add_argument("--timeout", type=float, default=900)

    replay = sub.add_parser("benchmark-scheduler")
    replay.add_argument("--vessel", required=True)
    replay.add_argument("--matrix-report", required=True)
    replay.add_argument("--benchmarks", required=True)
    replay.add_argument("--workload")

    native = sub.add_parser("import-llama-bench")
    native.add_argument("--manifest", required=True)
    native.add_argument("--profile-id", required=True)
    native.add_argument("--raw", required=True)
    native.add_argument("--output", required=True)
    return parser


def _show(args) -> None:
    print(load_vessel(args.manifest).model_dump_json(indent=2))


def _discover(args) -> None:
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


def _reference(args) -> None:
    from .reference import run

    result = run(
        args.binary,
        args.model,
        args.output,
        args.timeout,
        vessel_id=args.vessel_id,
        vessel_name=args.vessel_name,
        resource_prefix=args.resource_prefix,
    )
    print(result.model_dump_json(indent=2))


def _benchmark(args) -> None:
    from .benchmark_matrix import run

    print(json.dumps(run(args.binary, args.model, args.manifest, args.output, args.timeout), indent=2))


def _replay(args) -> None:
    from .benchmark_scheduler import run

    result = run(args.vessel, args.matrix_report, args.benchmarks, workload=args.workload)
    print(json.dumps(result, indent=2))


def _native(args) -> None:
    from .llama_bench_import import import_profile

    result = import_profile(args.manifest, args.profile_id, args.raw, args.output)
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    handlers = {
        "show": _show,
        "discover": _discover,
        "validate-reference": _reference,
        "benchmark-matrix": _benchmark,
        "benchmark-scheduler": _replay,
        "import-llama-bench": _native,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.error("a command is required")
    handler(args)


if __name__ == "__main__":
    main()
