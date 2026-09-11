"""Opt-in real reference run; no ordinary test imports this module."""
from __future__ import annotations

import platform
from pathlib import Path
from uuid import uuid4

from .adapters.llama_cpp import LlamaCppAdapter
from .benchmarks import BenchmarkObservation, BenchmarkStore, Workload
from .discovery.linux import GenericLinuxDiscovery
from .events import Event, EventStore, materialize
from .models import ExecutionMode, InferenceProfile, ModelArtifact
from .runtime import ExecutionRequest, ValidationDepth, ValidationState
from .scheduler import MissionRequirements, Scheduler
from .validation import LayerState, ReferenceReport


def run(binary: str, model: str, output_dir: str) -> ReferenceReport:
    out=Path(output_dir); report=ReferenceReport(environment={"platform":platform.platform()})
    report.record(0,LayerState.PASS,"imports and schemas loaded")
    events=EventStore(out/"events.jsonl"); report.record(1,LayerState.PASS,"append-only event store available")
    vessel=GenericLinuxDiscovery("work-vm","Work VM","station").discover(); report.artifact["vessel"]=vessel.model_dump(); report.record(2,LayerState.PASS,"generic Linux discovery completed")
    artifact=ModelArtifact(foundation="SmolLM2-135M-Instruct-GGUF",quantization="Q4_K_M")
    profile=InferenceProfile(id="work-vm-cpu-resident",runtime_id="llama.cpp",strategy="resident",artifact=artifact,participating_resources=[vessel.resources[0].id],options={"model_path":model})
    adapter=LlamaCppAdapter(binary); validation=adapter.validate(profile); report.artifact["validation"]=validation.model_dump(mode="json")
    if validation.state is not ValidationState.SUPPORTED:
        report.record(3,LayerState.UNSUPPORTED if validation.state is ValidationState.UNSUPPORTED else LayerState.UNKNOWN,validation.rationale); report.save(out/"reference-report.json"); return report
    report.record(3,LayerState.PASS,validation.rationale); report.record(4,LayerState.PASS,"GGUF artifact present")
    request=ExecutionRequest(request_id=str(uuid4()),profile=profile,prompt="Return exactly: LONG_HAUL_OK",max_tokens=12)
    direct=adapter.execute(request)
    if not direct.success:
        report.record(5,LayerState.FAIL_RUNTIME,direct.error_detail or "inference failed"); report.save(out/"reference-report.json"); return report
    validation.depth=ValidationDepth.EXECUTION; validation.provenance="measured"; report.artifact["validation"]=validation.model_dump(mode="json")
    report.record(5,LayerState.PASS,"normalized inference and model load succeeded")
    benchmark=BenchmarkObservation(profile=profile,plan_mode=ExecutionMode.LOCAL,resources=profile.participating_resources,workload=Workload(name="exact-string",cold=True),provenance="measured",load_seconds=direct.timings.load_seconds,ttft_seconds=direct.timings.ttft_seconds,prefill_tps=direct.timings.prefill_tps,decode_tps=direct.timings.decode_tps)
    store=BenchmarkStore(out/"benchmarks.jsonl")
    try:
        store.append(benchmark); queried=store.query(profile.id)
    except (OSError, ValueError) as exc:
        report.record(6,LayerState.FAIL_SYSTEM,f"benchmark persistence/query failed: {exc}"); report.save(out/"reference-report.json"); return report
    if not queried:
        report.record(6,LayerState.FAIL_SYSTEM,"benchmark persistence/query returned no observation"); report.save(out/"reference-report.json"); return report
    validation.depth=ValidationDepth.BENCHMARK; report.artifact["validation"]=validation.model_dump(mode="json")
    report.record(6,LayerState.PASS,"measured benchmark persisted and queried")
    scheduler=Scheduler([vessel],benchmarks=store.query(profile.id),validations=[validation]); selected=scheduler.select(MissionRequirements(id="reference",allow_modes={ExecutionMode.LOCAL}),[profile]); report.artifact["selected_plan"]=selected.plan.model_dump(); report.record(7,LayerState.PASS,"scheduler selected validated LOCAL resident plan")
    scheduled=adapter.execute(ExecutionRequest(request_id=str(uuid4()),profile=profile,prompt="Say hello.",max_tokens=8)); report.record(8,LayerState.PASS if scheduled.success else LayerState.FAIL_RUNTIME,"selected plan executed" if scheduled.success else (scheduled.error_detail or "failed"))
    events.append(Event(event_type="request",source="reference",payload={"request_id":request.request_id})); events.append(Event(event_type="decision",source="scheduler",payload={"plan_id":selected.plan.plan_id})); events.append(Event(event_type="observation",source="runtime",payload={"success":scheduled.success,"output":scheduled.output})); report.record(9,LayerState.PASS,"execution events persisted")
    materialize(events.events()); report.record(10,LayerState.PASS,"fresh event-log replay completed")
    report.artifact["execution_result"]=scheduled.model_dump(mode="json"); report.save(out/"reference-report.json"); return report
