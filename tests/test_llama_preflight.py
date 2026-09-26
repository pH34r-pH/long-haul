"""Real local fixture processes, not llama.cpp/model or hardware qualification."""
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from long_haul.adapters import llama_cpp as m
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import ExecutionRequest, FailureClass, ValidationDepth, ValidationState

pytestmark = pytest.mark.skipif(os.name != "posix", reason="declared Linux/POSIX runtime probe")


@pytest.fixture
def executable(tmp_path):
    def make(body):
        path = tmp_path / "fixture cli"
        path.write_text(f"#!{sys.executable}\n" + body + "\n")
        path.chmod(0o700)
        return path
    return make


def profile(tmp_path):
    model = tmp_path / "fixture.gguf"
    model.write_bytes(b"fixture, not a real GGUF")
    return InferenceProfile(
        id="fixture", runtime_id="llama.cpp", strategy="resident",
        artifact=ModelArtifact(foundation="synthetic"), participating_resources=["test-cpu"],
        options={"model_path": str(model)},
    )


def test_success_retains_identity_fingerprint_and_preflight_depth(executable, tmp_path):
    binary = executable("print('fixture version 1.0')")
    adapter = m.LlamaCppAdapter(binary)
    result = adapter.validate(profile(tmp_path))
    assert result.state is ValidationState.SUPPORTED
    assert result.depth is ValidationDepth.PREFLIGHT
    assert result.provenance == "probe"
    assert result.runtime.version == "fixture version 1.0"
    assert result.runtime.build_id == hashlib.sha256(binary.read_bytes()).hexdigest()[:16]
    assert result.runtime.capabilities["identity_probe"] == "ready"
    assert "execution remain unverified" in result.rationale


def test_stderr_version_is_supported(executable):
    binary = executable("import sys; print('fixture version 1.0', file=sys.stderr)")
    assert m.LlamaCppAdapter(binary).identity().version == "fixture version 1.0"


@pytest.mark.parametrize(("body", "status"), [
    ("import sys; print('DO_NOT_RETAIN'); sys.exit(3)", "unusable"),
    ("pass", "unknown-version"),
    ("import os; os.write(1, b'\\xff')", "unknown-version"),
    ("print('bad\\x00banner')", "unknown-version"),
    ("import os; os.write(1, b'x' * 20000)", "output-limit"),
    ("import os; os.write(2, b'x' * 20000)", "output-limit"),
])
def test_failed_probes_are_unknown_not_supported(executable, tmp_path, body, status):
    adapter = m.LlamaCppAdapter(executable(body))
    result = adapter.validate(profile(tmp_path))
    assert result.state is ValidationState.UNKNOWN
    assert result.runtime.capabilities["identity_probe"] == status
    assert result.runtime.capabilities["resident"] is False
    assert result.runtime.version is None
    assert result.runtime.build_id is None
    assert not result.runtime.backends
    assert "DO_NOT_RETAIN" not in result.model_dump_json()


def test_timeout_is_bounded(executable, tmp_path):
    adapter = m.LlamaCppAdapter(executable("import time; time.sleep(10)"), identity_timeout_seconds=0.2)
    start = time.monotonic()
    result = adapter.validate(profile(tmp_path))
    assert time.monotonic() - start < 3
    assert result.runtime.capabilities["identity_probe"] == "timeout"
    assert result.state is ValidationState.UNKNOWN


def test_closed_pipe_but_running_process_also_times_out(executable):
    body = "import os, time; os.close(1); os.close(2); time.sleep(10)"
    adapter = m.LlamaCppAdapter(executable(body), identity_timeout_seconds=0.2)
    assert adapter.identity().capabilities["identity_probe"] == "timeout"


def test_child_holding_output_is_killed_with_its_group(executable, tmp_path):
    pid_file = tmp_path / "child.pid"
    body = (
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])\n"
        f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "print('fixture version 1.0')"
    )
    adapter = m.LlamaCppAdapter(executable(body), identity_timeout_seconds=2.0)
    assert adapter.identity().capabilities["identity_probe"] == "timeout"
    child_pid = int(pid_file.read_text())
    # A killed child can remain a zombie until the OS reaps it; it must not run.
    for _ in range(30):
        state_file = Path(f"/proc/{child_pid}/stat")
        if not state_file.exists() or state_file.read_text().split(") ", 1)[1].startswith("Z"):
            break
        time.sleep(0.02)
    else:
        pytest.fail("probe child is still running")


def test_relative_binary_is_not_a_path_search(executable, tmp_path, monkeypatch):
    binary = executable("print('fixture version 1.0')")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "/path/that/does/not/exist")
    adapter = m.LlamaCppAdapter(Path(binary.name))
    assert adapter.identity().capabilities["identity_probe"] == "ready"
    assert adapter.binary == binary


def test_missing_binary_is_unknown(tmp_path):
    result = m.LlamaCppAdapter(tmp_path / "missing").validate(profile(tmp_path))
    assert result.state is ValidationState.UNKNOWN
    assert result.runtime.capabilities["identity_probe"] == "missing"


def test_non_executable_binary_is_denied(executable, tmp_path):
    binary = executable("print('fixture version 1.0')")
    binary.chmod(0o600)
    result = m.LlamaCppAdapter(binary).validate(profile(tmp_path))
    assert result.state is ValidationState.UNKNOWN
    assert result.runtime.capabilities["identity_probe"] == "denied"


def test_unusable_executable_does_not_crash(executable):
    binary = executable("pass")
    binary.write_bytes(b"not an executable image")
    assert m.LlamaCppAdapter(binary).identity().capabilities["identity_probe"] == "unusable"


def test_directory_is_not_a_runtime(tmp_path):
    assert m.LlamaCppAdapter(tmp_path).identity().capabilities["identity_probe"] == "unusable"


def test_changed_binary_cannot_receive_a_stale_build_identity(executable):
    binary = executable("with open(__file__, 'a') as f: f.write('# changed\\n')\nprint('version')")
    result = m.LlamaCppAdapter(binary).identity()
    assert result.capabilities["identity_probe"] == "binary-changed"
    assert result.build_id is None


def test_hashing_does_not_read_entire_binary_into_memory(executable):
    binary = executable("print('fixture version 1.0')")
    with patch.object(Path, "read_bytes", side_effect=AssertionError("must stream")):
        assert m.LlamaCppAdapter(binary).identity().capabilities["identity_probe"] == "ready"


def test_binary_size_limit_is_checked_before_execution(executable):
    binary = executable("print('fixture version 1.0')")
    with patch.object(m, "MAX_BINARY_BYTES", 1), patch.object(m, "_version_probe") as probe:
        assert m.LlamaCppAdapter(binary).identity().capabilities["identity_probe"] == "binary-size-limit"
    probe.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 6, True])
def test_invalid_probe_timeout_rejected(tmp_path, timeout):
    with pytest.raises(ValueError):
        m.LlamaCppAdapter(tmp_path / "binary", identity_timeout_seconds=timeout)


def test_failed_identity_never_launches_inference(executable, tmp_path):
    adapter = m.LlamaCppAdapter(executable("import sys; sys.exit(1)"))
    request = ExecutionRequest(request_id="test", profile=profile(tmp_path), prompt="not executed")
    with patch.object(m.subprocess, "run", side_effect=AssertionError("inference must not run")):
        result = adapter.execute(request)
    assert result.success is False
    assert result.error_class is FailureClass.RUNTIME


def test_successful_fixture_can_reach_existing_execution_path(executable, tmp_path):
    binary = executable("import sys\nprint('fixture version 1.0' if '--version' in sys.argv else 'fixture output')")
    result = m.LlamaCppAdapter(binary).execute(
        ExecutionRequest(request_id="test", profile=profile(tmp_path), prompt="fixture prompt")
    )
    assert result.success is True
    assert result.output == "fixture output"


def test_missing_model_remains_unsupported(executable, tmp_path):
    p = profile(tmp_path)
    Path(p.options["model_path"]).unlink()
    result = m.LlamaCppAdapter(executable("print('version')")).validate(p)
    assert result.state is ValidationState.UNSUPPORTED


def test_different_runtime_and_strategy_remain_unsupported(executable, tmp_path):
    adapter = m.LlamaCppAdapter(executable("print('version')"))
    p = profile(tmp_path)
    for field, value in (("runtime_id", "other"), ("strategy", "pipeline")):
        result = adapter.validate(p.model_copy(update={field: value}))
        assert result.state is ValidationState.UNSUPPORTED


def test_native_build_and_runtime_declarations_are_separate():
    declaration = json.loads((Path(__file__).resolve().parents[1] / ".fleet/requirements.json").read_text())
    build = declaration["profiles"]["llama-cpp-build"]
    runtime = declaration["profiles"]["llama-cpp-cpu-runtime"]
    assert build["phases"] == ["build"]
    assert runtime["phases"] == ["runtime"]
    assert {"cmake", "ninja", "cc", "c++"} <= set(build["tools"])
    assert set(runtime["tools"]) == {"python", "llama-cli"}
    assert not {"cuda", "nvcc", "vulkan", "metal"} & set(build["tools"])
    declared = {item["path"] for item in declaration["manifests"]}
    for selection in (build, runtime):
        for need in selection["tools"].values():
            assert set(need["sources"]) <= declared
