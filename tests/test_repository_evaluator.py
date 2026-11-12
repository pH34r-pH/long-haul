"""Hermetic evaluator semantics; fixture adapter never claims real isolation."""
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from repository_evaluator_fixture import git, reference, setup

from long_haul.resource_policy import (
    PolicyRule,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from long_haul.work import WorkContract
from long_haul.work.repository_candidate import freeze_repository_candidate
from long_haul.work.repository_evaluation_models import (
    FrozenRepositoryCandidate,
    RepositoryCheckEvidence,
    RepositoryEvaluationProfileV1,
    RepositoryEvidencePacketV1,
    SandboxExecutionReceipt,
    evaluate_repository_packet,
)
from long_haul.work.repository_evaluator import (
    RepositoryEvaluationRunner,
    repository_evidence_event,
)
from long_haul.work.repository_evidence_admission import (
    PublicEvidenceAdmission,
    evidence_packet_digest,
)
from long_haul.work.repository_models import DigestReference, content_digest
from long_haul.work.repository_sandbox import (
    PYTHON_BOOTSTRAP,
    START_MARKER,
    DockerGVisorSandbox,
    SandboxResult,
    _bounded_output,
    _startup_receipt,
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
        facts = SandboxExecutionReceipt(
            container_id="f" * 64, environment_fingerprint=self.environment.fingerprint,
            outcome=self.outcome, cleanup="removed", process_stopped=True,
            startup_verified=True,
            stdout_digest=content_digest(output), stderr_digest=content_digest(b""), exit_code=0,
            provenance="synthetic-fixture",
        )
        return SandboxResult(self.outcome, output, b"", 0,
                             DigestReference(reference="urn:long-haul:sandbox:" + facts.container_id,
                                             digest=facts.fingerprint), facts)


def run_fixture(tmp_path, **options):
    root, work, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    sandbox = FixtureSandbox(profile.environment, **options)
    packet, result = RepositoryEvaluationRunner(sandbox).evaluate(
        work, snapshot, profile, attempt_id="attempt-1", packet_id="packet-1",
    )
    return root, work, snapshot, profile, sandbox, packet, result


def test_evaluator_materializes_only_under_owned_scratch(tmp_path):
    _, work, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    scratch = tmp_path / "owned-scratch"
    scratch.mkdir(mode=0o700)

    class ScratchSandbox(FixtureSandbox):
        def execute(self, target, command):
            assert target.parent == scratch
            return super().execute(target, command)

    sandbox = ScratchSandbox(profile.environment)
    packet, _ = RepositoryEvaluationRunner(sandbox, scratch_root=scratch).evaluate(
        work, snapshot, profile, attempt_id="attempt-1", packet_id="packet-1",
    )
    assert packet.validity == "valid" and sandbox.calls
    assert list(scratch.iterdir()) == []


def public_evidence_admission(work, profile, packet):
    policy = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:public-packet", resource_type="public-evidence",
                                  content_digest=evidence_packet_digest(packet)),
        lifecycle="active", policy_id="urn:fixture:public-packet-policy", policy_assigner="urn:fixture:controller",
        permissions=[PolicyRule(rule_id="public", actions=["distribute"], purposes=["public-evaluator-evidence"],
                                principal_ids=[profile.evaluator_id], contract_ids=[work.contract_id])],
    )
    request = ResourceUseRequest(
        request_id="public-evidence:" + packet.evidence.attempt_id, resource_id=policy.identity.resource_id,
        action="distribute", purpose="public-evaluator-evidence", caller_id=profile.evaluator_id,
        contract_id=work.contract_id, mission_id=work.mission_id,
        repository_id=work.repository_work.source.repository_id, now=datetime.now(UTC) - timedelta(seconds=1),
    )
    return PublicEvidenceAdmission(policy, policy.fingerprint, request, request.fingerprint)


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
    from long_haul.events import Event, EventStore
    history = EventStore(tmp_path / "events.jsonl")
    # Historical V1 records remain readable without authorizing a new disclosure.
    historical = Event(event_type="work_evidence", payload={"repository_evidence": packet.model_dump(mode="json")})
    history.path.write_text(historical.model_dump_json() + "\n")
    assert RepositoryEvidencePacketV1.model_validate(history.events()[0].payload["repository_evidence"]) == packet
    with pytest.raises(TypeError, match="explicit public disclosure admission"):
        repository_evidence_event(packet)
    admission = public_evidence_admission(work, profile, packet)
    history.append(repository_evidence_event(packet, contract=work, candidate=snapshot.candidate,
                                             profile=profile, admission=admission))
    assert RepositoryEvidencePacketV1.model_validate(history.events()[1].payload["repository_evidence"]) == packet
    revoked = admission.policy.model_copy(update={"lifecycle": ResourceLifecycle.RETRACTED})
    with pytest.raises(PermissionError, match="exact current disclosure admission"):
        repository_evidence_event(packet, contract=work, candidate=snapshot.candidate, profile=profile,
                                  admission=replace(admission, policy=revoked))


def test_legacy_public_event_requires_current_exact_packet_distribution_policy(tmp_path):
    _, work, snapshot, profile, _, packet, _ = run_fixture(tmp_path)
    admission = public_evidence_admission(work, profile, packet)
    changed = packet.model_copy(update={"producing_attempt_references": (reference("another-attempt"),)})
    with pytest.raises(PermissionError, match="exact current disclosure admission"):
        repository_evidence_event(changed, contract=work, candidate=snapshot.candidate,
                                  profile=profile, admission=admission)
    revoked = admission.policy.model_copy(update={"lifecycle": ResourceLifecycle.RETRACTED})
    admission = replace(admission, policy=revoked, expected_policy_fingerprint=revoked.fingerprint)
    with pytest.raises(PermissionError, match="distribution is not currently allowed"):
        repository_evidence_event(packet, contract=work, candidate=snapshot.candidate,
                                  profile=profile, admission=admission)


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
    (tmp_path / "program.py").write_text("print(3)\n")

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


def test_pass_cannot_wrap_invalid_sandbox_outcome(tmp_path):
    *_, packet, _ = run_fixture(tmp_path)
    observed = packet.checks[0]
    facts = SandboxExecutionReceipt(container_id="d" * 64, environment_fingerprint=packet.evaluator_environment.digest,
                                    outcome="invalid", cleanup="removed", process_stopped=True,
                                    stdout_digest=observed.stdout_digest, stderr_digest=observed.stderr_digest,
                                    exit_code=observed.exit_code)
    value = observed.model_dump()
    value["sandbox_execution"] = facts.model_dump()
    value["sandbox_receipt"] = {"reference": "urn:long-haul:sandbox:" + facts.container_id, "digest": facts.fingerprint}
    with pytest.raises(ValidationError, match="outcome"):
        RepositoryCheckEvidence.model_validate(value)


def test_wrong_observed_output_cannot_be_declared_pass(tmp_path):
    _, work, snapshot, profile, _, packet, _ = run_fixture(tmp_path)
    value = packet.model_dump()
    observed = value["checks"][0]
    observed["stdout_digest"] = content_digest(b"wrong output")
    observed["sandbox_execution"]["stdout_digest"] = observed["stdout_digest"]
    facts = SandboxExecutionReceipt.model_validate(observed["sandbox_execution"])
    observed["sandbox_receipt"]["digest"] = facts.fingerprint
    coherent_but_wrong = RepositoryEvidencePacketV1.model_validate(value)
    with pytest.raises(ValueError, match="expected behavior"):
        evaluate_repository_packet(work, coherent_but_wrong, snapshot.candidate, profile)


def test_exported_candidate_paths_are_unique_and_safe(tmp_path):
    _, _, snapshot, _, _, _, _ = run_fixture(tmp_path)
    value = snapshot.candidate.model_dump()
    value["files"] = (*value["files"], value["files"][0])
    with pytest.raises(ValidationError, match="unique"):
        FrozenRepositoryCandidate.model_validate(value)
    value = snapshot.candidate.model_dump()
    value["changed_paths"] = ("../outside",)
    with pytest.raises(ValidationError, match="repository-relative"):
        FrozenRepositoryCandidate.model_validate(value)
    value = snapshot.candidate.model_dump()
    value["source"]["visibility"] = "private" if value["base"]["visibility"] == "public" else "public"
    with pytest.raises(ValidationError, match="same repository and classification"):
        FrozenRepositoryCandidate.model_validate(value)


def test_unverified_candidate_cannot_spoof_trusted_startup_marker(tmp_path):
    script = tmp_path / "program.py"
    script.write_text("import sys;sys.stderr.write('LONG_HAUL_SUT_STARTED_V1\\n');print('forged')")
    process = subprocess.run([sys.executable, "-I", "-S", "-c", PYTHON_BOOTSTRAP, str(script), "0" * 64],
                             capture_output=True, check=False)
    assert process.returncode != 0 and process.stdout == b"" and not process.stderr.startswith(START_MARKER)


def test_output_flood_cannot_starve_startup_proof(tmp_path):
    import time

    from long_haul.work.repository_evaluation_models import SandboxLimits

    script = tmp_path / "program.py"
    script.write_text("import sys;assert sys.stdin.read()=='input';sys.stdout.write('x'*1000000)")
    digest = content_digest(script.read_bytes()).split(":", 1)[1]
    process = subprocess.Popen([sys.executable, "-I", "-S", "-c", PYTHON_BOOTSTRAP, str(script), digest],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert _startup_receipt(process, time.monotonic() + 5) == START_MARKER
        outcome, stdout, stderr = _bounded_output(process, b"\0input", SandboxLimits(max_output_bytes=128))
        assert outcome == "output_limit" and len(stdout) + len(stderr) <= 128
    finally:
        process.kill()
        process.communicate()


@pytest.mark.parametrize("still_running,expected", [(False, "output_limit"), (True, "invalid")])
def test_failed_kill_requires_observed_stopped_state(tmp_path, monkeypatch, still_running, expected):
    from contextlib import contextmanager

    from repository_evaluator_fixture import environment

    from long_haul.work.repository_evaluation_models import CandidateCommand

    adapter = DockerGVisorSandbox.__new__(DockerGVisorSandbox)
    adapter.environment = environment()

    @contextmanager
    def attached(_):
        yield None

    def control(*args):
        if args[0] == "kill":
            raise subprocess.CalledProcessError(1, ["docker", "kill"])
        return json.dumps([{"State": {"Running": still_running, "ExitCode": 0}}])

    monkeypatch.setattr(adapter, "_attached_process", attached)
    monkeypatch.setattr(adapter, "_control", control)
    monkeypatch.setattr("long_haul.work.repository_sandbox._startup_receipt", lambda *args: START_MARKER)
    monkeypatch.setattr("long_haul.work.repository_sandbox._bounded_output", lambda *args, **kw: ("output_limit", b"x", b""))
    command = CandidateCommand(program="program.py", expected_stdout_digest=content_digest(b""))
    assert adapter._execute("f" * 64, command).outcome == expected


def test_docker_controls_ignore_ambient_credentials_and_proxy_config(tmp_path, monkeypatch):
    poisoned = tmp_path / "poisoned"
    poisoned.mkdir()
    (poisoned / "config.json").write_text(json.dumps({"proxies": {"default": {"httpProxy": "secret"}}}))
    monkeypatch.setenv("DOCKER_CONFIG", str(poisoned))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HTTP_PROXY", "secret")
    monkeypatch.setenv("LONG_HAUL_SECRET", "secret")
    client = tmp_path / "docker"
    client.write_text("#!" + sys.executable + "\n" +
                      "import json,os,pathlib,sys\n" +
                      "assert sys.argv[1] == '--config'\n" +
                      "assert list(pathlib.Path(sys.argv[2]).iterdir()) == []\n" +
                      "assert not any(k in os.environ for k in ['DOCKER_CONFIG','HTTP_PROXY','LONG_HAUL_SECRET'])\n" +
                      "print(json.dumps({'args':sys.argv[3:]}))\n")
    client.chmod(0o755)
    adapter = DockerGVisorSandbox.__new__(DockerGVisorSandbox)
    adapter.docker_path = client
    assert json.loads(adapter._control("info")) == {"args": ["info"]}
    with adapter._attached_process("f" * 64) as process:
        stdout, stderr = process.communicate()
        assert process.returncode == 0 and stderr == b""
        assert json.loads(stdout) == {"args": ["start", "--attach", "--interactive", "f" * 64]}
    adapter.image_environment = {"PATH": "/usr/local/bin:/usr/bin"}
    expected = ["PATH=/usr/local/bin:/usr/bin", "PYTHONDONTWRITEBYTECODE=1", "PYTHONHASHSEED=0"]
    adapter._verify_environment(expected)
    for injected in ("HTTP_PROXY=secret", "PYTHONHASHSEED=0"):
        with pytest.raises(ValueError, match="environment differs"):
            adapter._verify_environment([*expected, injected])


def test_gvisor_declines_profiles_below_qualified_runtime_minimum(tmp_path):
    from repository_evaluator_fixture import environment

    profile = environment()
    profile = profile.model_copy(update={"limits": profile.limits.model_copy(update={"pids": 32})})
    with pytest.raises(ValueError, match="at least 128 host tasks"):
        DockerGVisorSandbox(profile, tmp_path / "absent-runtime")
