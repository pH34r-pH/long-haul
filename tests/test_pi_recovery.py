"""Unconfirmed removal retains private recovery state and denies reuse."""
import json
import subprocess
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pi_worker_fixture import admit_prompt, public_evaluator_admission
from repository_evaluator_fixture import profile
from test_harness_trajectory import reference
from test_pi_worker import setup_guard

from long_haul.adapters.harness_trace import TraceNormalizer
from long_haul.adapters.pi_harness import PiCodingHarness
from long_haul.adapters.pi_recovery import PiWorkerCleanupRequired, PiWorkerRecovery
from long_haul.adapters.pi_worker import DockerGVisorPiWorker, PiWorkerRequest
from long_haul.work import AcceptancePredicate
from long_haul.work.harness import harness_attempt
from long_haul.work.repository_evaluator import public_evaluator_reference
from long_haul.work.repository_models import DigestReference


def fake_executor(tmp_path):
    contract, _, old, _, registry, _ = setup_guard(tmp_path)
    contract.capabilities = ["read", "edit"]
    evaluator = profile(reference("synthetic-evaluator"))
    contract.repository_work = contract.repository_work.model_copy(update={
        "task": evaluator.task, "public_evaluation": public_evaluator_reference(evaluator),
        "sealed_evaluator": DigestReference(reference="urn:long-haul:evaluator:sealed", digest=evaluator.fingerprint),
    })
    contract.success_predicates = [AcceptancePredicate(predicate_id=check.requirement.predicate_id, kind="test",
        description=check.requirement.description, evaluator_reference=DigestReference(
            reference="urn:long-haul:evaluator-requirement:" + check.requirement.predicate_id,
            digest=check.fingerprint)) for check in evaluator.checks]
    from long_haul.work import WorkSession
    session = WorkSession(contract_id=contract.contract_id, session_id=old.attempt.session_id, crew_id=old.attempt.crew_id)
    attempt = harness_attempt(contract, session, old.attempt.harness, "cleanup-negative")
    normalizer = TraceNormalizer(contract, attempt, old.policy, b"synthetic-recovery-external-key-256-bits", old.admission)
    worker = object.__new__(DockerGVisorPiWorker)
    worker.environment = SimpleNamespace(fingerprint=contract.repository_work.environment.digest)
    worker.identity = reference("fake-control")
    worker.assets = SimpleNamespace(verify=lambda: None, toolchain_reference=lambda: contract.repository_work.toolchain)
    worker.harness_identity = lambda _: attempt.harness
    worker._cleanup_pending, worker.recovery_path, worker._control_root = False, None, None
    worker._create_arguments = lambda _: ["create"]
    worker._verify_configuration = lambda *_: None
    worker._attached_process = lambda *_: nullcontext(SimpleNamespace())
    worker.fail_removal = True
    def control(*arguments):
        if arguments[0] == "rm" and worker.fail_removal:
            raise subprocess.CalledProcessError(1, ["docker", "rm"])
        if arguments[:2] == ("container", "ls"):
            return ""
        return "c" * 64 if arguments[0] == "create" else json.dumps([{"State": {"Running": True}}])
    worker._control = control
    def drive(process, normalizer, registry, root, prompt, cancel):
        harness = PiCodingHarness(normalizer, registry, root, lambda _: None, lambda: None, prompt)
        harness.projector.state.settle("settled")
        return harness
    worker._drive = drive
    base = SimpleNamespace(candidate=SimpleNamespace(base=contract.repository_work.source),
                           contents={"fixture/code.py": b"before\n"})
    request = PiWorkerRequest(tmp_path, base, normalizer, registry, lambda _: "task", {}, reference("template"),
                              admit_prompt(normalizer, "task"),
                              evaluator_admission=public_evaluator_admission(evaluator, contract, attempt, datetime.now(UTC)))
    return worker, request


def test_failed_removal_keeps_workspace_control_and_private_identity_until_recovery(tmp_path):
    worker, request = fake_executor(tmp_path)
    with pytest.raises(PiWorkerCleanupRequired) as failure:
        worker.run(request)
    path = failure.value.receipt_path
    receipt = PiWorkerRecovery.model_validate_json(path.read_bytes())
    assert receipt.container_id == "c" * 64 and receipt.attempt == request.normalizer.attempt
    assert receipt.control.exists() and receipt.workspace.exists()
    assert not receipt.lease_releasable and not receipt.workspace_reusable
    assert path.stat().st_mode & 0o777 == 0o600
    assert str(receipt.workspace) not in str(failure.value)
    with pytest.raises(PiWorkerCleanupRequired):
        worker.run(request)
    with pytest.raises(subprocess.CalledProcessError):
        worker.recover()
    assert receipt.workspace.exists() and worker._cleanup_pending
    worker.fail_removal = False
    cleanup = worker.recover()
    assert cleanup.process_tree_stopped and cleanup.attempt_id == receipt.attempt.attempt_id
    assert not receipt.workspace.exists() and not receipt.control.exists()
    assert path.exists() and (path.parent / "cleanup.json").exists()
    assert not worker._cleanup_pending and worker.recovery_path is None


def test_acknowledged_removal_must_confirm_absence_before_recovery_or_reuse(tmp_path):
    worker, request = fake_executor(tmp_path)
    with pytest.raises(PiWorkerCleanupRequired) as failure:
        worker.run(request)
    receipt = PiWorkerRecovery.model_validate_json(failure.value.receipt_path.read_bytes())
    original_control = worker._control
    worker.fail_removal = False

    def control(*args):
        if args[:2] == ("container", "ls"):
            return receipt.container_id + "\n"
        return original_control(*args)

    worker._control = control
    with pytest.raises((ValueError, PiWorkerCleanupRequired)):
        worker.recover()
    assert receipt.workspace.exists() and receipt.control.exists()
    assert worker._cleanup_pending and not worker.lease_releasable
    assert not (failure.value.receipt_path.parent / "cleanup.json").exists()


@pytest.mark.parametrize("field", ["workspace", "control", "container_id"])
def test_mutated_recovery_receipt_cannot_redirect_cleanup(tmp_path, field):
    worker, request = fake_executor(tmp_path)
    with pytest.raises(PiWorkerCleanupRequired) as failure:
        worker.run(request)
    path = failure.value.receipt_path
    original = PiWorkerRecovery.model_validate_json(path.read_bytes())
    decoy = tmp_path / "unrelated-directory"
    decoy.mkdir()
    (decoy / "keep").write_text("synthetic")
    value = "f" * 64 if field == "container_id" else decoy
    path.write_text(original.model_copy(update={field: value}).model_dump_json())
    worker.fail_removal = False
    calls = []
    control = worker._control
    worker._control = lambda *args: (calls.append(args), control(*args))[1]
    with pytest.raises(ValueError, match="recovery"):
        worker.recover()
    assert not calls
    assert (decoy / "keep").read_text() == "synthetic"
    assert original.workspace.exists() and original.control.exists()
    assert worker._cleanup_pending and not worker.lease_releasable


def test_fresh_executor_recovery_requires_independent_expected_record(tmp_path):
    worker, request = fake_executor(tmp_path)
    with pytest.raises(PiWorkerCleanupRequired) as failure:
        worker.run(request)
    path = failure.value.receipt_path
    expected = worker._recovery
    worker._recovery, worker.recovery_path = None, None
    worker.fail_removal = False
    with pytest.raises(TypeError, match="trusted expected record"):
        worker.recover(path)
    assert expected.workspace.exists() and expected.control.exists()
    cleanup = worker.recover(path, expected_recovery=expected)
    assert cleanup.process_tree_stopped and not expected.workspace.exists() and not expected.control.exists()


@pytest.mark.parametrize("census", ["present", "error"])
def test_acknowledged_removal_does_not_allow_unconfirmed_initial_cleanup(tmp_path, census):
    worker, request = fake_executor(tmp_path)
    original_control = worker._control
    worker.fail_removal = False

    def control(*args):
        if args[:2] == ("container", "ls"):
            if census == "error":
                raise subprocess.CalledProcessError(1, args)
            return "c" * 64 + "\n"
        return original_control(*args)

    worker._control = control
    with pytest.raises(PiWorkerCleanupRequired) as failure:
        worker.run(request)
    receipt = PiWorkerRecovery.model_validate_json(failure.value.receipt_path.read_bytes())
    assert receipt.workspace.exists() and receipt.control.exists()
    assert worker._cleanup_pending and not worker.lease_releasable


def test_worker_rejects_unadmitted_sealed_evaluator_before_launch(tmp_path):
    worker, request = fake_executor(tmp_path)
    with pytest.raises(TypeError, match="trusted worker admission"):
        worker.run(replace(request, evaluator_admission=None))
    assert worker.lease_releasable and worker._control_root is None


def test_worker_rejects_missing_sealed_evaluator_before_launch(tmp_path):
    worker, request = fake_executor(tmp_path)
    contract = request.normalizer.contract
    contract.repository_work = contract.repository_work.model_copy(update={"sealed_evaluator": None})
    with pytest.raises(ValueError, match="requires an exact sealed evaluator"):
        worker.run(request)
    assert worker.lease_releasable and worker._control_root is None
