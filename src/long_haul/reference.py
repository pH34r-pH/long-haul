"""Opt-in real reference run; no ordinary test imports this module."""
from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .adapters.llama_cpp import LlamaCppAdapter
from .benchmarks import BenchmarkObservation, BenchmarkStore, Workload
from .events import Event, EventStore, materialize
from .models import ExecutionMode, InferenceProfile, ModelArtifact, ResourceKind
from .runtime import ExecutionRequest, ValidationDepth, ValidationState
from .scheduler import MissionRequirements, Scheduler
from .validation import LayerState, ReferenceReport


@dataclass(frozen=True)
class _Options:
    binary: str
    model: str
    output_dir: str
    timeout_seconds: float
    vessel_id: str
    vessel_name: str
    resource_prefix: str | None


def _discover(options: _Options):
    if os.name == "nt":
        from .discovery.windows import GenericWindowsDiscovery

        return GenericWindowsDiscovery(
            options.vessel_id,
            options.vessel_name,
            "station",
            resource_prefix=options.resource_prefix,
        ).discover()
    from .discovery.linux import GenericLinuxDiscovery

    return GenericLinuxDiscovery(options.vessel_id, options.vessel_name, "station").discover()


def _failure_state(result) -> LayerState:
    if result.error_class and result.error_class.value == "FAIL_RESOURCE":
        return LayerState.FAIL_RESOURCE
    return LayerState.FAIL_RUNTIME


class _ReferenceRun:
    def __init__(self, options: _Options):
        self.options = options
        self.out = Path(options.output_dir)
        self.report = ReferenceReport(environment={"platform": platform.platform()})
        self.events = EventStore(self.out / "events.jsonl")
        self.vessel = None
        self.profile = None
        self.adapter = LlamaCppAdapter(options.binary)
        self.validation = None

    def checkpoint(self) -> None:
        self.report.save(self.out / "reference-report.json")

    def prepare(self) -> bool:
        self.report.record(0, LayerState.PASS, "imports and schemas loaded")
        self.report.record(1, LayerState.PASS, "append-only event store available")
        self.vessel = _discover(self.options)
        self.report.artifact["vessel"] = self.vessel.model_dump()
        self.report.record(2, LayerState.PASS, "platform discovery completed")
        cpu = next((r for r in self.vessel.resources if r.kind is ResourceKind.CPU), None)
        if cpu is None:
            self.report.record(3, LayerState.FAIL_RESOURCE, "discovery returned no CPU resource")
            self.checkpoint()
            return False
        artifact = ModelArtifact(
            foundation="SmolLM2-135M-Instruct-GGUF",
            quantization="Q4_K_M",
        )
        self.profile = InferenceProfile(
            id=f"{self.options.vessel_id}-cpu-resident",
            runtime_id="llama.cpp",
            strategy="resident",
            artifact=artifact,
            participating_resources=[cpu.id],
            options={"model_path": self.options.model},
        )
        self.validation = self.adapter.validate(self.profile)
        self.report.artifact["validation"] = self.validation.model_dump(mode="json")
        if self.validation.state is ValidationState.SUPPORTED:
            self.report.record(3, LayerState.PASS, self.validation.rationale)
            self.report.record(4, LayerState.PASS, "GGUF artifact present")
            return True
        state = (
            LayerState.UNSUPPORTED
            if self.validation.state is ValidationState.UNSUPPORTED
            else LayerState.UNKNOWN
        )
        self.report.record(3, state, self.validation.rationale)
        self.checkpoint()
        return False

    def direct(self) -> bool:
        request = ExecutionRequest(
            request_id=str(uuid4()),
            profile=self.profile,
            prompt="Return exactly: LONG_HAUL_OK",
            max_tokens=12,
            timeout_seconds=self.options.timeout_seconds,
        )
        self.events.append(
            Event(
                event_type="request",
                source="reference",
                payload={"request_id": request.request_id, "state": "started"},
            )
        )
        self.report.record(5, LayerState.UNKNOWN, "inference is in progress")
        self.checkpoint()
        result = self.adapter.execute(request)
        if not result.success:
            self.report.set_layer(5, _failure_state(result), result.error_detail or "inference failed")
            self.checkpoint()
            return False
        self.validation.depth = ValidationDepth.EXECUTION
        self.validation.provenance = "measured"
        self.report.artifact["validation"] = self.validation.model_dump(mode="json")
        self.report.set_layer(5, LayerState.PASS, "normalized inference and model load succeeded")
        self.direct_result = result
        return True

    def benchmark(self) -> BenchmarkStore | None:
        result = self.direct_result
        observation = BenchmarkObservation(
            runtime=result.runtime,
            profile=self.profile,
            plan_mode=ExecutionMode.LOCAL,
            resources=self.profile.participating_resources,
            workload=Workload(name="exact-string", cold=True),
            provenance="measured",
            load_seconds=result.timings.load_seconds,
            ttft_seconds=result.timings.ttft_seconds,
            prefill_tps=result.timings.prefill_tps,
            decode_tps=result.timings.decode_tps,
        )
        store = BenchmarkStore(self.out / "benchmarks.jsonl")
        try:
            store.append(observation)
            queried = store.query(self.profile.id)
        except (OSError, ValueError) as exc:
            self.report.record(6, LayerState.FAIL_SYSTEM, f"benchmark persistence/query failed: {exc}")
            self.checkpoint()
            return None
        if not queried:
            self.report.record(6, LayerState.FAIL_SYSTEM, "benchmark persistence/query returned no observation")
            self.checkpoint()
            return None
        self.validation.depth = ValidationDepth.BENCHMARK
        self.report.artifact["validation"] = self.validation.model_dump(mode="json")
        self.report.record(6, LayerState.PASS, "measured benchmark persisted and queried")
        return store

    def scheduled(self, store: BenchmarkStore) -> bool:
        scheduler = Scheduler(
            [self.vessel],
            benchmarks=store.query(self.profile.id),
            validations=[self.validation],
            current_runtimes=[self.adapter.identity()],
        )
        selected = scheduler.select(
            MissionRequirements(id="reference", allow_modes={ExecutionMode.LOCAL}),
            [self.profile],
        )
        self.report.artifact["selected_plan"] = selected.plan.model_dump()
        self.report.record(7, LayerState.PASS, "scheduler selected validated LOCAL resident plan")
        request = ExecutionRequest(
            request_id=str(uuid4()),
            profile=self.profile,
            prompt="Say hello.",
            max_tokens=8,
            timeout_seconds=self.options.timeout_seconds,
        )
        self.events.append(Event(event_type="decision", source="scheduler", payload={"plan_id": selected.plan.plan_id}))
        self.events.append(Event(event_type="request", source="reference", payload={"request_id": request.request_id, "state": "started"}))
        self.report.record(8, LayerState.UNKNOWN, "scheduled execution is in progress")
        self.checkpoint()
        result = self.adapter.execute(request)
        if not result.success:
            self.report.set_layer(8, _failure_state(result), result.error_detail or "scheduled execution failed")
            self.report.artifact["execution_result"] = result.model_dump(mode="json")
            self.checkpoint()
            return False
        self.report.set_layer(8, LayerState.PASS, "scheduler-selected plan executed")
        self.report.artifact["execution_result"] = result.model_dump(mode="json")
        return True

    def finish(self) -> None:
        self.events.append(
            Event(
                event_type="observation",
                source="runtime",
                payload={"success": True, "output": self.report.artifact["execution_result"]["output"]},
            )
        )
        self.report.record(9, LayerState.PASS, "execution events persisted")
        materialize(self.events.events())
        self.report.record(10, LayerState.PASS, "fresh event-log replay completed")
        self.checkpoint()


def run(
    binary: str,
    model: str,
    output_dir: str,
    timeout_seconds: float = 900,
    *,
    vessel_id: str | None = None,
    vessel_name: str | None = None,
    resource_prefix: str | None = None,
) -> ReferenceReport:
    options = _Options(
        binary=binary,
        model=model,
        output_dir=output_dir,
        timeout_seconds=timeout_seconds,
        vessel_id=vessel_id or ("windows-host" if os.name == "nt" else "work-vm"),
        vessel_name=vessel_name or ("Windows Host" if os.name == "nt" else "Work VM"),
        resource_prefix=resource_prefix,
    )
    reference = _ReferenceRun(options)
    if not reference.prepare() or not reference.direct():
        return reference.report
    store = reference.benchmark()
    if store is None or not reference.scheduled(store):
        return reference.report
    reference.finish()
    return reference.report
