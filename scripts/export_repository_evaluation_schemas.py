"""Export offline exact-candidate evaluator schemas for Compiler consumers."""
import argparse
import json
from pathlib import Path

from long_haul.work.repository_evaluation_models import (
    FrozenRepositoryCandidate,
    RepositoryEvaluationProfileV1,
    RepositoryEvidencePacketV1,
)


def export(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for model in (RepositoryEvaluationProfileV1, RepositoryEvidencePacketV1, FrozenRepositoryCandidate):
        schema = model.model_json_schema()
        name = model.model_fields["profile"].default
        schema["$id"] = "https://ph34r.dev/ns/long-haul/" + name + ".schema.json"
        (output / (name + ".schema.json")).write_text(json.dumps(schema, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/repository-evaluation"))
    export(parser.parse_args().output_dir)
