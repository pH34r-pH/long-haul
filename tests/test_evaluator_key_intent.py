"""Synthetic local recovery intents; scheduler and Key Vault remain independent."""
import os
from pathlib import Path

import pytest

from long_haul.adapters.evaluator_key_intent import (
    EvaluatorKeyWriteIntent,
    claim_key_write,
    load_claimed_key_intent,
    persist_key_write_intent,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private recovery roots require POSIX semantics")


@pytest.fixture
def intent():
    return EvaluatorKeyWriteIntent.generate(
        vault_url="https://fixture.vault.azure.net", source_fingerprint="sha256:" + "1" * 64,
        contract_fingerprint="sha256:" + "2" * 64, evaluator_fingerprint="sha256:" + "3" * 64,
        adapter_fingerprint="sha256:" + "4" * 64,
    )


def test_intent_is_private_persistent_and_claim_is_one_shot(tmp_path, intent):
    tmp_path.chmod(0o700)
    path = persist_key_write_intent(tmp_path, intent)
    assert path.stat().st_mode & 0o777 == 0o600
    claim_key_write(tmp_path, intent, intent.fingerprint)
    assert load_claimed_key_intent(tmp_path, intent.token, intent.fingerprint) == intent
    marker = tmp_path / (intent.token + ".key-claim.json")
    assert marker.read_text() == intent.fingerprint and marker.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        claim_key_write(tmp_path, intent, intent.fingerprint)


def test_recovery_requires_an_exact_private_claim(tmp_path, intent):
    tmp_path.chmod(0o700)
    persist_key_write_intent(tmp_path, intent)
    with pytest.raises(FileNotFoundError):
        load_claimed_key_intent(tmp_path, intent.token, intent.fingerprint)
    claim_key_write(tmp_path, intent, intent.fingerprint)
    with pytest.raises(PermissionError):
        load_claimed_key_intent(tmp_path, intent.token, "sha256:" + "0" * 64)
    with pytest.raises(ValueError):
        load_claimed_key_intent(tmp_path, "../outside", intent.fingerprint)
    marker = tmp_path / (intent.token + ".key-claim.json")
    marker.write_text("wrong")
    with pytest.raises(PermissionError):
        load_claimed_key_intent(tmp_path, intent.token, intent.fingerprint)


def test_recovery_record_errors_do_not_expose_private_locator(tmp_path, intent):
    tmp_path.chmod(0o700)
    with pytest.raises(FileNotFoundError) as missing:
        load_claimed_key_intent(tmp_path, intent.token, intent.fingerprint)
    assert intent.token not in str(missing.value) and str(tmp_path) not in str(missing.value)
    persist_key_write_intent(tmp_path, intent)
    claim_key_write(tmp_path, intent, intent.fingerprint)
    with pytest.raises(FileExistsError) as duplicate:
        claim_key_write(tmp_path, intent, intent.fingerprint)
    assert intent.token not in str(duplicate.value) and str(tmp_path) not in str(duplicate.value)


def test_malformed_intent_error_does_not_echo_private_contents(tmp_path, intent):
    tmp_path.chmod(0o700)
    path = persist_key_write_intent(tmp_path, intent)
    path.write_text('{"token":"SYNTHETIC_PRIVATE_LOCATOR"}')
    with pytest.raises(ValueError) as failure:
        claim_key_write(tmp_path, intent, intent.fingerprint)
    assert "SYNTHETIC_PRIVATE_LOCATOR" not in str(failure.value)


def test_claim_requires_independent_exact_fingerprint_and_matching_intent(tmp_path, intent):
    tmp_path.chmod(0o700)
    path = persist_key_write_intent(tmp_path, intent)
    with pytest.raises(PermissionError, match="independently admitted"):
        claim_key_write(tmp_path, intent, "sha256:" + "0" * 64)
    changed = intent.model_copy(update={"source_fingerprint": "sha256:" + "0" * 64})
    with pytest.raises(PermissionError, match="differs"):
        claim_key_write(tmp_path, changed, changed.fingerprint)
    assert path.exists() and not (tmp_path / (intent.token + ".key-claim.json")).exists()


def test_unvalidated_copied_intent_cannot_escape_recovery_root(tmp_path, intent):
    tmp_path.chmod(0o700)
    invalid = intent.model_copy(update={"token": "../outside"})
    with pytest.raises(ValueError, match="invalid private key recovery intent"):
        persist_key_write_intent(tmp_path, invalid)
    with pytest.raises(ValueError, match="invalid private key recovery intent"):
        claim_key_write(tmp_path, invalid, invalid.fingerprint)
    assert not (tmp_path.parent / "outside.key-intent.json").exists()


@pytest.mark.parametrize("tamper", ["symlink", "hardlink", "permissions"])
def test_claim_rejects_untrusted_recovery_file(tmp_path, intent, tamper):
    tmp_path.chmod(0o700)
    path = persist_key_write_intent(tmp_path, intent)
    if tamper == "symlink":
        replacement = tmp_path / "replacement"
        path.rename(replacement)
        path.symlink_to(replacement)
    elif tamper == "hardlink":
        os.link(path, tmp_path / "second-link")
    else:
        path.chmod(0o644)
    with pytest.raises((OSError, PermissionError)):
        claim_key_write(tmp_path, intent, intent.fingerprint)
    assert not Path(tmp_path / (intent.token + ".key-claim.json")).exists()
