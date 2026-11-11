"""Required hosted-CI qualification; absence never certifies real isolation."""
import json
import os
import subprocess
from pathlib import Path

import pytest
from repository_evaluator_fixture import environment, git, setup

from long_haul.work.repository_evaluation_models import CandidateCommand
from long_haul.work.repository_evaluator import RepositoryEvaluationRunner
from long_haul.work.repository_models import content_digest
from long_haul.work.repository_sandbox import DockerGVisorSandbox


@pytest.fixture
def sandbox():
    required = os.environ.get("LONG_HAUL_REQUIRE_GVISOR") == "1"
    if not required:
        pytest.skip("real gVisor qualification runs in the strict hosted CI job")
    runsc = Path(os.environ["LONG_HAUL_RUNSC"])
    return DockerGVisorSandbox(environment(), runsc)


def synthetic_diagnostics(result):
    # Only these public synthetic probes may expose their bounded startup errors.
    return {"outcome": result.outcome, "exit_code": result.exit_code,
            "synthetic_stderr_prefix": result.stderr[:512].decode("utf-8", errors="replace"),
            "execution": result.execution_receipt.model_dump() if result.execution_receipt else None}


def test_real_public_sealed_readonly_candidate_and_redacted_packet(tmp_path, sandbox):
    _, work, snapshot, profile = setup(tmp_path, sandbox.identity)
    runner = RepositoryEvaluationRunner(sandbox)
    packet, result = runner.evaluate(work, snapshot, profile, attempt_id="real-1", packet_id="real-1")
    assert result.disposition.value == "ACCEPTED" and packet.validity == "valid"
    assert "[-5, 2]" not in packet.model_dump_json()
    assert all(check.sandbox_receipt is not None for check in packet.checks if check.classification in {"PUBLIC", "SEALED"})
    print(json.dumps({"qualification": "real-gvisor", "candidate": snapshot.candidate.fingerprint,
                      "profile": profile.fingerprint, "environment": profile.environment.fingerprint,
                      "packet_fingerprint": packet.fingerprint, "packet": packet.model_dump(mode="json"),
                      "disposition": result.disposition.value}))


def test_worker_workspace_and_candidate_cannot_see_sealed_or_control_material(tmp_path, sandbox, monkeypatch):
    root, work, _, _ = setup(tmp_path, sandbox.identity)
    sentinel = tmp_path / "control-plane-private.txt"
    sentinel.write_text("synthetic-not-a-secret")
    monkeypatch.setenv("LONG_HAUL_SYNTHETIC_CREDENTIAL", "must-not-enter")
    program = (
        "import os, pathlib, socket\n"
        f"assert not pathlib.Path({str(sentinel)!r}).exists()\n"
        "assert not pathlib.Path('/sealed').exists()\n"
        "assert 'LONG_HAUL_SYNTHETIC_CREDENTIAL' not in os.environ\n"
        "try:\n    pathlib.Path('/candidate/program.py').write_text('tamper')\n"
        "except OSError: pass\nelse: raise AssertionError('candidate writable')\n"
        "s=socket.socket();s.settimeout(.2)\n"
        "try:\n    s.connect(('192.0.2.1', 9))\n"
        "except OSError: pass\nelse: raise AssertionError('external network available')\n"
        "pathlib.Path('/tmp/usable').write_text('scratch')\nprint('isolated')\n"
    )
    (root / "program.py").write_text(program)
    git(root, "commit", "-qam", "isolated probe")
    from long_haul.work.repository_candidate import freeze_repository_candidate
    snapshot = freeze_repository_candidate(root, git(root, "rev-parse", "HEAD"), work)
    with snapshot.materialize() as target:
        result = sandbox.execute(target, CandidateCommand(program="program.py", expected_stdout_digest=content_digest(b"isolated\n")))
    assert result.outcome == "completed" and result.exit_code == 0 and result.stdout == b"isolated\n", synthetic_diagnostics(result)
    assert sentinel.read_text() == "synthetic-not-a-secret"
    assert result.receipt is not None
    print(json.dumps({"qualification": "readonly-network-secrets", "receipt": result.execution_receipt.model_dump()}))


@pytest.mark.parametrize("program,outcome", [
    ("import time;time.sleep(60)\n", "timeout"),
    ("import sys;sys.stdout.write('x'*1000000)\n", "output_limit"),
])
def test_timeout_output_limit_and_forced_teardown(tmp_path, sandbox, program, outcome):
    root, work, _, _ = setup(tmp_path, sandbox.identity)
    (root / "program.py").write_text(program)
    git(root, "commit", "-qam", "bounded probe")
    from long_haul.work.repository_candidate import freeze_repository_candidate
    snapshot = freeze_repository_candidate(root, git(root, "rev-parse", "HEAD"), work)
    with snapshot.materialize() as target:
        result = sandbox.execute(target, CandidateCommand(program="program.py", expected_stdout_digest=content_digest(b"")))
    assert result.outcome == outcome and result.receipt is not None, synthetic_diagnostics(result)
    assert result.execution_receipt.startup_verified
    assert len(result.stdout) + len(result.stderr) <= sandbox.environment.limits.max_output_bytes
    container = result.receipt.reference.rsplit(":", 1)[1]
    check = subprocess.run(["/usr/bin/docker", "inspect", container], capture_output=True, check=False)
    assert check.returncode != 0
    print(json.dumps({"qualification": outcome + "-cleanup", "receipt": result.execution_receipt.model_dump()}))


def test_candidate_cannot_find_sealed_marker_or_shadow_trusted_python(tmp_path, sandbox):
    from long_haul.work.repository_candidate import freeze_repository_candidate

    root, work, _, profile = setup(tmp_path, sandbox.identity)
    (root / "program.py").write_text(
        "import os\nprint(3 if os.path.exists('/sealed') or 'LONG_HAUL_REQUIRE_GVISOR' in os.environ else 999)\n"
    )
    (root / "python3").write_text("print(3)\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "adversarial candidate")
    work = work.model_copy(update={"allowed_paths": ["program.py", "python3"]})
    snapshot = freeze_repository_candidate(root, git(root, "rev-parse", "HEAD"), work)
    packet, result = RepositoryEvaluationRunner(sandbox).evaluate(
        work, snapshot, profile, attempt_id="adversarial", packet_id="adversarial",
    )
    assert result.disposition.value == "REJECTED" and packet.validity == "valid"
    assert next(c for c in packet.checks if c.predicate_id == "public").state.value == "FAIL"
    print(json.dumps({"qualification": "marker-and-tool-shadow-rejection", "packet": packet.fingerprint}))
