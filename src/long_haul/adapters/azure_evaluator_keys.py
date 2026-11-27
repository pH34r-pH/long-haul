"""Experimental version-pinned Key Vault adapter; trusted control only.

The caller supplies an authenticated SDK client and an independently admitted
resource policy. This module does not provision Azure identities or storage.
"""
from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from itertools import islice
from pathlib import Path
from urllib.parse import urlsplit

import pydantic
import pydantic_core

from ..resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from ..resource_policy_models import canonical_json
from ..work.private_evaluator import PrivateEvaluatorProfile
from ..work.repository_evaluation_models import (
    RepositoryEvaluationProfileV1,
    evaluator_implementation_reference,
    work_contract_fingerprint,
)
from ..work.repository_models import content_digest
from .evaluator_key_intent import (
    EvaluatorKeyWriteIntent,
    claim_key_write,
    load_claimed_key_intent,
)

_CONTENT_TYPE = "application/vnd.long-haul.evaluator-keys-v2+json"
_NAME = re.compile(r"lheval-[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VAULT_HOST = re.compile(r"[a-z0-9][a-z0-9-]{1,22}[a-z0-9]\.vault\.azure\.net\Z")


class KeyVaultWriteUncertain(RuntimeError):
    """Never retry an ambiguous write; trusted recovery must inspect the name."""

    def __init__(self, name, version=None):
        super().__init__("private key write outcome is uncertain; retain for controlled recovery")
        self.secret_name = name
        self.secret_version = version


@dataclass(frozen=True, repr=False)
class FrozenKeyReference:
    vault_url: str
    name: str
    version: str
    evaluator_fingerprint: str
    implementation_fingerprint: str

    def __repr__(self):
        return "FrozenKeyReference(<private>)"


@dataclass(frozen=True, repr=False)
class PreparedKeyWrite:
    """In-memory secret material and the pre-write identity for trusted control."""

    profile: PrivateEvaluatorProfile = field(repr=False)
    intent: EvaluatorKeyWriteIntent = field(repr=False)


@dataclass(frozen=True, repr=False)
class KeyWriteGrant:
    """Scheduler-owned recovery root and independently admitted intent fingerprint."""

    recovery_root: Path = field(repr=False)
    expected_intent_fingerprint: str = field(repr=False)


@dataclass(frozen=True)
class KeyUseAdmission:
    """Authenticated control must supply this registry entry, not model data."""

    policy: object = field(repr=False)
    expected_policy_fingerprint: str = field(repr=False)
    request: object = field(repr=False)
    expected_request_fingerprint: str = field(repr=False)

    def authorize(self, source, contract, action, now):
        request = self.request
        extension = contract.repository_work
        if (extension is None or now.tzinfo is None or now.utcoffset() is None
                or not isinstance(self.policy, ResourcePolicyProfile) or not isinstance(request, ResourceUseRequest)):
            raise PermissionError("private evaluator key use lacks current admission")
        actual = (self.policy.fingerprint, request.fingerprint, self.policy.identity.content_digest,
                  request.action, request.purpose, request.caller_id, request.contract_id,
                  request.mission_id, request.repository_id, source.task, source.implementation)
        expected = (self.expected_policy_fingerprint, self.expected_request_fingerprint, source.fingerprint,
                    action, "private-evaluator-key", source.evaluator_id, contract.contract_id,
                    contract.mission_id, extension.source.repository_id, extension.task, evaluator_implementation_reference())
        if actual != expected or request.action is not action or request.now > now:
            raise PermissionError("private evaluator key use lacks current admission")
        fresh = request.model_copy(update={"now": now})
        if ResourcePolicyEvaluator().evaluate(self.policy, fresh).status is not PolicyDecisionStatus.ALLOW:
            raise PermissionError("private evaluator key use is not currently allowed")


def _vault_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or not _VAULT_HOST.fullmatch(parsed.hostname)
            or parsed.netloc != parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("invalid admitted Key Vault origin")
    return "https://" + parsed.hostname


def _reference(vault_url, name, version, fingerprint, implementation):
    if (not _NAME.fullmatch(name) or not _VERSION.fullmatch(version) or not _FINGERPRINT.fullmatch(fingerprint)
            or not _FINGERPRINT.fullmatch(implementation)):
        raise ValueError("invalid versioned private key reference")
    return FrozenKeyReference(vault_url, name, version, fingerprint, implementation)


def _metadata(secret, reference, now):
    properties = secret.properties
    expected_id = f"{reference.vault_url}/secrets/{reference.name}/{reference.version}"
    if (secret.id != expected_id or properties.version != reference.version or properties.enabled is not True
            or properties.content_type != _CONTENT_TYPE):
        raise ValueError("private key version metadata does not match")
    if properties.not_before and (properties.not_before.tzinfo is None or now < properties.not_before):
        raise ValueError("private key version is not yet active")
    if properties.expires_on and (properties.expires_on.tzinfo is None or now >= properties.expires_on):
        raise ValueError("private key version is expired")


def _sole_version(client, vault_url, name):
    versions = list(islice(client.list_properties_of_secret_versions(
        name, max_page_size=2, logging_enable=False, retry_total=0), 2))
    if len(versions) != 1:
        raise ValueError("private key recovery requires exactly one provider version")
    version = versions[0].version
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ValueError("private key recovery version identity differs")
    if versions[0].id != f"{vault_url}/secrets/{name}/{version}":
        raise ValueError("private key recovery version identity differs")
    return version


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate private key record field")
        result[key] = value
    return result


def _decode(source, reference, value):
    if not isinstance(value, str) or len(value.encode()) > 20000:
        raise ValueError("invalid private key record")
    try:
        data = json.loads(value, object_pairs_hook=_unique_object)
        if set(data) != {"schema", "source_fingerprint", "evaluator_fingerprint", "keys"}:
            raise ValueError("invalid private key record shape")
        if (data["schema"] != "long-haul-private-evaluator-keys-v2"
                or data["source_fingerprint"] != source.fingerprint
                or data["evaluator_fingerprint"] != reference.evaluator_fingerprint
                or not isinstance(data["keys"], dict)):
            raise ValueError("private key record binding mismatch")
        keys = {key: bytes.fromhex(value) for key, value in data["keys"].items()}
        if any(value.hex() != data["keys"][key] for key, value in keys.items()):
            raise ValueError("private key record encoding is not canonical")
        profile = PrivateEvaluatorProfile.from_profile(source, keys)
        if profile.fingerprint != reference.evaluator_fingerprint:
            raise ValueError("private evaluator commitment changed")
        return profile
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("invalid or mismatched private key record") from None


class KeyVaultEvaluatorKeys:
    """Version-pinned adapter over an already-authenticated SecretClient."""

    def __init__(self, client, admitted_vault_url):
        self._vault_url = _vault_url(admitted_vault_url)
        if _vault_url(client.vault_url) != self._vault_url:
            raise ValueError("Key Vault client does not match the admitted origin")
        self._client = client

    @staticmethod
    def implementation_fingerprint():
        root = Path(__file__).parent
        files = ("azure_evaluator_keys.py", "evaluator_key_intent.py", "pi_workspace_intent.py")
        packages = ("azure-core", "azure-keyvault-secrets", "requests", "urllib3", "certifi", "idna",
                    "charset-normalizer", "isodate", "typing-extensions")
        manifest = {"files": {name: content_digest((root / name).read_bytes()) for name in files},
                    "packages": {name: metadata.version(name) for name in packages},
                    "python": platform.python_version(), "pydantic": pydantic.__version__,
                    "pydantic_core": pydantic_core.__version__}
        return content_digest(canonical_json(manifest).encode())

    def _assert_client(self):
        if _vault_url(self._client.vault_url) != self._vault_url:
            raise ValueError("Key Vault client origin changed")

    def prepare(self, source, contract):
        """Generate in-memory keys and a locator to persist before provider writes."""
        if type(source) is not RepositoryEvaluationProfileV1:
            raise TypeError("private evaluator keys require an exact source profile")
        self._assert_client()
        profile = PrivateEvaluatorProfile.generate(source)
        intent = EvaluatorKeyWriteIntent.generate(
            vault_url=self._vault_url, source_fingerprint=source.fingerprint,
            contract_fingerprint=work_contract_fingerprint(contract), evaluator_fingerprint=profile.fingerprint,
            adapter_fingerprint=self.implementation_fingerprint(),
        )
        return PreparedKeyWrite(profile, intent)

    def freeze(self, source, contract, retention, reading, prepared, grant):
        if (type(source) is not RepositoryEvaluationProfileV1
                or not isinstance(retention, KeyUseAdmission) or not isinstance(reading, KeyUseAdmission)
                or type(prepared) is not PreparedKeyWrite or type(grant) is not KeyWriteGrant):
            raise TypeError("private evaluator keys require trusted policy and write-intent admission")
        self._assert_client()
        retention.authorize(source, contract, ResourceAction.RETAIN, datetime.now(UTC))
        reading.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
        profile, intent = prepared.profile, prepared.intent
        actual = (intent.vault_url, intent.source_fingerprint, intent.contract_fingerprint,
                  intent.evaluator_fingerprint, intent.adapter_fingerprint)
        expected = (self._vault_url, source.fingerprint, work_contract_fingerprint(contract), profile.fingerprint,
                    self.implementation_fingerprint())
        if type(profile) is not PrivateEvaluatorProfile or actual != expected or profile._profile.fingerprint != source.fingerprint:
            raise ValueError("private evaluator key write intent changed")
        payload = canonical_json({"schema": "long-haul-private-evaluator-keys-v2",
                                  "source_fingerprint": source.fingerprint,
                                  "evaluator_fingerprint": profile.fingerprint,
                                  "keys": {key: value.hex() for key, value in profile._keys.items()}})
        if len(payload.encode()) > 20000:
            raise ValueError("private key record exceeds bounded size")
        name = intent.secret_name
        claim_key_write(grant.recovery_root, intent, grant.expected_intent_fingerprint)
        version = None
        try:
            retention.authorize(source, contract, ResourceAction.RETAIN, datetime.now(UTC))
            reading.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
            self._assert_client()
            secret = self._client.set_secret(name, payload, enabled=True, content_type=_CONTENT_TYPE,
                                              logging_enable=False, retry_total=0)
            version = secret.properties.version
            reference = _reference(self._vault_url, name, version, profile.fingerprint,
                                   self.implementation_fingerprint())
            _metadata(secret, reference, datetime.now(UTC))
            reading.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
            replay = self._client.get_secret(name, reference.version, logging_enable=False, retry_total=0)
            _metadata(replay, reference, datetime.now(UTC))
            if _decode(source, reference, replay.value).fingerprint != profile.fingerprint:
                raise ValueError("private key version readback changed")
            retention.authorize(source, contract, ResourceAction.RETAIN, datetime.now(UTC))
            reading.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
        # A provider failure or interruption may follow a successful write and
        # may contain the request body. Retain an opaque handle without retrying.
        # The scheduler-admitted intent and one-shot claim survive process death
        # only if the supplied recovery directory is durable and trusted.
        except BaseException:  # noqa: BLE001 -- cancellation must not lose the recovery handle.
            raise KeyVaultWriteUncertain(name, version if isinstance(version, str) and _VERSION.fullmatch(version) else None) from None
        return profile, reference

    def load(self, source, contract, admission, reference):
        if (type(source) is not RepositoryEvaluationProfileV1
                or not isinstance(admission, KeyUseAdmission) or not isinstance(reference, FrozenKeyReference)):
            raise TypeError("private evaluator keys require trusted admission and versioned reference")
        self._assert_client()
        if reference.vault_url != self._vault_url:
            raise ValueError("private key reference targets another vault")
        _reference(reference.vault_url, reference.name, reference.version, reference.evaluator_fingerprint,
                   reference.implementation_fingerprint)
        if reference.implementation_fingerprint != self.implementation_fingerprint():
            raise ValueError("private key authority implementation changed")
        admission.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
        try:
            secret = self._client.get_secret(reference.name, reference.version, logging_enable=False, retry_total=0)
            _metadata(secret, reference, datetime.now(UTC))
            profile = _decode(source, reference, secret.value)
        # SDK exceptions are not safe to publish: they can contain provider data.
        except Exception:  # noqa: BLE001
            raise ValueError("private key version is unavailable or invalid") from None
        admission.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
        return profile

    def recover(self, source, contract, admission, grant, token):
        """Read one exact claimed version; never create, retry or delete a secret."""
        if (type(source) is not RepositoryEvaluationProfileV1 or not isinstance(admission, KeyUseAdmission)
                or type(grant) is not KeyWriteGrant):
            raise TypeError("private key recovery requires trusted read and intent admission")
        self._assert_client()
        intent = load_claimed_key_intent(grant.recovery_root, token, grant.expected_intent_fingerprint)
        actual = (intent.vault_url, intent.source_fingerprint, intent.contract_fingerprint, intent.adapter_fingerprint)
        expected = (self._vault_url, source.fingerprint, work_contract_fingerprint(contract),
                    self.implementation_fingerprint())
        if actual != expected:
            raise PermissionError("private key recovery lacks an exact current binding")
        admission.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
        try:
            self._assert_client()
            version = _sole_version(self._client, self._vault_url, intent.secret_name)
            reference = _reference(self._vault_url, intent.secret_name, version, intent.evaluator_fingerprint,
                                   intent.adapter_fingerprint)
            admission.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
            self._assert_client()
            secret = self._client.get_secret(intent.secret_name, version, logging_enable=False, retry_total=0)
            _metadata(secret, reference, datetime.now(UTC))
            profile = _decode(source, reference, secret.value)
            admission.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
            self._assert_client()
            if _sole_version(self._client, self._vault_url, intent.secret_name) != version:
                raise ValueError("private key recovery version changed")
        except Exception:  # noqa: BLE001 -- provider exceptions may disclose private locators or payloads.
            raise ValueError("private key recovery is unavailable or ambiguous") from None
        admission.authorize(source, contract, ResourceAction.READ, datetime.now(UTC))
        return profile, reference
