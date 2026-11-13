"""Synthetic Key Vault protocol checks; no live identity or storage claim."""
import json
import os
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ServiceRequestError
from azure.core.pipeline.policies import RetryPolicy
from azure.core.pipeline.transport import HttpTransport
from azure.keyvault.secrets import KeyVaultSecret, SecretClient, SecretProperties
from azure.keyvault.secrets._generated.models import SecretAttributes
from repository_evaluator_fixture import setup
from test_repository_evaluator import FixtureSandbox

from long_haul.adapters import azure_evaluator_keys
from long_haul.adapters.azure_evaluator_keys import (
    FrozenKeyReference,
    KeyUseAdmission,
    KeyVaultWriteUncertain,
    KeyWriteGrant,
)
from long_haul.adapters.azure_evaluator_keys import (
    KeyVaultEvaluatorKeys as ProductionKeyVaultEvaluatorKeys,
)
from long_haul.adapters.evaluator_key_intent import (
    claim_key_write,
    persist_key_write_intent,
)
from long_haul.resource_policy import (
    PolicyRule,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyProfile,
    ResourceUseRequest,
)

VAULT = "https://synthetic-vault.vault.azure.net"


class KeyVaultEvaluatorKeys(ProductionKeyVaultEvaluatorKeys):
    """Test-only controller persists and independently admits synthetic intents."""

    def __init__(self, client, vault):
        super().__init__(client, vault)
        self._test_root = tempfile.TemporaryDirectory(prefix="synthetic-key-intent-")

    def freeze(self, source, contract, retention, reading):
        prepared = self.prepare(source, contract)
        root = Path(self._test_root.name)
        persist_key_write_intent(root, prepared.intent)
        grant = KeyWriteGrant(root, prepared.intent.fingerprint)
        return super().freeze(source, contract, retention, reading, prepared, grant)


class FakeSecretClient:
    vault_url = VAULT

    def __init__(self):
        self.records = {}
        self.calls = []
        self.on_set = None
        self.on_get = None
        self.fail_after_set = False
        self.tamper_read = False

    def set_secret(self, name, value, *, enabled, content_type, logging_enable, retry_total):
        assert logging_enable is False and retry_total == 0
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

    def get_secret(self, name, version, *, logging_enable, retry_total):
        assert logging_enable is False and retry_total == 0
        self.calls.append(("get", name, version))
        secret = self.records[(name, version)]
        if self.on_get:
            self.on_get()
        if self.tamper_read:
            return SimpleNamespace(id=secret.id, properties=secret.properties, value="not-json")
        return secret


class SDKModelSecretClient(FakeSecretClient):
    """Exercise real pinned SDK response models without network or credentials."""

    def set_secret(self, name, value, *, enabled, content_type, logging_enable, retry_total):
        original = super().set_secret(name, value, enabled=enabled, content_type=content_type,
                                      logging_enable=logging_enable, retry_total=retry_total)
        properties = SecretProperties(attributes=SecretAttributes(enabled=enabled), vault_id=original.id,
                                      content_type=content_type)
        secret = KeyVaultSecret(properties, value)
        self.records[(name, properties.version)] = secret
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


@pytest.mark.skipif(os.name != "posix", reason="private recovery requires POSIX semantics")
def test_production_write_requires_durable_admitted_one_shot_intent(tmp_path):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    client = FakeSecretClient()
    authority = ProductionKeyVaultEvaluatorKeys(client, VAULT)
    prepared = authority.prepare(source, contract)
    retention, reading = key_admission(source, contract, "retain"), key_admission(source, contract, "read")
    grant = KeyWriteGrant(root, prepared.intent.fingerprint)
    with pytest.raises(FileNotFoundError):
        authority.freeze(source, contract, retention, reading, prepared, grant)
    assert not client.calls
    persist_key_write_intent(root, prepared.intent)
    with pytest.raises(PermissionError, match="independently admitted"):
        authority.freeze(source, contract, retention, reading, prepared, KeyWriteGrant(root, "sha256:" + "0" * 64))
    assert not client.calls
    _, reference = authority.freeze(source, contract, retention, reading, prepared, grant)
    assert reference.name == prepared.intent.secret_name
    with pytest.raises(FileExistsError):
        authority.freeze(source, contract, retention, reading, prepared, grant)
    assert [call[0] for call in client.calls] == ["set", "get"]


@pytest.mark.skipif(os.name != "posix", reason="process-death probe requires POSIX semantics")
def test_process_death_after_provider_write_leaves_durable_locator_and_claim(tmp_path):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)

    class DyingClient(FakeSecretClient):
        def set_secret(self, name, *_args, **_kwargs):
            (root / "synthetic-provider-name").write_text(name)
            os._exit(42)

    authority = ProductionKeyVaultEvaluatorKeys(DyingClient(), VAULT)
    prepared = authority.prepare(source, contract)
    persist_key_write_intent(root, prepared.intent)
    grant = KeyWriteGrant(root, prepared.intent.fingerprint)
    pid = os.fork()
    if pid == 0:
        authority.freeze(source, contract, key_admission(source, contract, "retain"),
                         key_admission(source, contract, "read"), prepared, grant)
        os._exit(1)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 42
    assert (root / "synthetic-provider-name").read_text() == prepared.intent.secret_name
    assert (root / (prepared.intent.token + ".key-intent.json")).exists()
    with pytest.raises(FileExistsError):
        claim_key_write(root, prepared.intent, prepared.intent.fingerprint)


@pytest.mark.skipif(os.name != "posix", reason="private recovery requires POSIX semantics")
def test_client_origin_is_rechecked_after_claim_before_write(tmp_path, monkeypatch):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    client = FakeSecretClient()
    authority = ProductionKeyVaultEvaluatorKeys(client, VAULT)
    prepared = authority.prepare(source, contract)
    persist_key_write_intent(root, prepared.intent)

    def claim_then_change_origin(*args):
        claim_key_write(*args)
        client.vault_url = "https://other-vault.vault.azure.net"

    monkeypatch.setattr(azure_evaluator_keys, "claim_key_write", claim_then_change_origin)
    with pytest.raises(KeyVaultWriteUncertain):
        authority.freeze(source, contract, key_admission(source, contract, "retain"),
                         key_admission(source, contract, "read"), prepared,
                         KeyWriteGrant(root, prepared.intent.fingerprint))
    assert not client.calls
    assert (root / (prepared.intent.token + ".key-claim.json")).exists()


@pytest.mark.parametrize("module", ["evaluator_key_intent.py", "pi_workspace_intent.py"])
def test_adapter_identity_binds_recovery_implementation(monkeypatch, module):
    baseline = ProductionKeyVaultEvaluatorKeys.implementation_fingerprint()
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read_bytes(path) + (b" changed" if path.name == module else b""))
    assert ProductionKeyVaultEvaluatorKeys.implementation_fingerprint() != baseline


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


def test_key_vault_adapter_accepts_pinned_sdk_response_models_offline(tmp_path):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    authority = KeyVaultEvaluatorKeys(SDKModelSecretClient(), VAULT)
    private, reference = authority.freeze(source, contract, key_admission(source, contract, "retain"),
                                          key_admission(source, contract, "read"))
    assert authority.load(source, contract, key_admission(source, contract, "read"), reference).fingerprint == private.fingerprint


def test_pinned_sdk_retry_policy_disables_retry_for_ambiguous_network_failure():
    policy = RetryPolicy()
    settings = policy.configure_retries({"retry_total": 0})
    assert not policy.increment(settings, response=SimpleNamespace(http_request=object()),
                                error=ServiceRequestError("synthetic network failure"))


def test_pinned_sdk_pipeline_does_not_retry_an_ambiguous_key_write(tmp_path):
    class OfflineTransport(HttpTransport):
        def __init__(self):
            self.calls = 0

        def open(self):
            pass

        def close(self):
            pass

        def __exit__(self, *_args):
            self.close()

        def send(self, _request, **_kwargs):
            self.calls += 1
            raise ServiceRequestError("synthetic network failure")

    class OfflineCredential:
        def get_token(self, *_args, **_kwargs):
            raise AssertionError("no token or network access is permitted")

    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    transport = OfflineTransport()
    client = SecretClient(vault_url=VAULT, credential=OfflineCredential(), transport=transport, retry_total=3)
    authority = KeyVaultEvaluatorKeys(client, VAULT)
    with pytest.raises(KeyVaultWriteUncertain):
        authority.freeze(source, contract, key_admission(source, contract, "retain"),
                         key_admission(source, contract, "read"))
    assert transport.calls == 1


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
    client.vault_url = "https://other-vault.vault.azure.net"
    with pytest.raises(ValueError, match="origin changed"):
        authority.freeze(source, contract, key_admission(source, contract, "retain"),
                         key_admission(source, contract, "read"))
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


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("stage", ["write", "readback"])
def test_key_vault_interrupted_write_retains_recovery_locator(tmp_path, interruption, stage):
    _, contract, _, source = setup(tmp_path, FixtureSandbox.identity)
    client = FakeSecretClient()

    def interrupt():
        raise interruption("SYNTHETIC_SECRET_DO_NOT_DISCLOSE")

    if stage == "write":
        client.on_set = interrupt
    else:
        client.on_get = interrupt
    with pytest.raises(KeyVaultWriteUncertain) as caught:
        KeyVaultEvaluatorKeys(client, VAULT).freeze(
            source, contract, key_admission(source, contract, "retain"), key_admission(source, contract, "read")
        )
    assert len(client.records) == 1 and sum(call[0] == "set" for call in client.calls) == 1
    assert caught.value.secret_name == client.calls[0][1]
    assert caught.value.secret_version == (None if stage == "write" else "a" * 32)
    assert "SYNTHETIC_SECRET_DO_NOT_DISCLOSE" not in str(caught.value)


def test_key_reference_repr_never_exposes_locator():
    reference = FrozenKeyReference(VAULT, "secret-name", "secret-version", "fingerprint", "implementation")
    assert "secret-name" not in repr(reference) and "secret-version" not in repr(reference)
