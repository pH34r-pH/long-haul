"""Verify source identity without network or a runtime policy dependency."""
import hashlib
import json

import pytest

from scripts.verify_dpv_source import (
    MANIFEST,
    MAX_SOURCE_BYTES,
    NAMESPACE,
    UPSTREAM_PATH,
    verify_source,
)


def test_pin_records_immutable_dpv_release():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["tag"] == "dpv-2.0"
    assert manifest["commit"] == "0849628c56408676a7bbb26d2f5a239f22297bf3"
    assert manifest["namespace"] == NAMESPACE and manifest["upstream_path"] == UPSTREAM_PATH
    assert manifest["sha256"] == "3191f7a90b97487eba8f10ac609f8d8d18910b38ccd38f9bf097646e3f414bd2"


def test_verifier_accepts_exact_bytes_and_rejects_changes(tmp_path):
    source = tmp_path / "dpv.ttl"
    source.write_bytes(b"synthetic source\n")
    manifest = {"commit": "a" * 40, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "upstream_path": UPSTREAM_PATH, "namespace": NAMESPACE}
    assert verify_source(source, manifest)["bytes"] == len(source.read_bytes())
    source.write_bytes(b"altered source\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_source(source, manifest)
    source.write_bytes(b"x" * (MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError, match="byte limit"):
        verify_source(source, manifest)


@pytest.mark.parametrize("field,value", [("commit", "branch"), ("sha256", "invalid"),
                                         ("upstream_path", "other.ttl"), ("namespace", "urn:other")])
def test_verifier_rejects_invalid_pin_before_read(tmp_path, field, value):
    manifest = {"commit": "a" * 40, "sha256": "b" * 64, "upstream_path": UPSTREAM_PATH, "namespace": NAMESPACE}
    manifest[field] = value
    with pytest.raises(ValueError, match="invalid DPV source pin"):
        verify_source(tmp_path / "missing.ttl", manifest)
