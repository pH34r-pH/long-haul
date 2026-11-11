"""Allowlisted metadata and keyed fingerprints, before ordinary persistence."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field

from ..resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from ..work.contracts import WorkContract
from ..work.harness import HarnessAttempt, NormalizedArguments, harness_time
from ..work.repository_models import (
    DigestReference,
    FrozenProjection,
    content_digest,
    repository_path,
)


class TraceAdmissionDenied(ValueError):
    pass


class TraceArgumentsInvalid(ValueError):
    pass


class TraceNormalizationPolicy(FrozenProjection):
    version: Literal["semantic-metadata-v1"] = "semantic-metadata-v1"
    key_reference: DigestReference
    disclose_paths: bool = Field(default=False, strict=True)
    disclose_native_ids: bool = Field(default=False, strict=True)
    max_argument_bytes: int = Field(default=65536, ge=256, le=1048576)
    max_result_bytes: int = Field(default=65536, ge=256, le=1048576)


@dataclass(frozen=True)
class TraceUseAdmission:
    """Registry input from authenticated control, never imported from a tool event."""

    profile: ResourcePolicyProfile
    expected_policy_fingerprint: str
    request: ResourceUseRequest

    def authorize(self, attempt: HarnessAttempt, now: datetime) -> DigestReference:
        now = harness_time(now)
        request = self.request
        dimensions = ("contract_id", "mission_id", "crew_id", "repository_id")
        if (self.profile.fingerprint != self.expected_policy_fingerprint
                or request.caller_id != attempt.session_id or request.now > now
                or request.action is not ResourceAction.ANNOTATE or request.purpose != "operational-trace"
                or any(getattr(request, key) != getattr(attempt, key) for key in dimensions)):
            raise TraceAdmissionDenied("trace use lacks exact authenticated current admission")
        fresh = request.model_copy(update={"now": now})
        decision = ResourcePolicyEvaluator().evaluate(self.profile, fresh)
        if decision.status is not PolicyDecisionStatus.ALLOW:
            raise TraceAdmissionDenied("trace annotation is expired, revoked, or unauthorized")
        return DigestReference(reference="artifact:trace-policy-decision", digest=content_digest(
            decision.model_dump_json().encode(),
        ))


def supported_pi_tools(contract: WorkContract) -> tuple[str, ...]:
    """Native Bash cannot be used to implement a narrower test capability."""
    capabilities = set(contract.capabilities)
    if not capabilities <= {"read", "edit", "search"}:
        raise ValueError("Pi profile does not admit shell, test, or unknown capabilities")
    mappings = {"read": ("read",), "edit": ("edit", "write"), "search": ("grep", "find", "ls")}
    return tuple(tool for capability in sorted(capabilities) for tool in mappings[capability])


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _positive(value, default=None, *, zero=False):
    actual = default if value is None else value
    if actual is None:
        return None
    if type(actual) is not int or actual < (0 if zero else 1) or actual > 1000000:
        raise TraceArgumentsInvalid("invalid bounded numeric argument")
    return actual


class TraceNormalizer:
    """The secret key is held outside records; its public tag declares comparability."""

    def __init__(self, contract: WorkContract, attempt: HarnessAttempt, policy: TraceNormalizationPolicy,
                 key: bytes, admission: TraceUseAdmission):
        if len(key) < 32:
            raise ValueError("trace fingerprints require at least 256 bits of external key material")
        if content_digest(contract.model_dump_json().encode()) != attempt.contract_digest:
            raise ValueError("trace normalizer contract identity mismatch")
        self.contract, self.attempt, self.policy, self.admission = contract, attempt, policy, admission
        self.tools = supported_pi_tools(contract)
        self._key = key
        implementation = Path(__file__).read_bytes()
        config = {"policy": policy.model_dump(mode="json"), "key_tag": self.fingerprint("key-tag", "v1")}
        self.identity = DigestReference(reference="urn:long-haul:trace-normalization-v1",
                                        digest=content_digest(implementation + _canonical(config)))

    def fingerprint(self, domain: str, value: object) -> str:
        data = domain.encode() + b"\0" + _canonical(value)
        return "hmac-sha256:" + hmac.new(self._key, data, hashlib.sha256).hexdigest()

    def native_id(self, value: str) -> tuple[str, str]:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
            raise TraceArgumentsInvalid("invalid bounded native correlation identifier")
        if self.policy.disclose_native_ids:
            return value, "raw"
        return self.fingerprint("native-id", value), "keyed"

    def _path(self, value) -> str:
        if not isinstance(value, str) or ".." in PurePosixPath(value).parts:
            raise TraceArgumentsInvalid("invalid repository target")
        path = PurePosixPath(value)
        if path.is_absolute():
            try:
                path = path.relative_to("/workspace")
            except ValueError:
                raise TraceArgumentsInvalid("target is outside the projected workspace") from None
        try:
            normalized = repository_path(path.as_posix())
            if not self.contract.permits_path(normalized):
                raise ValueError("outside scope")
        except ValueError:
            raise TraceArgumentsInvalid("target is outside admitted repository scope") from None
        return normalized

    def _content(self, value) -> str:
        if not isinstance(value, str):
            raise TraceArgumentsInvalid("content and query arguments must be strings")
        return self.fingerprint("content", value)

    def _edits(self, arguments):
        edits = arguments.get("edits")
        if edits is None and set(arguments) == {"path", "oldText", "newText"}:
            edits = [{"oldText": arguments["oldText"], "newText": arguments["newText"]}]
        if not isinstance(edits, list) or not 1 <= len(edits) <= 128:
            raise TraceArgumentsInvalid("edit requires bounded replacements")
        normalized = []
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"oldText", "newText"}:
                raise TraceArgumentsInvalid("invalid replacement fields")
            normalized.append({name: self._content(value) for name, value in edit.items()})
        return normalized

    @staticmethod
    def _read(arguments):
        return {"offset": _positive(arguments.get("offset"), 1), "limit": _positive(arguments.get("limit"))}

    def _write(self, arguments):
        return {"content_fingerprint": self._content(arguments.get("content"))}

    def _edit(self, arguments):
        return {"content_fingerprint": self.fingerprint("edits", self._edits(arguments))}

    def _search(self, tool, arguments):
        normalized = {"limit": _positive(arguments.get("limit"), 100 if tool == "grep" else 1000)}
        if tool in {"grep", "find"}:
            normalized["query_fingerprint"] = self._content(arguments.get("pattern"))
        if tool != "grep":
            return normalized
        for flag in ("ignoreCase", "literal"):
            value = arguments.get(flag, False)
            if type(value) is not bool:
                raise TraceArgumentsInvalid("invalid search flag")
            normalized[flag] = value
        normalized["context"] = _positive(arguments.get("context"), 0, zero=True)
        normalized["glob"] = self._content(arguments["glob"]) if "glob" in arguments else None
        return normalized

    def _known(self, tool, arguments):
        allowed = {
            "read": {"path", "offset", "limit"}, "write": {"path", "content"},
            "edit": {"path", "edits", "oldText", "newText"},
            "grep": {"path", "pattern", "glob", "ignoreCase", "literal", "context", "limit"},
            "find": {"path", "pattern", "limit"}, "ls": {"path", "limit"},
        }
        if not set(arguments) <= allowed[tool]:
            raise TraceArgumentsInvalid("unknown tool argument fields")
        if any(value is None for value in arguments.values()):
            raise TraceArgumentsInvalid("explicit null is not an omitted native tool argument")
        target = self._path(arguments.get("path", "."))
        normalized = {"target_fingerprint": self.fingerprint("target", target)}
        handlers = {"read": self._read, "write": self._write, "edit": self._edit}
        normalized.update(handlers[tool](arguments) if tool in handlers else self._search(tool, arguments))
        details = {"target_fingerprint": normalized["target_fingerprint"],
                   "path": target if self.policy.disclose_paths else None}
        details.update({name: value for name, value in normalized.items() if name in NormalizedArguments.model_fields})
        return NormalizedArguments(argument_fingerprint=self.fingerprint("arguments", normalized), **details)

    def normalize(self, tool: str, arguments: dict, now: datetime):
        receipt = self.admission.authorize(self.attempt, now)
        if (not isinstance(tool, str) or len(tool) > 128 or not isinstance(arguments, dict)
                or len(_canonical(arguments)) > self.policy.max_argument_bytes):
            raise TraceArgumentsInvalid("argument size or structure is invalid")
        if tool not in self.tools:
            return "unknown", "unknown", NormalizedArguments(
                argument_fingerprint=self.fingerprint("unadmitted-action", {"tool": tool, "arguments": arguments}),
            ), receipt
        normalized = self._known(tool, arguments)
        action = "search" if tool in {"grep", "find", "ls"} else tool
        return tool, action, normalized, receipt

    def result(self, value: object) -> str:
        if len(_canonical(value)) > self.policy.max_result_bytes:
            raise TraceArgumentsInvalid("result exceeds trace normalization limit")
        return self.fingerprint("result", value)
