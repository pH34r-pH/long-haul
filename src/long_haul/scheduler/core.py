"""Runtime-agnostic plan selection over normalized evidence."""
from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ..benchmarks import BenchmarkObservation
from ..benchmarks.store import Workload, evidence_is_fresh
from ..models import (
    ExecutionMode,
    ExecutionPlan,
    InferenceProfile,
    Link,
    Vessel,
    canonical_json,
)
from ..runtime import ProfileValidation, RuntimeIdentity, ValidationState


@dataclass(frozen=True)
class MissionRequirements:
    id: str; required_resources: set[str] = field(default_factory=set)
    requires_synchronous_cross_node: bool = False; allow_modes: set[ExecutionMode] = field(default_factory=lambda: set(ExecutionMode))
    workload: Workload | None = None
    evidence_not_before: datetime | None = None
    safety_ok: bool = True; cooperation_ok: bool = True; minimum_memory_mib: float = 0; allow_unknown_runtime: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_memory_mib) or self.minimum_memory_mib < 0:
            raise ValueError("minimum_memory_mib must be finite and nonnegative")

@dataclass
class CandidateResult:
    plan: ExecutionPlan; eligible: bool; reasons: list[str]
    evidence: BenchmarkObservation | None = None
    rank: tuple[int, int, int, int, int, float] | None = None

class Scheduler:
    def __init__(self, vessels: Iterable[Vessel], links: Iterable[Link] = (), benchmarks: Iterable[BenchmarkObservation] = (), validations: Iterable[ProfileValidation] = (), current_runtimes: Iterable[RuntimeIdentity] = ()):
        self.vessels = list(vessels); self.links = list(links); self.benchmarks = list(benchmarks)
        self.resources = {r.id: r for v in self.vessels for r in v.resources}
        self.validations = list(validations)
        runtime_groups: dict[str, list[RuntimeIdentity]] = {}
        for runtime in current_runtimes:
            runtime_groups.setdefault(runtime.runtime_id, []).append(runtime)
        self.current_runtimes = {key: group[0] for key, group in runtime_groups.items()
                                 if all(runtime == group[0] for runtime in group)}
    def generate(self, profiles: Iterable[InferenceProfile]) -> list[ExecutionPlan]:
        output = []
        for profile in profiles:
            resources = profile.participating_resources
            vessels = sorted({v.id for v in self.vessels if any(r.id in resources for r in v.resources)})
            for mode in ExecutionMode:
                output.append(ExecutionPlan(plan_id=f"{mode.value.lower()}:{profile.id}",mode=mode,vessels=vessels,resources=resources,inference_profile_id=profile.id))
        return output
    def _validation(self, profile: InferenceProfile) -> ProfileValidation | None:
        runtime = self.current_runtimes.get(profile.runtime_id)
        if runtime is None or not (runtime.version or runtime.build_id):
            return None
        matches = [v for v in self.validations if (
            v.profile_id == profile.id and v.runtime == runtime
            and v.artifact_key == profile.artifact.key
            and v.resources == profile.participating_resources
            and v.strategy == profile.strategy and canonical_json(v.options) == canonical_json(profile.options)
        )]
        # Contradictory or repeated validations need an explicit reconciliation
        # policy. Collection order cannot silently decide certification.
        unique = {json.dumps(v.model_dump(mode="json"), sort_keys=True, allow_nan=False): v for v in matches}
        return next(iter(unique.values())) if len(unique) == 1 else None

    def _evidence(self, mission: MissionRequirements, plan: ExecutionPlan, profile: InferenceProfile) -> BenchmarkObservation | None:
        runtime = self.current_runtimes.get(profile.runtime_id)
        if runtime is None or mission.workload is None or mission.evidence_not_before is None:
            return None
        cutoff = mission.evidence_not_before
        if cutoff.tzinfo is None:
            return None
        comparable = [b for b in self.benchmarks if
                      self._compatible_observation(b, mission, plan, profile, runtime)
                      and evidence_is_fresh(b.timestamp, cutoff) and self._valid_metrics(b)]
        # No scientific aggregation/sample policy is implied by storage order.
        # Multiple records retain uncertainty until a caller defines that policy.
        unique = {json.dumps(b.model_dump(mode="json", exclude={"id"}), sort_keys=True, allow_nan=False): b for b in sorted(comparable, key=lambda b: b.id)}
        return next(iter(unique.values())) if len(unique) == 1 else None

    @staticmethod
    def _compatible_observation(benchmark: BenchmarkObservation, mission: MissionRequirements, plan: ExecutionPlan, profile: InferenceProfile, runtime: RuntimeIdentity) -> bool:
        return (benchmark.profile.identity == profile.identity and benchmark.plan_mode is plan.mode
                and benchmark.resources == plan.resources and benchmark.workload == mission.workload
                and benchmark.runtime == runtime and benchmark.error is None
                and benchmark.provenance == "measured")

    @staticmethod
    def _valid_metrics(benchmark: BenchmarkObservation) -> bool:
        metrics = [benchmark.load_seconds, benchmark.ttft_seconds, benchmark.prefill_tps,
                   benchmark.decode_tps, benchmark.power_watts]
        metrics.extend(benchmark.residency_mib.values())
        metrics.extend(benchmark.utilization.values())
        metrics.extend(value for value in benchmark.network.values() if isinstance(value, (int, float)))
        return benchmark.decode_tps is not None and all(value is None or (math.isfinite(value) and value >= 0) for value in metrics)

    def evaluate(self, mission: MissionRequirements, plan: ExecutionPlan, profile: InferenceProfile) -> CandidateResult:
        reasons: list[str] = []
        validation = self._validation(profile)
        if validation is None: reasons.append("no runtime feasibility validation")
        elif validation.state is ValidationState.UNSUPPORTED: reasons.append(f"runtime profile unsupported: {validation.rationale}")
        elif validation.state is ValidationState.UNKNOWN and not mission.allow_unknown_runtime: reasons.append(f"runtime profile unknown outside exploration policy: {validation.rationale}")
        reasons.extend(self._placement_reasons(mission, plan, profile))
        evidence = self._evidence(mission, plan, profile)
        if evidence is None: reasons.append("no comparable benchmark evidence; uncertainty retained")
        return CandidateResult(plan, not reasons or reasons == ["no comparable benchmark evidence; uncertainty retained"], reasons, evidence)
    def _placement_reasons(self, mission: MissionRequirements, plan: ExecutionPlan, profile: InferenceProfile) -> list[str]:
        reasons: list[str] = []
        if not mission.safety_ok: reasons.append("hard safety constraint failed")
        if not mission.cooperation_ok: reasons.append("hard cooperation-integrity constraint failed")
        if plan.mode not in mission.allow_modes: reasons.append("mode is outside mission constraints")
        if not mission.required_resources <= set(plan.resources): reasons.append("required resource placement missing")
        if set(profile.participating_resources) != set(plan.resources): reasons.append("plan/profile resource pairing mismatch")
        memory_issue = self._memory_issue(mission, plan, profile)
        if memory_issue is not None:
            reasons.append(memory_issue)
        if self._unacceptable_pipeline_link(mission, plan):
            reasons.append("synchronous PIPELINE requires acceptable direct Tailscale path")
        return reasons

    def _unacceptable_pipeline_link(self, mission: MissionRequirements, plan: ExecutionPlan) -> bool:
        if plan.mode is not ExecutionMode.PIPELINE or len(plan.vessels) <= 1 or not mission.requires_synchronous_cross_node:
            return False
        matching = [link for link in self.links if {link.source, link.target} == set(plan.vessels)]
        return not any(link.kind == "tailscale" and link.direct and link.path == "direct" for link in matching)

    def _memory_issue(self, mission: MissionRequirements, plan: ExecutionPlan, profile: InferenceProfile) -> str | None:
        # Do not sum RAM/VRAM: a profile names participating resources and the
        # runtime validates its placement. This only checks a single named
        # capacity requirement; split/offload profiles express their own
        # placement in `options` and are validated by their runtime adapter.
        capacities = [self.resources[r].capacity_mib() for r in plan.resources if r in self.resources]
        known = [capacity for capacity in capacities if capacity is not None]
        # Repeated requirements are simultaneous lower bounds, so the largest
        # normalized bound wins independently of ordering (never a sum).
        required = max([c.as_mib() for c in profile.requirements if c.kind == "memory"] + [mission.minimum_memory_mib])
        if not required:
            return None
        if not known:
            return "named resource memory capacity unknown"
        return "profile requirements exceed named resource capacity" if max(known) < required else None

    def rank(self, candidates: Iterable[CandidateResult]) -> list[CandidateResult]:
        for result in candidates:
            if not result.eligible: continue
            # Feasibility is checked first; measured performance breaks eligible ties.
            measured = int(result.evidence is not None and result.evidence.provenance == "measured")
            # Measurement establishes performance provenance, never output correctness.
            continuity = int(result.plan.mode is not ExecutionMode.PIPELINE)
            efficiency = -(result.evidence.decode_tps if result.evidence and result.evidence.decode_tps else 0.0)
            result.rank = (0, 0, 0, -measured, -continuity, efficiency)
        return sorted(candidates, key=lambda c: (not c.eligible, c.rank or (99,)*6, c.plan.plan_id))
    def select(self, mission: MissionRequirements, profiles: Iterable[InferenceProfile]) -> CandidateResult:
        profiles_by_id = {p.id:p for p in profiles}
        candidates = [self.evaluate(mission, plan, profiles_by_id[plan.inference_profile_id]) for plan in self.generate(profiles_by_id.values())]
        ranked = self.rank(candidates)
        if not any(c.eligible for c in ranked): raise ValueError("no eligible plan: " + "; ".join(r for c in ranked for r in c.reasons))
        return next(c for c in ranked if c.eligible)
