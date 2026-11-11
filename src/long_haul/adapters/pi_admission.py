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
    ResourceIdentity,
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
class WorkerPolicyBinding:
    profile: ResourcePolicyProfile
    expected_policy_fingerprint: str
    purpose: str = "repository-work"


@dataclass(frozen=True)
class WorkerPromptAdmission:
    """Caller attests these parents cover the entire exact task/context view."""
    parents: tuple[WorkerPolicyBinding, ...]
    content_fingerprint: str
    context_manifest: DigestReference
    task_digest: DigestReference | None = None


@dataclass(frozen=True)
class WorkerResourceAdmission:
    profile: ResourcePolicyProfile
    expected_policy_fingerprint: str
    content_digest: str | None
    purpose: str = "repository-work"
    output_policy: WorkerPolicyBinding | None = None
    derived: DerivedResourceBinding | None = None


@dataclass(frozen=True)
class DerivedResourceBinding:
    identity: ResourceIdentity
    parents: tuple[WorkerPolicyBinding, ...]
    operation: DigestReference

    @property
    def content_digest(self):
        return self.identity.content_digest


class ToolAdmissionReceipt(FrozenProjection):
    attempt_id: str
    session_id: str
    native_id: str
    operation_fingerprint: str
    policies: tuple[DigestReference, ...]
    resource_fingerprint: str


class PiToolAdmission:
    def __init__(self, normalizer: TraceNormalizer, registry: dict[str, WorkerResourceAdmission]):
        self.normalizer = normalizer
        self.registry = registry  # Authenticated caller may revoke a live profile.
        self._pending = None
        self._authorized = None
        self._seen_ui: set[str] = set()
        self.receipts: list[ToolAdmissionReceipt] = []
        self.derived = {path: binding.derived for path, binding in registry.items() if binding.derived is not None}
        self.derivations: list[dict] = []
        self._inputs: dict[str, WorkerPolicyBinding] = {}
        self._roots = {path: (binding.expected_policy_fingerprint, binding.content_digest,
                              binding.output_policy.expected_policy_fingerprint if binding.output_policy else None)
                       for path, binding in registry.items()}

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

    def _policy(self, parents, action, now, *, request_class="tool"):
        attempt = self.attempt
        for parent in parents:
            if parent.profile.fingerprint != parent.expected_policy_fingerprint or parent.purpose != "repository-work":
                raise TraceAdmissionDenied("worker parent policy or purpose changed")
        request = {"request_id": attempt.attempt_id + ":" + request_class + ":" + str(len(self.receipts)) + ":" + action.value,
                   "caller_id": attempt.session_id, "contract_id": attempt.contract_id,
                   "mission_id": attempt.mission_id, "crew_id": attempt.crew_id,
                   "repository_id": attempt.repository_id, "action": action, "purpose": "repository-work", "now": now}
        decision = ResourcePolicyEvaluator().evaluate_all(
            [parent.profile for parent in parents], lambda resource: ResourceUseRequest(resource_id=resource, **request),
        )
        if decision.status is not PolicyDecisionStatus.ALLOW:
            raise TraceAdmissionDenied("current shared resource policy denies native file use")
        return DigestReference(reference="artifact:worker-policy-decision", digest=content_digest(
            decision.model_dump_json().encode(),
        ))

    def admit_prompt(self, admission, fingerprint, now):
        if (admission is None or not admission.parents or admission.content_fingerprint != fingerprint
                or admission.context_manifest != self.attempt.context_manifest
                or admission.task_digest != self.attempt.digest):
            raise TraceAdmissionDenied("exact prompt/context view lacks authenticated complete-source admission")
        receipts = tuple(self._policy(admission.parents, action, now, request_class="input")
                         for action in (ResourceAction.READ, ResourceAction.AI_AUGMENT))
        self._inputs.update({parent.profile.identity.resource_id: parent for parent in admission.parents})
        return receipts

    def authorize(self, record: dict, root: Path, now: datetime) -> ToolAdmissionReceipt:
        self._exact_contract()
        request = self._guard_request(record)
        tool, args = request["toolName"], request["args"]
        _, _, normalized, _ = self.normalizer.normalize(tool, args, now)
        binding_id = (request["toolCallId"], tool, normalized.argument_fingerprint)
        if self._pending != binding_id or tool not in {"read", "write", "edit"}:
            raise TraceArgumentsInvalid("native interlock does not match exact pending operation")
        path, binding = self._target(tool, args, root)
        parents = self._parents(binding)
        actions = (ResourceAction.DERIVE,) if tool == "write" else (ResourceAction.READ, ResourceAction.AI_AUGMENT)
        if tool == "edit":
            actions += (ResourceAction.DERIVE,)
        if tool in {"write", "edit"}:
            parents = self._derivation_parents(path, parents)
        policies = tuple(self._policy(parents, action, now) for action in actions)
        receipt = ToolAdmissionReceipt(
            attempt_id=self.attempt.attempt_id, session_id=self.attempt.session_id,
            native_id=self.normalizer.native_id(request["toolCallId"])[0],
            operation_fingerprint=normalized.argument_fingerprint, policies=policies,
            resource_fingerprint=self.normalizer.fingerprint("worker-resource", self._identity(binding).model_dump(mode="json")),
        )
        self.receipts.append(receipt)
        self._authorized = receipt
        self._target_path = path
        self._before = {name: content_digest(data) for name, data in regular_contents(root).items()}
        self._operation_parents = parents
        if tool in {"read", "edit"}:
            self._inputs.update({parent.profile.identity.resource_id: parent for parent in parents})
        return receipt

    @staticmethod
    def _identity(binding):
        return binding.identity if isinstance(binding, DerivedResourceBinding) else binding.profile.identity

    @staticmethod
    def _parents(binding):
        if isinstance(binding, DerivedResourceBinding):
            return binding.parents
        return (WorkerPolicyBinding(binding.profile, binding.expected_policy_fingerprint, binding.purpose),)

    def _check_roots(self):
        for path, expected in self._roots.items():
            root = self.registry.get(path)
            actual = None if root is None else (root.profile.fingerprint, root.content_digest,
                root.output_policy.profile.fingerprint if root.output_policy else None)
            if actual != expected:
                raise TraceAdmissionDenied("authenticated source or output authority changed")

    def _derivation_parents(self, path, parents):
        output = self.registry[path].output_policy
        if output is None:
            raise TraceAdmissionDenied("mutation requires explicit caller-admitted output policy")
        unique = {}
        for parent in (*parents, *self._inputs.values(), output):
            resource = parent.profile.identity.resource_id
            if resource in unique and unique[resource].expected_policy_fingerprint != parent.expected_policy_fingerprint:
                raise TraceAdmissionDenied("conflicting parent identity")
            unique[resource] = parent
        return tuple(unique[key] for key in sorted(unique))

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
        self._check_roots()
        binding = self.derived.get(path, self.registry.get(path))
        contents = regular_contents(root)
        if binding is None or (content_digest(contents[path]) if path in contents else None) != binding.content_digest:
            raise TraceAdmissionDenied("target lacks exact current source admission")
        if isinstance(binding, WorkerResourceAdmission) and binding.profile.identity.content_digest != binding.content_digest:
            raise TraceAdmissionDenied("source digest differs from its authenticated resource identity")
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

    def end(self, record: dict, root: Path, now: datetime | None = None):
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
        result = DigestReference(reference="artifact:native-tool-admission", digest=receipt.fingerprint)
        if changed:
            if now is None:
                raise TraceAdmissionDenied("derivation requires current authenticated completion time")
            result = self._derive(after[self._target_path], result, now)
        self._pending = None
        self._authorized = None
        return result

    def _derive(self, digest, operation, now):
        self._exact_contract()
        self._check_roots()
        decision = self._policy(self._operation_parents, ResourceAction.DERIVE, now)
        identity = ResourceIdentity(resource_id="urn:long-haul:derived:" + content_digest(
            (self.attempt.fingerprint + operation.digest + digest).encode(),
        ).removeprefix("sha256:"), resource_type="derived-repository-file", content_digest=digest)
        self.derived[self._target_path] = DerivedResourceBinding(identity, self._operation_parents, operation)
        projection = {"attempt_id": self.attempt.attempt_id, "session_id": self.attempt.session_id,
                      "operation": operation.model_dump(), "decision": decision.model_dump(),
                      "input_resource_fingerprint": self._authorized.resource_fingerprint,
                      "resource_fingerprint": self.normalizer.fingerprint("worker-resource", identity.model_dump(mode="json")),
                      "parents": [parent.expected_policy_fingerprint for parent in self._operation_parents]}
        self.derivations.append(projection)
        return DigestReference(reference="artifact:worker-derivation", digest=content_digest(
            json.dumps(projection, sort_keys=True).encode(),
        ))
