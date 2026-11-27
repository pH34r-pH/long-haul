"""Synthetic process/parser checks; no weights, inference or Fleet qualification."""
import base64
import os
import struct
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from long_haul.experiments import qwen_cpu
from long_haul.experiments._bounded_process import (
    ProcessLimits,
    ProcessOutcome,
    run_process,
)
from long_haul.experiments.qwen_cpu import (
    RawEngineSpec,
    TokenCase,
    parse_output,
    run_raw_engine,
)
from long_haul.models import InferenceProfile, ModelArtifact


def limits(**changes):
    values = {"wall_seconds": 2, "cpu_seconds": 2, "address_space_bytes": 256 * 1024**2,
                  "max_output_bytes": 1024, "cpus": [min(os.sched_getaffinity(0))]}
    return ProcessLimits(**(values | changes))


def case(**changes):
    return TokenCase(**({"id": "synthetic", "prompt": "fixture", "prompt_ids": [1, 2],
                            "expected_ids": [3, 4], "max_tokens": 64} | changes))


def output(stderr=b"PROMPT_TOKENS: 1,2\nGEN_TOKENS: 3,4\n", **changes):
    return ProcessOutcome(**({"outcome": "completed", "wall_seconds": .1, "exit_code": 0,
                                 "stderr_base64": base64.b64encode(stderr).decode(),
                                 "leader_reaped": True} | changes))


def profile(spec):
    return InferenceProfile(
        id="fixture-profile", runtime_id="qwen3-raw-hex-opus", strategy="resident",
        artifact=ModelArtifact(foundation="synthetic-no-model"),
        participating_resources=["fixture-cpu"],
        options={"binary_sha256": qwen_cpu.ENGINE_SHA256["opus"],
                 "model_sha256": spec.model_sha256, "threads": 1},
    )


@pytest.mark.parametrize("field,value", [
    ("prompt_ids", [True]), ("prompt_ids", ["1"]), ("expected_ids", [-1]),
    ("expected_ids", [151936]), ("max_tokens", True), ("prompt", "nul\0byte"),
    ("prompt", "x" * 16001), ("prompt_ids", [1] * 960),
])
def test_invalid_case_rejected_before_execution(field, value):
    with pytest.raises(ValidationError):
        case(**{field: value})


@pytest.mark.parametrize("values", [[True], ["1"], [-1], [1, 1], []])
def test_cpu_placement_is_explicit_and_strict(values):
    with pytest.raises(ValidationError):
        limits(cpus=values)


def test_parse_tokens_and_keep_reported_rates_separate():
    ids, generated, rates = parse_output(
        b"PROMPT_TOKENS: 1,2,\nGEN_TOKENS: 3,4\n"
        b"[engine] prefill: 2 tokens in 1.00 s (2.00 tok/s)\n"
        b"[engine] decode : 1 steps in 0.10 s (10.00 tok/s, threads=1)\n", 64)
    assert ids == [1, 2] and generated == [3, 4]
    assert rates == {"prefill": 2, "decode": 10}


@pytest.mark.parametrize("stderr", [
    b"PROMPT_TOKENS: 1,2\n",  # missing generated IDs
    b"PROMPT_TOKENS: 1,2\nPROMPT_TOKENS: 1,2\nGEN_TOKENS: 3\n",
    b"PROMPT_TOKENS: 1,,2\nGEN_TOKENS: 3\n",
    b"PROMPT_TOKENS: -1\nGEN_TOKENS: 3\n",
    b"PROMPT_TOKENS: 1\nGEN_TOKENS: 151936\n",
    b"PROMPT_TOKENS: 1\nGEN_TOKENS: 3,4,5\n",
    b"PROMPT_TOKENS: 1\nGEN_TOKENS: 3\ndecode : (nan tok/s)\n",
    b"PROMPT_TOKENS: 1\nGEN_TOKENS: 3\ndecode : (inf tok/s)\n",
    b"PROMPT_TOKENS: 1\nGEN_TOKENS: 3\ndecode : (-1 tok/s)\n",
    b"PROMPT_TOKENS: 1\nGEN_TOKENS: 3\ndecode : (1 tok/s)\ndecode : (2 tok/s)\n",
    b"\xffPROMPT_TOKENS: 1\nGEN_TOKENS: 3\n",
])
def test_malformed_or_ambiguous_evidence_rejected(stderr):
    with pytest.raises((ValueError, UnicodeError)):
        parse_output(stderr, 2)


def synthetic_run(monkeypatch, tmp_path, process):
    spec = RawEngineSpec(kind="opus", binary=tmp_path / "fixture-engine",
                         model=tmp_path / "fixture-model", model_sha256="a" * 64)
    spec.binary.write_bytes(b"0" * qwen_cpu.ENGINE_BYTES["opus"])
    calls = []
    monkeypatch.setattr(qwen_cpu, "_environment", lambda *_: {"provenance": "simulated"})
    monkeypatch.setattr(qwen_cpu, "_digest", lambda _: qwen_cpu.ENGINE_SHA256["opus"])
    monkeypatch.setattr(qwen_cpu, "_check_model", lambda _: None)
    monkeypatch.setattr(qwen_cpu, "run_process", lambda argv, budget: calls.append(argv) or process)
    bound = profile(spec)
    result = run_raw_engine(spec, case(), limits(), bound, provenance="simulated")
    return result, calls, bound


def test_exact_match_requires_complete_vectors_not_matching_prefix(monkeypatch, tmp_path):
    result, _, _ = synthetic_run(monkeypatch, tmp_path, output(b"PROMPT_TOKENS: 1,2\nGEN_TOKENS: 3,4,5\n"))
    assert result.outcome == "correctness_failed" and result.exact_tokens_match is False
    assert result.observation.error == "correctness_failed"
    assert result.scientific_acceptance == "not-assessed"


def test_success_binds_actual_runtime_and_never_promotes_engine_timer(monkeypatch, tmp_path):
    raw = b"PROMPT_TOKENS: 1,2\nGEN_TOKENS: 3,4\ndecode : (999.0 tok/s)\n"
    result, calls, original = synthetic_run(monkeypatch, tmp_path, output(raw))
    assert result.outcome == "completed" and result.exact_tokens_match
    assert result.engine_reported_tps == {"decode": 999}
    assert result.observation.decode_tps is None and result.observation.prefill_tps is None
    assert result.observation.provenance == "simulated"
    assert result.runtime.runtime_id == "qwen3-raw-hex-opus"
    assert result.observation.runtime == result.runtime
    assert result.observation.profile.options["process_limits"]["cpus"] == list(limits().cpus)
    assert "process_limits" not in original.options
    assert calls[0][-5:] == ["-n", "64", "-t", "1", "--print-tokens"]
    assert result.process.stderr == raw


@pytest.mark.parametrize("outcome", ["timeout", "output_limit", "nonzero_exit", "cleanup_failed"])
def test_process_failure_cannot_become_correctness_success(monkeypatch, tmp_path, outcome):
    result, _, _ = synthetic_run(monkeypatch, tmp_path, output(outcome=outcome, exit_code=-9))
    assert result.outcome == outcome and result.exact_tokens_match is None
    assert result.observation.error == outcome and result.observation.decode_tps is None


def test_binary_digest_rejection_does_not_launch_or_create_measurement(monkeypatch, tmp_path):
    binary = tmp_path / "not-the-engine"
    binary.write_bytes(b"0" * qwen_cpu.ENGINE_BYTES["opus"])
    spec = RawEngineSpec(kind="opus", binary=binary, model=tmp_path / "absent", model_sha256="a" * 64)
    monkeypatch.setattr(qwen_cpu, "_environment", lambda *_: {})
    result = run_raw_engine(spec, case(), limits(), profile(spec))
    assert result.outcome == "preflight_rejected" and "digest" in result.diagnostic
    assert result.process is None and result.observation is None


def test_false_llama_identity_rejected(tmp_path):
    spec = RawEngineSpec(kind="opus", binary=tmp_path / "engine", model=tmp_path / "model", model_sha256="a" * 64)
    bound = profile(spec)
    bound.runtime_id = "llama.cpp"
    with pytest.raises(ValueError, match="exact raw runtime"):
        run_raw_engine(spec, case(), limits(), bound)


@pytest.mark.parametrize("quota", ["50000 100000", "max 0", "-1 100000", "0 100000", "garbage"])
def test_partial_or_invalid_quota_cannot_claim_one_full_cpu(quota):
    with pytest.raises(ValueError):
        qwen_cpu._check_cpu_quota(quota, 1)


def test_cpu_flags_belong_to_each_selected_cpu():
    cpuinfo = "processor : 0\nflags : avx2 fma f16c\n\nprocessor : 2\nflags : avx2\n"
    assert qwen_cpu._flags_for_cpus(cpuinfo, (2,)) == [{"avx2"}]
    assert qwen_cpu._flags_for_cpus(cpuinfo, (3,)) == []


@pytest.mark.parametrize("offset", [8, 40, 60, 64, 104])
def test_unsupported_asmbin_header_rejected_before_model_hash(monkeypatch, tmp_path, offset):
    header = bytearray(128)
    struct.pack_into("<4s9I", header, 0, b"ASML", 2, 1024, 3072, 28, 16, 8, 128, 151936, 4096)
    struct.pack_into("<6I", header, 40, 0x358637bd, 0x49742400, 151645, 151643, 524287, 7)
    struct.pack_into("<6Q", header, 64, 128, 9973952, 175280320, 16720896, 643465408, 643469504)
    header[offset] ^= 1
    path = tmp_path / "sparse-format-fixture"
    with path.open("wb") as stream:
        stream.write(header)
        stream.truncate(643469504)  # sparse zeros, no model data or inference
    spec = RawEngineSpec(kind="opus", binary=tmp_path / "absent", model=path, model_sha256="a" * 64)
    def unexpected_hash(_):
        pytest.fail("invalid model header must fail before reading full weights")
    monkeypatch.setattr(qwen_cpu, "_digest", unexpected_hash)
    with pytest.raises(ValueError):
        qwen_cpu._check_model(spec)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process pilot boundary")
def test_real_fixture_process_environment_affinity_and_return_code(monkeypatch):
    monkeypatch.setenv("QWEN_TEST_DO_NOT_FORWARD", "synthetic-secret")
    program = "import os; print(os.getenv('QWEN_TEST_DO_NOT_FORWARD')); print(sorted(os.sched_getaffinity(0))); raise SystemExit(7)"
    result = run_process([sys.executable, "-I", "-S", "-c", program], limits())
    assert result.outcome == "nonzero_exit" and result.exit_code == 7
    assert result.stdout.decode().splitlines() == ["None", str(list(limits().cpus))]
    assert result.leader_reaped and result.wall_seconds > 0


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process pilot boundary")
def test_unbounded_output_is_cut_off_and_process_reaped():
    program = "import os;\nwhile True: os.write(1,b'x'*4096); os.write(2,b'y'*4096)"
    result = run_process([sys.executable, "-I", "-S", "-c", program], limits(max_output_bytes=512))
    assert result.outcome == "output_limit" and result.leader_reaped
    assert len(result.stdout) + len(result.stderr) == 512


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process pilot boundary")
def test_timeout_kills_group_including_pipe_holding_descendant():
    program = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(30)']);"
               "print(p.pid,flush=True); time.sleep(30)")
    result = run_process([sys.executable, "-I", "-S", "-c", program], limits(wall_seconds=.3))
    assert result.outcome == "timeout" and result.group_kill_sent and result.leader_reaped
    assert result.wall_seconds < 2
    child = int(result.stdout)
    status = Path(f"/proc/{child}/stat")
    # The outer PID1/container owns orphan reaping; SIGKILL must stop its work.
    deadline = time.monotonic() + 1
    while status.exists():
        try:
            state = status.read_text().split(") ", 1)[1]
        except FileNotFoundError:
            break
        if state.startswith("Z "):
            break
        assert time.monotonic() < deadline, "descendant still executing after group kill"
        time.sleep(.01)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process pilot boundary")
def test_address_space_limit_rejects_oversized_allocation():
    result = run_process([sys.executable, "-I", "-S", "-c", "bytearray(512*1024*1024)"],
                         limits(address_space_bytes=64 * 1024**2))
    assert result.outcome == "nonzero_exit" and b"MemoryError" in result.stderr
    assert result.leader_reaped
