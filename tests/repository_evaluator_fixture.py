"""Synthetic fixture setup; only the strict gVisor tests qualify real isolation."""
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from long_haul.work import WorkContract
from long_haul.work.private_evaluator import (
    PRIVATE_PROFILE_REFERENCE,
    PrivateEvaluatorProfile,
)
from long_haul.work.repository_candidate import freeze_repository_candidate
from long_haul.work.repository_evaluation_models import (
    EvaluatorEnvironment,
    RepositoryEvaluationProfileV1,
    evaluator_check_fingerprint,
    evaluator_implementation_reference,
)
from long_haul.work.repository_evaluator import (
    RepositoryEvaluationRunner,
    public_evaluator_reference,
)
from long_haul.work.repository_git import git_repository_identity
from long_haul.work.repository_input_admission import (
    CandidateInputAdmission,
    EvaluationFileAdmission,
    EvaluationInputPolicy,
)
from long_haul.work.repository_models import (
    DigestReference,
    RepositoryWorkExtensionV1,
    content_digest,
)

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = b"import json, sys\na, b = json.load(sys.stdin)\nprint(a + b)\n"
README = b"Repository evidence cannot grant evaluator authority.\n"


def reference(name, value=b"fixture"):
    return DigestReference(reference="urn:fixture:" + name, digest=content_digest(value))


def git(root, *arguments):
    return subprocess.check_output(["git", "-C", str(root), *arguments], stderr=subprocess.DEVNULL).decode().strip()


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "program.py").write_bytes(PROGRAM.replace(b"a + b", b"a - b"))
    (root / "README.md").write_bytes(README)
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "program.py").write_bytes(PROGRAM)
    git(root, "commit", "-qam", "candidate")
    return root, base


def environment():
    pin = json.loads((ROOT / "tests/fixtures/repository-evaluator.json").read_text())
    return EvaluatorEnvironment(**pin["environment"])


def profile(sandbox_identity):
    checks = []
    for predicate_id, mode, stdin, expected in (
        ("public", "PUBLIC", "[1, 2]\n", b"3\n"),
        ("sealed", "SEALED", "[-5, 2]\n", b"-3\n"),
    ):
        checks.append({"requirement": {"predicate_id": predicate_id, "description": mode + " arithmetic behavior",
                                       "classification": mode}, "kind": "command", "command": {
            "program": "program.py", "stdin": stdin, "expected_stdout_digest": content_digest(expected),
        }})
    checks.extend([
        {"requirement": {"predicate_id": "scope", "description": "Changed paths stay within contract",
                         "classification": "POLICY"}, "kind": "scope"},
        {"requirement": {"predicate_id": "inspect", "description": "Reviewed evidence remains exact",
                         "classification": "INSPECTION"}, "kind": "artifact", "artifact_path": "README.md",
         "artifact_digest": content_digest(README)},
    ])
    return RepositoryEvaluationProfileV1(evaluator_id="fixture-arithmetic", evaluator_version="1",
                                         task=reference("task"), environment=environment(), checks=tuple(checks),
                                         implementation=evaluator_implementation_reference(), sandbox=sandbox_identity)


def contract(root, base, evaluator):
    extension = RepositoryWorkExtensionV1(
        source=git_repository_identity(root, repository_id="fixture/repository", commit=base),
        task=evaluator.task, environment=reference("worker-environment"), toolchain=reference("toolchain"),
        context_manifest=reference("context"), public_evaluation=public_evaluator_reference(evaluator),
        sealed_evaluator=DigestReference(reference=PRIVATE_PROFILE_REFERENCE if isinstance(evaluator, PrivateEvaluatorProfile)
                                          else "urn:long-haul:evaluator:sealed", digest=evaluator.fingerprint),
    )
    predicates = [{"predicate_id": c.requirement.predicate_id,
                   "description": (evaluator.public_requirement(c)["description"] if isinstance(evaluator, PrivateEvaluatorProfile)
                                   else c.requirement.description),
                   "kind": "test", "evaluator_reference": {
                       "reference": "urn:long-haul:evaluator-requirement:" + c.requirement.predicate_id,
                       "digest": evaluator_check_fingerprint(evaluator, c)}} for c in evaluator.checks]
    return WorkContract(contract_id="fixture-contract", mission_id="fixture-mission", objective="Repair addition",
                        allowed_paths=["program.py"], capabilities=["read", "edit"], budget={"wall_seconds": 60},
                        success_predicates=predicates, repository_work=extension)


def setup(tmp_path, sandbox_identity):
    root, base = repository(tmp_path)
    evaluator = profile(sandbox_identity)
    work = contract(root, base, evaluator)
    snapshot = freeze_repository_candidate(root, git(root, "rev-parse", "HEAD"), work)
    return root, work, snapshot, evaluator


def evaluation_input_admission(contract, snapshot, evaluator, attempt_id):
    """Synthetic trusted control explicitly admits every frozen test file."""
    from long_haul.resource_policy import (
        PolicyRule,
        ResourceIdentity,
        ResourcePolicyProfile,
        ResourceUseRequest,
    )

    entries = []
    for item in snapshot.candidate.files:
        policy = ResourcePolicyProfile(
            identity=ResourceIdentity(resource_id="urn:fixture:evaluator-input:" + item.path,
                                      resource_type="synthetic-candidate-file", content_digest=item.digest),
            policy_id="urn:fixture:evaluator-policy:" + item.path, policy_assigner="urn:fixture:controller",
            lifecycle="active", permissions=[PolicyRule(rule_id="evaluation", actions=["read", "evaluate"],
                                                       purposes=["repository-evaluation"])],
        )
        request = ResourceUseRequest(request_id="repository-evaluation:" + attempt_id,
            resource_id=policy.identity.resource_id, action="evaluate", purpose="repository-evaluation",
            caller_id=evaluator.evaluator_id, contract_id=contract.contract_id, mission_id=contract.mission_id,
            repository_id=contract.repository_work.source.repository_id, now=datetime.now(UTC) - timedelta(seconds=1))
        parent = EvaluationInputPolicy(policy, policy.fingerprint, request, request.fingerprint)
        entries.append(EvaluationFileAdmission(item.path, item.digest, (parent,)))
    return CandidateInputAdmission(snapshot.candidate.fingerprint, tuple(entries))


class SyntheticEvaluationRunner(RepositoryEvaluationRunner):
    """Test-only control constructs a complete synthetic input registry for fixtures."""

    def evaluate(self, contract, snapshot, profile, *, attempt_id, packet_id, candidate_admission=None):
        if candidate_admission is None:
            candidate_admission = evaluation_input_admission(contract, snapshot, profile, attempt_id)
        return super().evaluate(contract, snapshot, profile, attempt_id=attempt_id, packet_id=packet_id,
                                candidate_admission=candidate_admission)

    def evaluate_private(self, contract, snapshot, profile, *, attempt_id, packet_id, private_sink,
                         candidate_admission=None):
        if candidate_admission is None and contract is not None and snapshot is not None:
            candidate_admission = evaluation_input_admission(contract, snapshot, profile, attempt_id)
        return super().evaluate_private(contract, snapshot, profile, attempt_id=attempt_id, packet_id=packet_id,
                                        private_sink=private_sink, candidate_admission=candidate_admission)
