"""Private durable intent and one-shot claim for an evaluator key write.

The scheduler must independently retain and authenticate the expected intent
fingerprint. A file in the local recovery directory is not write authority.
The directory must itself be durable and privately owned by trusted control.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

from pydantic import Field

from ..work.repository_models import FrozenProjection
from .pi_workspace_intent import _open_private_root

_DIGEST = r"^sha256:[0-9a-f]{64}$"


class EvaluatorKeyWriteIntent(FrozenProjection):
    token: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    vault_url: str = Field(max_length=256, repr=False)
    source_fingerprint: str = Field(pattern=_DIGEST, repr=False)
    contract_fingerprint: str = Field(pattern=_DIGEST, repr=False)
    evaluator_fingerprint: str = Field(pattern=_DIGEST, repr=False)
    adapter_fingerprint: str = Field(pattern=_DIGEST, repr=False)

    @classmethod
    def generate(cls, *, vault_url, source_fingerprint, contract_fingerprint,
                 evaluator_fingerprint, adapter_fingerprint):
        return cls(token=secrets.token_hex(32), vault_url=vault_url, source_fingerprint=source_fingerprint,
                   contract_fingerprint=contract_fingerprint, evaluator_fingerprint=evaluator_fingerprint,
                   adapter_fingerprint=adapter_fingerprint)

    @property
    def secret_name(self):
        return "lheval-" + self.token


def _write_once(directory, name, payload):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.fsync(directory)


def _validated_intent(intent):
    if type(intent) is not EvaluatorKeyWriteIntent:
        raise TypeError("key write requires an exact recovery intent")
    validated = EvaluatorKeyWriteIntent.model_validate(intent.model_dump())
    if validated != intent:
        raise ValueError("key write recovery intent changed")
    return validated


def persist_key_write_intent(root: Path, intent: EvaluatorKeyWriteIntent):
    """Sync the private locator and expected identities before scheduler admission."""
    intent = _validated_intent(intent)
    directory = _open_private_root(root)
    try:
        _write_once(directory, intent.token + ".key-intent.json", intent.model_dump_json().encode())
    finally:
        os.close(directory)
    return root / (intent.token + ".key-intent.json")


def _read_private(directory, name, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1 or metadata.st_mode & 0o077 or metadata.st_size > limit):
            raise PermissionError("key recovery record is not private and bounded")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise PermissionError("key recovery record is not private and bounded")
    return data


def load_claimed_key_intent(root: Path, token: str, expected_fingerprint: str):
    """Load a claimed intent only against an independently retained identity."""
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
        raise ValueError("invalid key recovery token")
    directory = _open_private_root(root)
    try:
        data = _read_private(directory, token + ".key-intent.json", 16384)
        intent = EvaluatorKeyWriteIntent.model_validate_json(data)
        claim = _read_private(directory, token + ".key-claim.json", 128)
    finally:
        os.close(directory)
    if (intent.token != token or intent.fingerprint != expected_fingerprint
            or claim != intent.fingerprint.encode("ascii")):
        raise PermissionError("key recovery intent is not independently admitted and claimed")
    return intent


def claim_key_write(root: Path, intent: EvaluatorKeyWriteIntent, expected_fingerprint: str):
    """Sync a one-shot write claim after matching independent scheduler admission."""
    intent = _validated_intent(intent)
    if intent.fingerprint != expected_fingerprint:
        raise PermissionError("key write intent is not independently admitted")
    directory = _open_private_root(root)
    try:
        data = _read_private(directory, intent.token + ".key-intent.json", 16384)
        persisted = EvaluatorKeyWriteIntent.model_validate_json(data)
        if persisted != intent or persisted.fingerprint != expected_fingerprint:
            raise PermissionError("key write intent differs from scheduler admission")
        _write_once(directory, intent.token + ".key-claim.json", persisted.fingerprint.encode("ascii"))
    finally:
        os.close(directory)
