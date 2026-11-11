"""Authenticated host admission for the pinned Pi native filesystem tools.

This is deliberately separate from annotation permission. Raw RPC arguments are
transient; only keyed operation bindings and shared-policy receipts survive.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from ..work.harness import HarnessAttempt
from ..work.repository_models import (
    DigestReference,
    FrozenProjection,
    content_digest,
)
from ..work.worker_candidate import regular_contents, worker_path
from .harness_trace import TraceAdmissionDenied, TraceArgumentsInvalid, TraceNormalizer

GUARD_TITLE = "long-haul-tool-admission-v1"


@dataclass(frozen=True)
class WorkerResourceAdmission:
    profile: ResourcePolicyProfile
    expected_policy_fingerprint: str
    content_digest: str | None
    purpose: str = "repository-work"


class ToolAdmissionReceipt(FrozenProjection):
    attempt_id: str
    session_id: str
    native_id: str
    operation_fingerprint: str
    policies: tuple[DigestReference, ...]


class PiToolAdmission:
    def __init__(self, normalizer: TraceNormalizer, registry: dict[str, WorkerResourceAdmission]):
        self.normalizer = normalizer
        self.registry = registry  # Authenticated caller may revoke a live profile.
        self._pending = None
        self._authorized = None
        self._seen_ui: set[str] = set()
        self.receipts: list[ToolAdmissionReceipt] = []

    @property
    def attempt(self) -> HarnessAttempt:
        return self.normalizer.attempt

    def _exact_contract(self):
        if content_digest(self.normalizer.contract.model_dump_json().encode()) != self.attempt.contract_digest:
            raise TraceAdmissionDenied("work contract changed after attempt admission")

    def start(self, record: dict):
        self._exact_contract()
        if self._pending is not None:
            raise TraceArgumentsInvalid("parallel native calls are unsupported")
        native = record["toolCallId"]
        self.normalizer.native_id(native)
        tool, args = record["toolName"], record["args"]
        if tool not in {"read", "write", "edit"} or tool not in self.normalizer.tools:
            raise TraceAdmissionDenied("native executable and search tools are unsupported in this worker profile")
        normalized = self.normalizer._known(tool, args)
        self._pending = (native, tool, normalized.argument_fingerprint)
        self._authorized = None

    def _policy(self, binding, action, path, now):
        attempt = self.attempt
        if (binding.profile.fingerprint != binding.expected_policy_fingerprint
                or binding.profile.identity.content_digest != binding.content_digest):
            raise TraceAdmissionDenied("worker resource policy or content identity changed")
        request = ResourceUseRequest(
            request_id=attempt.attempt_id + ":tool:" + str(len(self.receipts)) + ":" + action.value,
            resource_id=binding.profile.identity.resource_id, caller_id=attempt.session_id,
            contract_id=attempt.contract_id, mission_id=attempt.mission_id, crew_id=attempt.crew_id,
            repository_id=attempt.repository_id, action=action, purpose=binding.purpose, now=now,
        )
        decision = ResourcePolicyEvaluator().evaluate(binding.profile, request)
        if decision.status is not PolicyDecisionStatus.ALLOW:
            raise TraceAdmissionDenied("current shared resource policy denies native file use")
        return DigestReference(reference="artifact:worker-policy-decision", digest=content_digest(
            decision.model_dump_json().encode(),
        ))

    def authorize(self, record: dict, root: Path, now: datetime) -> ToolAdmissionReceipt:
        self._exact_contract()
        request = self._guard_request(record)
        tool, args = request["toolName"], request["args"]
        _, _, normalized, _ = self.normalizer.normalize(tool, args, now)
        binding_id = (request["toolCallId"], tool, normalized.argument_fingerprint)
        if self._pending != binding_id or tool not in {"read", "write", "edit"}:
            raise TraceArgumentsInvalid("native interlock does not match exact pending operation")
        path, binding = self._target(tool, args, root)
        actions = (ResourceAction.DERIVE,) if tool == "write" else (ResourceAction.READ, ResourceAction.AI_AUGMENT)
        if tool == "edit":
            actions += (ResourceAction.DERIVE,)
        policies = tuple(self._policy(binding, action, path, now) for action in actions)
        receipt = ToolAdmissionReceipt(
            attempt_id=self.attempt.attempt_id, session_id=self.attempt.session_id,
            native_id=self.normalizer.native_id(request["toolCallId"])[0],
            operation_fingerprint=normalized.argument_fingerprint, policies=policies,
        )
        self.receipts.append(receipt)
        self._authorized = receipt
        self._target_path = path
        self._before = {name: content_digest(data) for name, data in regular_contents(root).items()}
        return receipt

    def _guard_request(self, record):
        if self._authorized is not None:
            raise TraceArgumentsInvalid("native operation already has an admission response")
        if (record.get("type") != "extension_ui_request" or record.get("method") != "confirm"
                or record.get("title") != GUARD_TITLE or not isinstance(record.get("id"), str)
                or not 1 <= len(record["id"]) <= 128 or record["id"] in self._seen_ui):
            raise TraceArgumentsInvalid("unexpected or replayed native tool interlock")
        self._seen_ui.add(record["id"])
        request = json.loads(record["message"])
        if (set(request) != {"attempt_id", "session_id", "toolCallId", "toolName", "args"}
                or request["attempt_id"] != self.attempt.attempt_id
                or request["session_id"] != self.attempt.session_id):
            raise TraceArgumentsInvalid("native interlock belongs to another attempt or session")
        return request

    def _target(self, tool, args, root):
        path = worker_path(self.normalizer._path(args["path"]))
        # Native Pi expands @/~ and normalizes Unicode spaces; admit one exact
        # ASCII spelling and existing reads, so its filename fallbacks cannot run.
        if args["path"] != path or re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None:
            raise TraceArgumentsInvalid("native target spelling is ambiguous")
        binding = self.registry.get(path)
        contents = regular_contents(root)
        if binding is None or (content_digest(contents[path]) if path in contents else None) != binding.content_digest:
            raise TraceAdmissionDenied("target lacks exact current source admission")
        maximum = self._validate_size(tool, args, contents.get(path))
        if maximum is not None and sum(map(len, contents.values())) - len(contents.get(path, b"")) + maximum > 4194304:
            raise TraceArgumentsInvalid("native mutation exceeds candidate total limit")
        return path, binding

    @staticmethod
    def _validate_size(tool, args, previous):
        if tool == "read":
            if previous is None:
                raise TraceArgumentsInvalid("read target is not a projected file")
            return
        if tool == "write":
            maximum = len(args["content"].encode())
        else:
            if previous is None:
                raise TraceArgumentsInvalid("edit target is not a projected file")
            edits = args.get("edits", [{"oldText": args.get("oldText"), "newText": args.get("newText")}])
            if any(not edit["oldText"] for edit in edits):
                raise TraceArgumentsInvalid("edit requires nonempty old text")
            maximum = len(previous) + sum(len(edit["newText"].encode()) for edit in edits)
        if maximum > 1048576:
            raise TraceArgumentsInvalid("native mutation exceeds candidate file limit")
        return maximum

    def end(self, record: dict, root: Path):
        if self._pending is None or (record["toolCallId"], record["toolName"]) != self._pending[:2]:
            raise TraceArgumentsInvalid("unmatched native tool end")
        if self._authorized is None:
            raise TraceAdmissionDenied("native completion has no exact execution admission")
        after = {name: content_digest(data) for name, data in regular_contents(root).items()}
        changed = {name for name in self._before.keys() | after.keys() if self._before.get(name) != after.get(name)}
        allowed = {self._target_path} if self._pending[1] in {"write", "edit"} else set()
        if not changed <= allowed:
            raise TraceAdmissionDenied("native call changed an unadmitted target")
        receipt = self._authorized
        self._pending = None
        self._authorized = None
        return DigestReference(reference="artifact:native-tool-admission", digest=receipt.fingerprint)
