"""Runtime-agnostic plan selection over normalized evidence."""
from __future__ import annotations

import itertools
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

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
    restartable: bool = False
    preemption_policy: Literal["allow", "prefer", "forbid"] = "allow"

    def __post_init__(self) -> None:
        if not math.isfinite(self.minimum_memory_mib) or self.minimum_memory_mib < 0:
            raise ValueError("minimum_memory_mib must be finite and nonnegative")
        if self.preemption_policy not in {"allow", "prefer", "forbid"}:
            raise ValueError("preemption_policy must be allow, prefer, or forbid")
        if self.preemption_policy == "prefer" and not self.restartable:
            raise ValueError("preemptible resources can only be preferred for restartable work")

@dataclass
class CandidateResult:
    plan: ExecutionPlan; eligible: bool; reasons: list[str]
    evidence: BenchmarkObservation | None = None
    preemption_penalty: int = 0
    rank: tuple[int, int, int, float] | None = None

    @property
    def preemption_preference(self) -> int:
        """Backward-compatible alias for ``preemption_penalty`` (1 = penalized)."""
        return self.preemption_penalty

class Scheduler:
    def __init__(self, vessels: Iterable[Vessel], links: Iterable[Link] = (), benchmarks: Iterable[BenchmarkObservation] = (), validations: Iterable[ProfileValidation] = (), current_runtimes: Iterable[RuntimeIdentity] = (), *, now: datetime | None = None):
        self.vessels = list(vessels); self.links = list(links); self.benchmarks = list(benchmarks)
        self.vessels_by_id = {v.id: v for v in self.vessels}
        self.now = now or datetime.now(UTC)
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("scheduler time must be timezone-aware")
        self.resources = {r.id: r for v in self.vessels for r in v.resources}
        self.validations = list(validations)
        runtime_groups: dict[str, list[RuntimeIdentity]] = {}
        for runtime in current_runtimes:
            runtime_groups.setdefault(runtime.runtime_id, []).append(runtime)
        self.current_runtimes = {key: group[0] for key, group in runtime_groups.items()
                                 if all(runtime == group[0] for runtime in group)}
    def _derived_vessels(self, profile: InferenceProfile) -> tuple[list[str], list[str]]:
        resources = profile.participating_resources
        unknown = [r for r in resources if r not in self.resources]
        vessels = sorted({v.id for v in self.vessels if any(r.id in resources for r in v.resources)})
        return vessels, unknown

    def generate(self, profiles: Iterable[InferenceProfile]) -> list[ExecutionPlan]:
        output: list[ExecutionPlan] = []
        for profile in profiles:
            vessels, _ = self._derived_vessels(profile)
            if len(vessels) <= 1:
                modes: tuple[ExecutionMode, ...] = tuple(ExecutionMode)
            elif profile.synchronous_cross_node is True:
                modes = (ExecutionMode.PIPELINE,)
            else:
                modes = (ExecutionMode.POOL, ExecutionMode.PIPELINE, ExecutionMode.COMPOSE)
            for mode in modes:
                output.append(
                    ExecutionPlan(
                        plan_id=f"{mode.value.lower()}:{profile.id}",
                        mode=mode,
                        vessels=vessels,
                        resources=profile.participating_resources,
                        inference_profile_id=profile.id,
                    )
                )
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
        metric = benchmark.prefill_tps if benchmark.workload.prompt_tokens and not benchmark.workload.output_tokens else benchmark.decode_tps
        return metric is not None and all(value is None or (math.isfinite(value) and value >= 0) for value in metrics)

    def evaluate(self, mission: MissionRequirements, plan: ExecutionPlan, profile: InferenceProfile) -> CandidateResult:
        reasons: list[str] = []
        validation = self._validation(profile)
        if validation is None: reasons.append("no runtime feasibility validation")
        elif validation.state is ValidationState.UNSUPPORTED: reasons.append(f"runtime profile unsupported: {validation.rationale}")
        elif validation.state is ValidationState.UNKNOWN and not mission.allow_unknown_runtime: reasons.append(f"runtime profile unknown outside exploration policy: {validation.rationale}")
        reasons.extend(self._placement_reasons(mission, plan, profile))
        evidence = self._evidence(mission, plan, profile)
        if evidence is None: reasons.append("no comparable benchmark evidence; uncertainty retained")
        uses_preemptible = any(
            (self.vessels_by_id.get(vessel_id) is not None)
            and self.vessels_by_id[vessel_id].lease is not None
            and self.vessels_by_id[vessel_id].lease.preemptible
            for vessel_id in plan.vessels
        )
        preemption_penalty = int(mission.preemption_policy == "prefer" and not uses_preemptible)
        return CandidateResult(
            plan,
            not reasons or reasons == ["no comparable benchmark evidence; uncertainty retained"],
            reasons,
            evidence,
            preemption_penalty,
        )

    def _vessel_and_resource_reasons(
        self,
        mission: MissionRequirements,
        plan: ExecutionPlan,
        profile: InferenceProfile,
        actual_vessels: list[str],
        unknown_resources: list[str],
    ) -> list[str]:
        reasons: list[str] = []
        if not mission.required_resources <= set(plan.resources):
            reasons.append("required resource placement missing")
        if set(profile.participating_resources) != set(plan.resources):
            reasons.append("plan/profile resource pairing mismatch")
        if unknown_resources:
            reasons.append("unknown participating resource: " + ", ".join(sorted(set(unknown_resources))))
        if set(plan.vessels) != set(actual_vessels):
            reasons.append("plan/profile vessel ownership mismatch")
        if plan.mode is ExecutionMode.LOCAL and len(actual_vessels) != 1:
            reasons.append("LOCAL mode requires exactly one vessel")
        return reasons

    def _placement_reasons(self, mission: MissionRequirements, plan: ExecutionPlan, profile: InferenceProfile) -> list[str]:
        reasons: list[str] = []
        if not mission.safety_ok: reasons.append("hard safety constraint failed")
        if not mission.cooperation_ok: reasons.append("hard cooperation-integrity constraint failed")
        if plan.mode not in mission.allow_modes: reasons.append("mode is outside mission constraints")
        actual_vessels, unknown_resources = self._derived_vessels(profile)
        reasons.extend(self._vessel_and_resource_reasons(mission, plan, profile, actual_vessels, unknown_resources))
        reasons.extend(self._lease_reasons(mission, plan))
        memory_issue = self._memory_issue(mission, plan, profile)
        if memory_issue is not None:
            reasons.append(memory_issue)
        missing_edges = self._missing_synchronous_links(mission, plan, profile, actual_vessels)
        if missing_edges:
            edge_text = ", ".join(f"{u}<->{v}" for u, v in missing_edges)
            reasons.append(f"synchronous cross-node execution requires measured acceptable direct Tailscale path ({edge_text})")
        return reasons

    def _lease_reasons(self, mission: MissionRequirements, plan: ExecutionPlan) -> list[str]:
        reasons: list[str] = []
        missing = [vessel_id for vessel_id in plan.vessels if vessel_id not in self.vessels_by_id]
        if missing:
            reasons.append("planned vessel is unavailable")
        for vessel_id in plan.vessels:
            vessel = self.vessels_by_id.get(vessel_id)
            if vessel is None or vessel.lease is None:
                continue
            if not vessel.lease.active(self.now):
                reasons.append(f"resource lease for vessel {vessel_id} is expired or not yet active")
            if mission.preemption_policy == "forbid" and vessel.lease.preemptible:
                reasons.append(f"preemptible resource is forbidden for vessel {vessel_id}")
        return reasons

    @staticmethod
    def _requires_synchronous_cross_node(
        mission: MissionRequirements,
        plan: ExecutionPlan,
        profile: InferenceProfile,
        vessels: list[str],
    ) -> bool:
        if len(vessels) <= 1:
            return False
        return (
            plan.mode is ExecutionMode.PIPELINE
            or mission.requires_synchronous_cross_node
            or profile.synchronous_cross_node is True
        )

    def _missing_synchronous_links(
        self,
        mission: MissionRequirements,
        plan: ExecutionPlan,
        profile: InferenceProfile,
        vessels: list[str],
    ) -> list[tuple[str, str]]:
        if not self._requires_synchronous_cross_node(mission, plan, profile, vessels):
            return []
        missing: list[tuple[str, str]] = []
        for u, v in itertools.combinations(sorted(set(vessels)), 2):
            matching = [link for link in self.links if {link.source, link.target} == {u, v}]
            if not any(
                link.kind == "tailscale"
                and link.direct is True
                and link.path == "direct"
                and link.measured is True
                for link in matching
            ):
                missing.append((u, v))
        return missing

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
        """Deterministically rank candidates in a single pass.

        Eligible candidates are ordered by ``(-measured, preemption_penalty,
        -continuity, -throughput)`` with deterministic lexical ``plan_id``
        tie-breaking. Ineligible candidates always sort after eligible ones.
        """
        items = list(candidates)
        for result in items:
            if not result.eligible:
                result.rank = None
                continue
            measured = int(result.evidence is not None and result.evidence.provenance == "measured")
            continuity = int(result.plan.mode is not ExecutionMode.PIPELINE)
            evidence = result.evidence
            throughput = (
                evidence.prefill_tps
                if evidence.workload.prompt_tokens and not evidence.workload.output_tokens
                else evidence.decode_tps
            ) if evidence else None
            efficiency = -(throughput or 0.0)
            result.rank = (-measured, result.preemption_penalty, -continuity, efficiency)
        ineligible_rank = (1, 1, 1, float("inf"))
        return sorted(
            items,
            key=lambda c: (not c.eligible, c.rank if c.eligible and c.rank is not None else ineligible_rank, c.plan.plan_id),
        )
    def select(self, mission: MissionRequirements, profiles: Iterable[InferenceProfile]) -> CandidateResult:
        profiles_by_id = {p.id:p for p in profiles}
        candidates = [self.evaluate(mission, plan, profiles_by_id[plan.inference_profile_id]) for plan in self.generate(profiles_by_id.values())]
        ranked = self.rank(candidates)
        if not any(c.eligible for c in ranked): raise ValueError("no eligible plan: " + "; ".join(r for c in ranked for r in c.reasons))
        return next(c for c in ranked if c.eligible)
