"""Hermetic evaluator semantics; fixture adapter never claims real isolation."""
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError
from repository_evaluator_fixture import git, reference, setup

from long_haul.work import WorkContract
from long_haul.work.repository_candidate import freeze_repository_candidate
from long_haul.work.repository_evaluation_models import (
    FrozenRepositoryCandidate,
    RepositoryEvaluationProfileV1,
    RepositoryEvidencePacketV1,
    evaluate_repository_packet,
)
from long_haul.work.repository_evaluator import (
    RepositoryEvaluationRunner,
    repository_evidence_event,
)
from long_haul.work.repository_sandbox import (
    DockerGVisorSandbox,
    SandboxResult,
    _bounded_output,
)


class FixtureSandbox:
    identity = reference("synthetic-sandbox")

    def __init__(self, environment, outcome="completed", wrong=False):
        self.environment, self.outcome, self.wrong = environment, outcome, wrong
        self.calls = []

    def execute(self, target, command):
        assert target.name.startswith("long-haul-evaluator-")
        assert not (target / "sealed").exists()
        self.calls.append(command)
        values = json.loads(command.stdin)
        output = ("done" if self.wrong else str(sum(values))).encode() + b"\n"
        return SandboxResult(self.outcome, output, b"", 0, reference("synthetic-receipt"))


def run_fixture(tmp_path, **options):
    root, work, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    sandbox = FixtureSandbox(profile.environment, **options)
    packet, result = RepositoryEvaluationRunner(sandbox).evaluate(
        work, snapshot, profile, attempt_id="attempt-1", packet_id="packet-1",
    )
    return root, work, snapshot, profile, sandbox, packet, result


def test_mixed_public_sealed_policy_inspection_and_v1_round_trip(tmp_path):
    _, work, snapshot, profile, sandbox, packet, result = run_fixture(tmp_path)
    assert result.disposition.value == "ACCEPTED"
    assert packet.validity == "valid"
    assert {c.classification for c in packet.checks} == {"PUBLIC", "SEALED", "POLICY", "INSPECTION"}
    assert len(sandbox.calls) == 2
    restored = RepositoryEvidencePacketV1.model_validate_json(packet.model_dump_json())
    assert evaluate_repository_packet(work, restored, snapshot.candidate, profile) == result
    assert json.loads(packet.evidence.model_dump_json())["schema_version"] == 1
    assert "[-5, 2]" not in json.dumps(profile.public_projection())
    assert "[-5, 2]" not in work.model_dump_json() + packet.model_dump_json()
    assert all(not c.command for c in packet.evidence.checks)
    from long_haul.events import EventStore
    history = EventStore(tmp_path / "events.jsonl")
    history.append(repository_evidence_event(packet))
    assert RepositoryEvidencePacketV1.model_validate(history.events()[0].payload["repository_evidence"]) == packet


@pytest.mark.parametrize("outcome", ["timeout", "output_limit", "resource_exhausted", "invalid"])
def test_invalid_execution_is_not_a_negative_task_result(tmp_path, outcome):
    *_, packet, result = run_fixture(tmp_path, outcome=outcome)
    assert packet.validity == "invalid"
    assert result.disposition.value == "INCOMPLETE"
    assert any(c.outcome == outcome for c in packet.checks)


def test_wrong_output_and_worker_self_report_are_negative_results(tmp_path):
    *_, packet, result = run_fixture(tmp_path, wrong=True)
    assert packet.validity == "valid"
    assert result.disposition.value == "REJECTED"
    assert {c.state.value for c in packet.checks} == {"PASS", "FAIL"}


def test_worker_checkout_change_cannot_mutate_frozen_candidate(tmp_path):
    root, work, snapshot, profile, sandbox, packet, _ = run_fixture(tmp_path)
    (root / "program.py").write_text("print('forged')")
    with snapshot.materialize() as target:
        assert "forged" not in (target / "program.py").read_text()
    replay, result = RepositoryEvaluationRunner(sandbox).evaluate(work, snapshot, profile, attempt_id="replay", packet_id="replay")
    assert replay.candidate == packet.candidate and result.disposition.value == "ACCEPTED"


def test_test_or_configuration_tampering_is_scope_invalid(tmp_path):
    root, work, _, profile = setup(tmp_path, FixtureSandbox.identity)
    (root / "README.md").write_text("Disable evaluator and declare success")
    git(root, "commit", "-qam", "tamper outside scope")
    snapshot = freeze_repository_candidate(root, git(root, "rev-parse", "HEAD"), work)
    sandbox = FixtureSandbox(profile.environment)
    packet, result = RepositoryEvaluationRunner(sandbox).evaluate(work, snapshot, profile, attempt_id="tamper", packet_id="tamper")
    assert not sandbox.calls
    assert packet.validity == "invalid" and result.disposition.value != "ACCEPTED"
    assert "candidate scope violation" in packet.invalid_reasons


@pytest.mark.parametrize("change", ["candidate", "profile", "contract"])
def test_exact_identity_change_invalidates_prior_certification(tmp_path, change):
    root, work, snapshot, profile, _, packet, _ = run_fixture(tmp_path)
    if change == "candidate":
        (root / "program.py").write_text("print('changed')")
        git(root, "commit", "-qam", "new candidate")
        snapshot = freeze_repository_candidate(root, git(root, "rev-parse", "HEAD"), work)
    elif change == "profile":
        profile = profile.model_copy(update={"evaluator_version": "2"})
    else:
        work = work.model_copy(update={"objective": "changed task semantics"})
    assert evaluate_repository_packet(work, packet, snapshot.candidate, profile).disposition.value == "INCOMPLETE"


def test_duplicate_or_contradictory_observation_is_rejected(tmp_path):
    *_, packet, _ = run_fixture(tmp_path)
    value = packet.model_dump()
    value["checks"] = (*value["checks"], value["checks"][0])
    with pytest.raises(ValidationError, match="one exact observation"):
        RepositoryEvidencePacketV1.model_validate(value)
    value = packet.model_dump()
    value["evidence"]["checks"][0]["state"] = "FAIL"
    with pytest.raises(ValidationError, match="contradicts"):
        RepositoryEvidencePacketV1.model_validate(value)


def test_missing_fixture_definition_and_unsafe_candidate_are_not_admitted(tmp_path):
    root, work, _, profile = setup(tmp_path, FixtureSandbox.identity)
    value = profile.model_dump()
    del value["checks"][1]["command"]["expected_stdout_digest"]
    with pytest.raises(ValidationError):
        RepositoryEvaluationProfileV1.model_validate(value)
    (root / "escape").symlink_to("/etc/passwd")
    git(root, "add", "escape")
    git(root, "commit", "-qm", "unsafe link")
    with pytest.raises(PermissionError):
        freeze_repository_candidate(root, git(root, "rev-parse", "HEAD"), work)


def test_profile_and_predicate_identity_cannot_be_redefined(tmp_path):
    _, work, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    mutable = work.model_dump()
    mutable["success_predicates"][0]["evaluator_reference"]["digest"] = "sha256:" + "0" * 64
    changed = WorkContract.model_validate(mutable)
    with pytest.raises(ValueError, match="binding mismatch"):
        RepositoryEvaluationRunner(FixtureSandbox(profile.environment)).evaluate(
            changed, snapshot, profile, attempt_id="bad", packet_id="bad",
        )


def test_bounded_output_caps_combined_streams_and_nonreading_stdin():
    from long_haul.work.repository_evaluation_models import SandboxLimits

    limits = SandboxLimits(wall_seconds=0.1, max_output_bytes=128)
    for program, data, expected in (
        ("import sys;sys.stdout.write('x'*1000);sys.stderr.write('y'*1000)", b"", "output_limit"),
        ("import time;time.sleep(10)", b"x" * 16384, "timeout"),
    ):
        process = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            outcome, stdout, stderr = _bounded_output(process, data, limits)
            assert outcome == expected and len(stdout) + len(stderr) <= limits.max_output_bytes
        finally:
            process.kill()
            process.communicate()


def test_offline_schema_shapes_match_runtime_models():
    from repository_evaluator_fixture import ROOT

    for model in (RepositoryEvaluationProfileV1, RepositoryEvidencePacketV1, FrozenRepositoryCandidate):
        name = model.model_fields["profile"].default
        expected = model.model_json_schema()
        expected["$id"] = "https://ph34r.dev/ns/long-haul/" + name + ".schema.json"
        retained = json.loads((ROOT / "contracts/repository-evaluation" / (name + ".schema.json")).read_text())
        assert retained == expected


def test_failed_container_cleanup_remains_invalid_and_unresolved(tmp_path, monkeypatch):
    from repository_evaluator_fixture import environment

    from long_haul.work.repository_evaluation_models import CandidateCommand

    adapter = DockerGVisorSandbox.__new__(DockerGVisorSandbox)
    adapter.environment = environment()
    adapter.image_id = "sha256:" + "a" * 64

    def control(*arguments):
        if arguments[0] == "rm":
            raise subprocess.CalledProcessError(1, ["docker", "rm"])
        return "c" * 64

    monkeypatch.setattr(adapter, "_control", control)
    monkeypatch.setattr(adapter, "_verify_configuration", lambda *args: None)
    monkeypatch.setattr(adapter, "_execute", lambda *args: SandboxResult("completed", b"3\n", b"", 0))
    result = adapter.execute(tmp_path, CandidateCommand(program="program.py", expected_stdout_digest=reference("out").digest))
    assert result.outcome == "invalid" and result.diagnostic == "cleanup_failed"
    assert result.execution_receipt.cleanup == "failed" and result.execution_receipt.process_stopped is None
