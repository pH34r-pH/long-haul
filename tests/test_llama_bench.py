import os
import sys
from pathlib import Path

import pytest

from long_haul.adapters.llama_bench import LlamaBenchAdapter, LlamaBenchSweep, _command
from long_haul.models import InferenceProfile, ModelArtifact

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fixture executable uses a POSIX shebang")


@pytest.fixture
def bench_binary(tmp_path: Path) -> Path:
    path = tmp_path / "llama-bench"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "rows = [\n"
        "  {'build_commit':'fixture123','build_number':1,'cpu_info':'fixture cpu',"
        "'gpu_info':'fixture gpu','backends':'CUDA','model_type':'fixture',"
        "'model_size':123,'model_n_params':456,'n_batch':2048,'n_ubatch':512,"
        "'n_threads':12,'n_gpu_layers':0,'split_mode':'layer','main_gpu':0,"
        "'devices':'auto','tensor_split':'0.00','n_prompt':512,'n_gen':0,'n_depth':256,"
        "'test_time':'2026-09-28T00:00:00Z','avg_ns':1000,'stddev_ns':20,"
        "'avg_ts':321.5,'stddev_ts':2.5,'samples_ns':[990,1010],"
        "'samples_ts':[320.0,323.0]},\n"
        "  {'build_commit':'fixture123','build_number':1,'cpu_info':'fixture cpu',"
        "'gpu_info':'fixture gpu','backends':'CUDA','model_type':'fixture',"
        "'model_size':123,'model_n_params':456,'n_batch':2048,'n_ubatch':512,"
        "'n_threads':12,'n_gpu_layers':0,'split_mode':'layer','main_gpu':0,"
        "'devices':'auto','tensor_split':'0.00','n_prompt':0,'n_gen':128,'n_depth':256,"
        "'test_time':'2026-09-28T00:00:01Z','avg_ns':2000,'stddev_ns':30,"
        "'avg_ts':42.25,'stddev_ts':0.5,'samples_ns':[1980,2020],"
        "'samples_ts':[42.0,42.5]}\n"
        "]\n"
        "print(json.dumps(rows))\n"
    )
    path.chmod(0o700)
    return path


def profile(strategy: str = "resident", **options) -> InferenceProfile:
    return InferenceProfile(
        id="test",
        runtime_id="llama.cpp",
        strategy=strategy,
        artifact=ModelArtifact(foundation="fixture", quantization="Q4"),
        participating_resources=["ANC-C0"],
        options=options,
    )


def test_native_bench_normalizes_upstream_statistics(bench_binary, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    adapter = LlamaBenchAdapter(bench_binary)
    result = adapter.run(
        profile(),
        str(model),
        LlamaBenchSweep(
            prompt_tokens=[512],
            generation_tokens=[128],
            context_depths=[256],
            repetitions=2,
        ),
        timeout_seconds=5,
    )

    assert result.success is True
    assert len(result.observations) == 2
    pp, tg = result.observations
    assert pp.prefill_tps == pytest.approx(321.5)
    assert pp.decode_tps is None
    assert tg.decode_tps == pytest.approx(42.25)
    assert pp.workload.context_depth_tokens == 256
    assert pp.workload.repetitions == 2
    assert pp.statistics["build_commit"] == "fixture123"
    assert "model_filename" not in pp.statistics


def test_native_bench_command_forces_cpu_and_normalizes_tensor_split(tmp_path):
    sweep = LlamaBenchSweep()
    cpu = _command(tmp_path / "llama-bench", "model.gguf", profile(), sweep)
    assert cpu[cpu.index("-ngl") + 1] == "0"

    dual = profile(
        "multi_gpu",
        gpu_layers=999,
        split_mode="layer",
        tensor_split=[0.75, 0.25],
    )
    command = _command(tmp_path / "llama-bench", "model.gguf", dual, sweep)
    assert command[command.index("-ngl") + 1] == "999"
    assert command[command.index("-ts") + 1] == "0.75/0.25"
