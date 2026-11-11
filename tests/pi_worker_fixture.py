"""Public actual-Pi conformance setup; a synthetic provider is not an LLM."""
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from repository_evaluator_fixture import (
    contract,
    environment,
    profile,
    reference,
    repository,
)

from long_haul.adapters.harness_trace import (
    TraceNormalizationPolicy,
    TraceNormalizer,
    TraceUseAdmission,
)
from long_haul.adapters.pi_admission import WorkerResourceAdmission
from long_haul.adapters.pi_assets import PiRuntimeAssets
from long_haul.adapters.pi_rpc import pi_prompt
from long_haul.adapters.pi_worker import (
    DockerGVisorPiWorker,
    PiWorkerEnvironment,
    PiWorkerRequest,
)
from long_haul.resource_policy import (
    PolicyRule,
    ResourceIdentity,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from long_haul.work import WorkSession
from long_haul.work.harness import harness_attempt
from long_haul.work.repository_candidate import freeze_repository_candidate
from long_haul.work.repository_models import DigestReference, content_digest
from long_haul.work.repository_sandbox import DockerGVisorSandbox

PLAN = {"mode": "success", "actions": [
    {"tool": "read", "args": {"path": "program.py"}},
    {"tool": "edit", "args": {"path": "program.py", "edits": [{"oldText": "a - b", "newText": "a + b"}]}},
]}


def installed_assets():
    receipt_path = Path(os.environ["LONG_HAUL_PI_RECEIPT"])
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["audit_exit_code"] == 0 and receipt["lifecycle_scripts"] == "disabled"
    return PiRuntimeAssets(Path(os.environ["LONG_HAUL_PI_RUNTIME"]), receipt["runtime_tree"],
                           DigestReference(reference="artifact:pi-acquisition", digest=content_digest(receipt_path.read_bytes())))


def worker_setup(tmp_path, plan, *, seconds=20):
    assets = installed_assets()
    inherited = environment().model_dump()
    for key in ("image", "candidate_access", "toolchain"):
        inherited.pop(key)
    inherited["limits"]["wall_seconds"] = seconds
    inherited["limits"]["max_output_bytes"] = 262144
    worker = DockerGVisorPiWorker(PiWorkerEnvironment(**inherited), Path(os.environ["LONG_HAUL_RUNSC"]), assets)
    root, base = repository(tmp_path)
    evaluator = profile(DockerGVisorSandbox.implementation_reference())
    work = contract(root, base, evaluator)
    work.budget.max_attempts = 2
    work.repository_work = work.repository_work.model_copy(update={
        "environment": DigestReference(reference="urn:fixture:pi-environment", digest=worker.environment.fingerprint),
        "toolchain": assets.toolchain_reference(),
    })
    snapshot = freeze_repository_candidate(root, base, work)
    session = WorkSession(contract_id=work.contract_id, crew_id="fixture-crew", session_id="fixture-session")
    attempt = harness_attempt(work, session, worker.harness_identity(plan), "real-pi-fixture")
    now = datetime.now(UTC)
    trace = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:trace", resource_type="operational-trace"),
        authority_class="authoritative-control", lifecycle="active", policy_id="urn:fixture:trace-policy",
        policy_assigner="urn:fixture:operator", permissions=[PolicyRule(rule_id="trace", actions=["annotate"],
            purposes=["operational-trace"], crew_ids=[session.crew_id], contract_ids=[work.contract_id])],
    )
    request = ResourceUseRequest(request_id="trace-request", resource_id=trace.identity.resource_id,
        action="annotate", purpose="operational-trace", caller_id=session.session_id, crew_id=session.crew_id,
        mission_id=work.mission_id, contract_id=work.contract_id, repository_id=attempt.repository_id, now=now)
    normalizer = TraceNormalizer(work, attempt, TraceNormalizationPolicy(key_reference=reference("external-key")),
        b"synthetic-public-qualification-only-key-256-bits", TraceUseAdmission(trace, trace.fingerprint, request))
    digest = content_digest(snapshot.contents["program.py"])
    resource = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:program", resource_type="repository-file", content_digest=digest),
        authority_class="repository-reference", lifecycle="active", policy_id="urn:fixture:file-policy",
        policy_assigner="urn:fixture:operator", permissions=[PolicyRule(rule_id="file", actions=["read", "ai-augment", "derive"],
            purposes=["repository-work"], crew_ids=[session.crew_id], contract_ids=[work.contract_id])],
    )
    registry = {"program.py": WorkerResourceAdmission(resource, resource.fingerprint, digest)}
    prompt = lambda current: pi_prompt(work, session=session, evaluation_time=current)
    return worker, PiWorkerRequest(root, snapshot, normalizer, registry, prompt, plan), evaluator
