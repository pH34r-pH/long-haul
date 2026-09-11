"""Runtime-agnostic plan selection over normalized evidence."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..benchmarks import BenchmarkObservation
from ..models import ExecutionMode, ExecutionPlan, InferenceProfile, Link, Vessel
from ..runtime import ProfileValidation, ValidationState


@dataclass(frozen=True)
class MissionRequirements:
    id: str; required_resources: set[str] = field(default_factory=set)
    requires_synchronous_cross_node: bool = False; allow_modes: set[ExecutionMode] = field(default_factory=lambda: set(ExecutionMode))
    safety_ok: bool = True; cooperation_ok: bool = True; minimum_memory_mib: float = 0; allow_unknown_runtime: bool = False

@dataclass
class CandidateResult:
    plan: ExecutionPlan; eligible: bool; reasons: list[str]
    evidence: BenchmarkObservation | None = None
    rank: tuple[int, int, int, int, int, float] | None = None

class Scheduler:
    def __init__(self, vessels: Iterable[Vessel], links: Iterable[Link] = (), benchmarks: Iterable[BenchmarkObservation] = (), validations: Iterable[ProfileValidation] = ()):
        self.vessels = list(vessels); self.links = list(links); self.benchmarks = list(benchmarks)
        self.resources = {r.id: r for v in self.vessels for r in v.resources}
        self.validations = {v.profile_id: v for v in validations}
    def generate(self, profiles: Iterable[InferenceProfile]) -> list[ExecutionPlan]:
        output = []
        for profile in profiles:
            resources = profile.participating_resources
            vessels = sorted({v.id for v in self.vessels if any(r.id in resources for r in v.resources)})
            for mode in ExecutionMode:
                output.append(ExecutionPlan(plan_id=f"{mode.value.lower()}:{profile.id}",mode=mode,vessels=vessels,resources=resources,inference_profile_id=profile.id))
        return output
    def evaluate(self, mission: MissionRequirements, plan: ExecutionPlan, profile: InferenceProfile) -> CandidateResult:
        reasons: list[str] = []
        validation = self.validations.get(profile.id)
        if validation is None: reasons.append("no runtime feasibility validation")
        elif validation.state is ValidationState.UNSUPPORTED: reasons.append(f"runtime profile unsupported: {validation.rationale}")
        elif validation.state is ValidationState.UNKNOWN and not mission.allow_unknown_runtime: reasons.append(f"runtime profile unknown outside exploration policy: {validation.rationale}")
        if not mission.safety_ok: reasons.append("hard safety constraint failed")
        if not mission.cooperation_ok: reasons.append("hard cooperation-integrity constraint failed")
        if plan.mode not in mission.allow_modes: reasons.append("mode is outside mission constraints")
        if not mission.required_resources <= set(plan.resources): reasons.append("required resource placement missing")
        if set(profile.participating_resources) != set(plan.resources): reasons.append("plan/profile resource pairing mismatch")
        # Do not sum RAM/VRAM: a profile names participating resources and the
        # runtime validates its placement. This only checks a single named
        # capacity requirement; split/offload profiles express their own
        # placement in `options` and are validated by their runtime adapter.
        memory = max(((self.resources[r].capacity_mib() or 0) for r in plan.resources if r in self.resources), default=0)
        required = max([c.amount * (1024 if c.unit == "GiB" else 1) for c in profile.requirements if c.kind == "memory" and c.unit in ("MiB","GiB")] + [mission.minimum_memory_mib])
        if required and memory < required: reasons.append("profile requirements exceed named resource capacity")
        cross_node = len(plan.vessels) > 1
        if plan.mode is ExecutionMode.PIPELINE and cross_node and mission.requires_synchronous_cross_node:
            matching = [link for link in self.links if {link.source,link.target} == set(plan.vessels)]
            if not any(link.kind == "tailscale" and link.direct and link.path == "direct" for link in matching): reasons.append("synchronous PIPELINE requires acceptable direct Tailscale path")
        evidence = next((b for b in self.benchmarks if b.profile.id == profile.id and b.plan_mode is plan.mode and set(plan.resources) <= set(b.resources) and b.provenance == "measured"), None)
        if evidence is None: evidence = next((b for b in self.benchmarks if b.profile.id == profile.id and b.plan_mode is plan.mode and set(plan.resources) <= set(b.resources)), None)
        if evidence is None: reasons.append("no comparable benchmark evidence; uncertainty retained")
        return CandidateResult(plan, not reasons or reasons == ["no comparable benchmark evidence; uncertainty retained"], reasons, evidence)
    def rank(self, candidates: Iterable[CandidateResult]) -> list[CandidateResult]:
        for result in candidates:
            if not result.eligible: continue
            # Higher-priority fields are binary feasibility/quality gates, efficiency only breaks ties.
            measured = int(result.evidence is not None and result.evidence.provenance == "measured")
            quality = measured
            continuity = int(result.plan.mode is not ExecutionMode.PIPELINE)
            efficiency = -(result.evidence.decode_tps if result.evidence and result.evidence.decode_tps else 0.0)
            result.rank = (0, 0, 0, -quality, -continuity, efficiency)
        return sorted(candidates, key=lambda c: (not c.eligible, c.rank or (99,)*6, c.plan.plan_id))
    def select(self, mission: MissionRequirements, profiles: Iterable[InferenceProfile]) -> CandidateResult:
        profiles_by_id = {p.id:p for p in profiles}
        candidates = [self.evaluate(mission, plan, profiles_by_id[plan.inference_profile_id]) for plan in self.generate(profiles_by_id.values())]
        ranked = self.rank(candidates)
        if not any(c.eligible for c in ranked): raise ValueError("no eligible plan: " + "; ".join(r for c in ranked for r in c.reasons))
        return next(c for c in ranked if c.eligible)
