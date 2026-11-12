"""Synthetic Key Vault protocol checks; no live identity or storage claim."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from repository_evaluator_fixture import setup
from test_repository_evaluator import FixtureSandbox

from long_haul.adapters.azure_evaluator_keys import (
    FrozenKeyReference,
    KeyUseAdmission,
    KeyVaultEvaluatorKeys,
    KeyVaultWriteUncertain,
)
from long_haul.resource_policy import (
    PolicyRule,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyProfile,
    ResourceUseRequest,
)

VAULT = "https://synthetic-vault.vault.azure.net"


class FakeSecretClient:
    vault_url = VAULT

    def __init__(self):
        self.records = {}
        self.calls = []
        self.on_set = None
        self.on_get = None
        self.fail_after_set = False
        self.tamper_read = False

    def set_secret(self, name, value, *, enabled, content_type, logging_enable):
        assert logging_enable is False
        version = "a" * 32
        properties = SimpleNamespace(version=version, enabled=enabled, content_type=content_type,
                                     not_before=None, expires_on=None)
        secret = SimpleNamespace(id=f"{self.vault_url}/secrets/{name}/{version}", properties=properties, value=value)
        self.records[(name, version)] = secret
        self.calls.append(("set", name, version))
        if self.on_set:
            self.on_set()
        if self.fail_after_set:
            raise RuntimeError("SYNTHETIC_SECRET_DO_NOT_DISCLOSE")
        return secret

    def get_secret(self, name, version, *, logging_enable):
        assert logging_enable is False
        self.calls.append(("get", name, version))
        secret = self.records[(name, version)]
        if self.on_get:
            self.on_get()
        if self.tamper_read:
            return SimpleNamespace(id=secret.id, properties=secret.properties, value="not-json")
        return secret


def key_admission(source, contract, action):
    policy = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:key", resource_type="private-evaluator-key",
                                  content_digest=source.fingerprint),
        lifecycle="active", policy_id="urn:fixture:key-policy", policy_assigner="urn:fixture:controller",
        permissions=[PolicyRule(rule_id="key-use", actions=["read", "retain"], purposes=["private-evaluator-key"],
                                principal_ids=[source.evaluator_id], contract_ids=[contract.contract_id])],
    )
    request = ResourceUseRequest(
        request_id="fixture-key-" + action, resource_id=policy.identity.resource_id, action=action,
        purpose="private-evaluator-key", caller_id=source.evaluator_id, contract_id=contract.contract_id,
        mission_id=contract.mission_id, repository_id=contract.repository_work.source.repository_id,
        now=datetime.now(UTC) - timedelta(seconds=1),
    )
    return KeyUseAdmission(policy, policy.fingerprint, request, request.fingerprint)


def test_key_vault_freeze_and_exact_version_replay(tmp_path):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    client = FakeSecretClient()
    authority = KeyVaultEvaluatorKeys(client, VAULT)
    private, reference = authority.freeze(source, contract, key_admission(source, contract, "retain"),
                                          key_admission(source, contract, "read"))
    replay = authority.load(source, contract, key_admission(source, contract, "read"), reference)
    assert replay.fingerprint == private.fingerprint
    assert all(replay.check_fingerprint(check) == private.check_fingerprint(check) for check in source.checks)
    assert len(set(private._keys.values())) == len(private._keys)
    assert "[-5, 2]" not in client.records[(reference.name, reference.version)].value
    assert [call[0] for call in client.calls] == ["set", "get", "get"]
    assert reference.name not in repr(reference) and "[-5, 2]" not in repr(reference)
    second, another = authority.freeze(source, contract, key_admission(source, contract, "retain"),
                                       key_admission(source, contract, "read"))
    assert second.fingerprint != private.fingerprint and another.name != reference.name


def test_key_vault_rejects_wrong_policy_and_endpoint_before_access(tmp_path):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    client = FakeSecretClient()
    with pytest.raises(ValueError, match="admitted Key Vault origin"):
        KeyVaultEvaluatorKeys(client, "https://attacker.example")
    authority = KeyVaultEvaluatorKeys(client, VAULT)
    denied = key_admission(source, contract, "retain")
    revoked = denied.policy.model_copy(update={"lifecycle": ResourceLifecycle.RETRACTED})
    denied = replace(denied, policy=revoked, expected_policy_fingerprint=revoked.fingerprint)
    with pytest.raises(PermissionError, match="not currently allowed"):
        authority.freeze(source, contract, denied, key_admission(source, contract, "read"))
    assert client.calls == []


def test_key_vault_rejects_tampering_revocation_and_wrong_version(tmp_path):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    client = FakeSecretClient()
    authority = KeyVaultEvaluatorKeys(client, VAULT)
    _, reference = authority.freeze(source, contract, key_admission(source, contract, "retain"),
                                    key_admission(source, contract, "read"))
    secret = client.records[(reference.name, reference.version)]
    changed_source = source.model_copy(update={"evaluator_version": "another"})
    with pytest.raises(ValueError, match="unavailable or invalid"):
        authority.load(changed_source, contract, key_admission(changed_source, contract, "read"), reference)
    original_value = secret.value
    data = json.loads(original_value)
    data["keys"]["sealed"] = "00" * 32
    secret.value = json.dumps(data)
    with pytest.raises(ValueError, match="unavailable or invalid"):
        authority.load(source, contract, key_admission(source, contract, "read"), reference)
    secret.value = original_value
    secret.properties.enabled = False
    with pytest.raises(ValueError, match="unavailable or invalid"):
        authority.load(source, contract, key_admission(source, contract, "read"), reference)
    bad = replace(reference, version="b" * 32)
    with pytest.raises(ValueError, match="unavailable or invalid"):
        authority.load(source, contract, key_admission(source, contract, "read"), bad)
    with pytest.raises(ValueError, match="another vault"):
        authority.load(source, contract, key_admission(source, contract, "read"),
                       replace(reference, vault_url="https://another.vault.azure.net"))


def test_key_vault_rejects_expiry_and_untrusted_reference_before_returning_keys(tmp_path):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    client = FakeSecretClient()
    authority = KeyVaultEvaluatorKeys(client, VAULT)
    _, reference = authority.freeze(source, contract, key_admission(source, contract, "retain"),
                                    key_admission(source, contract, "read"))
    secret = client.records[(reference.name, reference.version)]
    secret.properties.expires_on = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ValueError, match="unavailable or invalid"):
        authority.load(source, contract, key_admission(source, contract, "read"), reference)
    before = len(client.calls)
    with pytest.raises(ValueError, match="invalid versioned"):
        authority.load(source, contract, key_admission(source, contract, "read"), replace(reference, name="../escape"))
    with pytest.raises(ValueError, match="implementation changed"):
        authority.load(source, contract, key_admission(source, contract, "read"),
                       replace(reference, implementation_fingerprint="sha256:" + "0" * 64))
    assert len(client.calls) == before


def test_key_vault_rechecks_policy_before_releasing_replayed_keys(tmp_path):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    client = FakeSecretClient()
    authority = KeyVaultEvaluatorKeys(client, VAULT)
    _, reference = authority.freeze(source, contract, key_admission(source, contract, "retain"),
                                    key_admission(source, contract, "read"))
    reading = key_admission(source, contract, "read")
    client.on_get = lambda: setattr(reading.policy, "lifecycle", ResourceLifecycle.RETRACTED)
    with pytest.raises(PermissionError, match="current admission"):
        authority.load(source, contract, reading, reference)


@pytest.mark.parametrize("failure", ["write", "readback", "revoked"])
def test_key_vault_ambiguous_write_is_never_retried(tmp_path, failure):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    client = FakeSecretClient()
    retention, reading = key_admission(source, contract, "retain"), key_admission(source, contract, "read")
    client.fail_after_set = failure == "write"
    client.tamper_read = failure == "readback"
    if failure == "revoked":
        client.on_set = lambda: setattr(reading.policy, "lifecycle", ResourceLifecycle.RETRACTED)
    with pytest.raises(KeyVaultWriteUncertain) as caught:
        KeyVaultEvaluatorKeys(client, VAULT).freeze(source, contract, retention, reading)
    assert "SYNTHETIC_SECRET_DO_NOT_DISCLOSE" not in str(caught.value)
    assert caught.value.secret_name.startswith("lheval-")
    assert caught.value.secret_name not in str(caught.value)
    assert sum(call[0] == "set" for call in client.calls) == 1


def test_key_reference_repr_never_exposes_locator():
    reference = FrozenKeyReference(VAULT, "secret-name", "secret-version", "fingerprint", "implementation")
    assert "secret-name" not in repr(reference) and "secret-version" not in repr(reference)
