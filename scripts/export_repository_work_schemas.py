"""Export exact offline authoring schemas for shared repository-work consumers."""
import argparse
import json
from pathlib import Path

from long_haul.work.repository_models import (
    RepositoryContextManifestV1,
    RepositoryWorkExtensionV1,
)


def export(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name, model in (("repository-work-v1", RepositoryWorkExtensionV1),
                        ("repository-context-v1", RepositoryContextManifestV1)):
        schema = model.model_json_schema()
        schema["$id"] = "https://ph34r.dev/ns/long-haul/" + name + ".schema.json"
        (output / (name + ".schema.json")).write_text(
            json.dumps(schema, sort_keys=True, indent=2) + "\n", encoding="utf-8",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("build/repository-work"))
    export(parser.parse_args().output_dir)
