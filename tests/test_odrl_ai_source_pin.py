"""Test reviewed source verification without depending on network access."""
import hashlib
import json

import pytest

from scripts.verify_odrl_ai_source import ACTIONS, FILES, MANIFEST, verify_sources


def _fixture(tmp_path):
    sections = []
    for action in ACTIONS:
        hierarchy = '<th>Included In:</th><td><a href="term-ai-training">training</a></td>' \
            if action == "ai-train-gen" else ""
        sections.append(f'<section id="term-{action}">http://www.w3.org/ns/odrl/2/{action}'
                        f"{hierarchy}</section>")
    files = {"index.html": "".join(sections).encode(), "ai-vocab.ttl": b"# TODO: empty test vocabulary\n"}
    manifest = {"commit": "a" * 40, "files": {}}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
        manifest["files"][name] = {"upstream_path": FILES[name], "sha256": hashlib.sha256(data).hexdigest()}
    return manifest


def test_pin_records_reviewed_immutable_source():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["commit"] == "a4da7f9b42b2c5b02eff2aa644d9180b6e272ad2"
    assert {name: entry["upstream_path"] for name, entry in manifest["files"].items()} == FILES


def test_verifier_checks_hashes_and_rejects_tampering(tmp_path):
    manifest = _fixture(tmp_path)
    result = verify_sources(manifest, tmp_path)
    assert result["generative_included_in_training"] is True
    assert result["turtle_status"] == "todo-skeleton"
    (tmp_path / "index.html").write_bytes(b"modified")
    with pytest.raises(ValueError, match="source hash mismatch"):
        verify_sources(manifest, tmp_path)


def test_verifier_rejects_changed_hierarchy_even_with_matching_hash(tmp_path):
    manifest = _fixture(tmp_path)
    path = tmp_path / "index.html"
    data = path.read_bytes().replace(b"term-ai-training\">training", b"term-ai-usage\">training")
    path.write_bytes(data)
    manifest["files"]["index.html"]["sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match="hierarchy missing"):
        verify_sources(manifest, tmp_path)


def test_verifier_requires_review_when_turtle_gains_a_statement(tmp_path):
    manifest = _fixture(tmp_path)
    path = tmp_path / "ai-vocab.ttl"
    data = path.read_bytes() + b"<urn:fixture:a> <urn:fixture:b> <urn:fixture:c> .\n"
    path.write_bytes(data)
    manifest["files"]["ai-vocab.ttl"]["sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(ValueError, match="review its machine-readable vocabulary"):
        verify_sources(manifest, tmp_path)
