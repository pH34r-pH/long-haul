"""Regression and conformance tests for scheduler topology policy (#286) and ranking (#292)."""
from __future__ import annotations

import itertools
import random

import pytest

from long_haul.fixtures.topology import anchorage, kestrel, tailscale
from long_haul.models import (
    ExecutionMode,
    ExecutionPlan,
    InferenceProfile,
    Link,
    ModelArtifact,
    Resource,
    ResourceKind,
    Vessel,
)
from long_haul.runtime import ProfileValidation, RuntimeIdentity, ValidationState
from long_haul.scheduler import CandidateResult, MissionRequirements, Scheduler


def _runtime() -> RuntimeIdentity:
    return RuntimeIdentity(runtime_id="fixture", build_id="b1")


def _profile(
    profile_id: str,
    resources: list[str],
    *,
    strategy: str = "pipeline_parallel",
    synchronous_cross_node: bool | None = None,
) -> InferenceProfile:
    return InferenceProfile(
        id=profile_id,
        runtime_id="fixture",
        strategy=strategy,
        artifact=ModelArtifact(foundation="test", quantization="Q4"),
        participating_resources=resources,
        synchronous_cross_node=synchronous_cross_node,
    )


def _validation(profile: InferenceProfile) -> ProfileValidation:
    return ProfileValidation(
        runtime=_runtime(),
        profile_id=profile.id,
        artifact_key=profile.artifact.key,
        resources=profile.participating_resources,
        strategy=profile.strategy,
        options=profile.options,
        state=ValidationState.SUPPORTED,
        rationale="fixture",
    )


def _measured_direct_link(source: str, target: str) -> Link:
    return Link(source=source, target=target, kind="tailscale", direct=True, path="direct", measured=True)


def test_two_vessel_no_link_split_cannot_select_aliased_mode() -> None:
    split = _profile("split", ["ANC-G0", "KST-G0"], synchronous_cross_node=True)
    scheduler = Scheduler([anchorage(), kestrel()], links=[], validations=[_validation(split)], current_runtimes=[_runtime()])
    mission = MissionRequirements(id="m", requires_synchronous_cross_node=True)
    with pytest.raises(ValueError, match="no eligible plan"):
        scheduler.select(mission, [split])

    # Even manual relabelling into LOCAL/POOL/COMPOSE/PIPELINE cannot bypass link policy.
    for mode in ExecutionMode:
        plan = ExecutionPlan(
            plan_id=f"{mode.value.lower()}:split",
            mode=mode,
            vessels=["anchorage-fixture", "kestrel-fixture"],
            resources=["ANC-G0", "KST-G0"],
            inference_profile_id="split",
        )
        res = scheduler.evaluate(mission, plan, split)
        assert not res.eligible
        assert any("direct Tailscale" in reason for reason in res.reasons)


def test_local_rejects_zero_multi_and_mismatched_vessel_plans() -> None:
    single = _profile("single", ["ANC-G0"], strategy="resident")
    multi = _profile("multi", ["ANC-G0", "KST-G0"], strategy="pool")
    scheduler = Scheduler(
        [anchorage(), kestrel()],
        validations=[_validation(single), _validation(multi)],
        current_runtimes=[_runtime()],
    )
    mission = MissionRequirements(id="m")

    # Zero-vessel plan rejected
    zero_plan = ExecutionPlan(plan_id="local:zero", mode=ExecutionMode.LOCAL, vessels=[], resources=["ANC-G0"], inference_profile_id="single")
    assert not scheduler.evaluate(mission, zero_plan, single).eligible

    # Multi-vessel LOCAL rejected
    multi_local = ExecutionPlan(
        plan_id="local:multi",
        mode=ExecutionMode.LOCAL,
        vessels=["anchorage-fixture", "kestrel-fixture"],
        resources=["ANC-G0", "KST-G0"],
        inference_profile_id="multi",
    )
    res = scheduler.evaluate(mission, multi_local, multi)
    assert not res.eligible
    assert any("LOCAL mode requires exactly one vessel" in r for r in res.reasons)

    # Mismatched vessel claim rejected
    spoofed = ExecutionPlan(
        plan_id="local:spoof",
        mode=ExecutionMode.LOCAL,
        vessels=["kestrel-fixture"],
        resources=["ANC-G0"],
        inference_profile_id="single",
    )
    res_spoof = scheduler.evaluate(mission, spoofed, single)
    assert not res_spoof.eligible
    assert any("vessel ownership mismatch" in r for r in res_spoof.reasons)


def test_pipeline_rejects_absent_or_unmeasured_links_even_with_default_mission() -> None:
    split = _profile("split", ["ANC-G0", "KST-G0"])
    default_mission = MissionRequirements(id="default")

    for links in ([], [tailscale("direct")], [tailscale("relay")]):
        scheduler = Scheduler(
            [anchorage(), kestrel()],
            links=links,
            validations=[_validation(split)],
            current_runtimes=[_runtime()],
        )
        plan = next(p for p in scheduler.generate([split]) if p.mode is ExecutionMode.PIPELINE)
        res = scheduler.evaluate(default_mission, plan, split)
        assert not res.eligible
        assert any("measured acceptable direct Tailscale" in r for r in res.reasons)

    # With a measured direct link, cross-node PIPELINE succeeds.
    measured_scheduler = Scheduler(
        [anchorage(), kestrel()],
        links=[_measured_direct_link("anchorage-fixture", "kestrel-fixture")],
        validations=[_validation(split)],
        current_runtimes=[_runtime()],
    )
    plan = next(p for p in measured_scheduler.generate([split]) if p.mode is ExecutionMode.PIPELINE)
    assert measured_scheduler.evaluate(default_mission, plan, split).eligible


def test_independent_pool_allowed_on_relayed_link_and_single_vessel_multi_gpu_allowed() -> None:
    pool_profile = _profile("pool", ["ANC-G0", "KST-G0"], strategy="independent_pool", synchronous_cross_node=False)
    dual_gpu = _profile("dual", ["ANC-G0", "ANC-G1"], strategy="tensor_parallel")
    scheduler = Scheduler(
        [anchorage(), kestrel()],
        links=[tailscale("relay")],
        validations=[_validation(pool_profile), _validation(dual_gpu)],
        current_runtimes=[_runtime()],
    )
    mission = MissionRequirements(id="m")
    pool_plan = next(p for p in scheduler.generate([pool_profile]) if p.mode is ExecutionMode.POOL)
    assert scheduler.evaluate(mission, pool_plan, pool_profile).eligible

    dual_local = next(p for p in scheduler.generate([dual_gpu]) if p.mode is ExecutionMode.LOCAL)
    assert scheduler.evaluate(mission, dual_local, dual_gpu).eligible


def test_three_vessel_synchronous_plan_requires_all_pairwise_edges() -> None:
    third = Vessel(
        id="relay-fixture",
        name="Third",
        class_name="light_craft",
        resources=[Resource(id="REL-G0", kind=ResourceKind.GPU, memory_mb=8192)],
    )
    split3 = _profile("split3", ["ANC-G0", "KST-G0", "REL-G0"], synchronous_cross_node=True)
    partial_links = [
        _measured_direct_link("anchorage-fixture", "kestrel-fixture"),
        _measured_direct_link("kestrel-fixture", "relay-fixture"),
    ]
    scheduler = Scheduler(
        [anchorage(), kestrel(), third],
        links=partial_links,
        validations=[_validation(split3)],
        current_runtimes=[_runtime()],
    )
    plan = scheduler.generate([split3])[0]
    res = scheduler.evaluate(MissionRequirements(id="m"), plan, split3)
    assert not res.eligible
    assert any("anchorage-fixture<->relay-fixture" in r for r in res.reasons)

    full_scheduler = Scheduler(
        [anchorage(), kestrel(), third],
        links=[*partial_links, _measured_direct_link("anchorage-fixture", "relay-fixture")],
        validations=[_validation(split3)],
        current_runtimes=[_runtime()],
    )
    assert full_scheduler.evaluate(MissionRequirements(id="m"), plan, split3).eligible


def test_generated_property_every_eligible_synchronous_plan_satisfies_all_edges() -> None:
    rng = random.Random(20261127)
    vessels = [
        Vessel(id=f"v{i}", name=f"V{i}", class_name="station", resources=[Resource(id=f"G{i}", kind=ResourceKind.GPU, memory_mb=8192)])
        for i in range(4)
    ]
    for trial in range(80):
        chosen = rng.sample(vessels, rng.randint(2, 4))
        chosen_ids = sorted(v.id for v in chosen)
        links: list[Link] = []
        for u, v in itertools.combinations(chosen_ids, 2):
            state = rng.choice(["none", "relay", "unmeasured_direct", "measured_direct"])
            if state == "relay":
                links.append(Link(source=u, target=v, kind="tailscale", direct=False, path="relay", measured=True))
            elif state == "unmeasured_direct":
                links.append(Link(source=u, target=v, kind="tailscale", direct=True, path="direct", measured=False))
            elif state == "measured_direct":
                links.append(_measured_direct_link(u, v))
        prof = _profile(f"p{trial}", [v.resources[0].id for v in chosen], synchronous_cross_node=rng.choice([True, None]))
        scheduler = Scheduler(vessels, links=links, validations=[_validation(prof)], current_runtimes=[_runtime()])
        mission = MissionRequirements(id=f"m{trial}", requires_synchronous_cross_node=rng.choice([True, False]))
        for mode in ExecutionMode:
            plan = ExecutionPlan(
                plan_id=f"{mode.value.lower()}:{prof.id}",
                mode=mode,
                vessels=chosen_ids,
                resources=prof.participating_resources,
                inference_profile_id=prof.id,
            )
            res = scheduler.evaluate(mission, plan, prof)
            is_sync = mode is ExecutionMode.PIPELINE or mission.requires_synchronous_cross_node or prof.synchronous_cross_node is True
            if res.eligible and is_sync:
                for u, v in itertools.combinations(chosen_ids, 2):
                    assert any(
                        {lk.source, lk.target} == {u, v}
                        and lk.kind == "tailscale"
                        and lk.direct is True
                        and lk.path == "direct"
                        and lk.measured is True
                        for lk in links
                    )


def test_rank_handles_one_shot_iterables_single_traversal_and_stale_ranks() -> None:
    scheduler = Scheduler([anchorage()])
    c1 = CandidateResult(
        plan=ExecutionPlan(plan_id="local:a", mode=ExecutionMode.LOCAL, vessels=["anchorage-fixture"]),
        eligible=True,
        reasons=[],
        preemption_penalty=1,
    )
    c2 = CandidateResult(
        plan=ExecutionPlan(plan_id="compose:b", mode=ExecutionMode.COMPOSE, vessels=["anchorage-fixture"]),
        eligible=True,
        reasons=[],
        preemption_penalty=0,
    )
    c3 = CandidateResult(
        plan=ExecutionPlan(plan_id="local:c", mode=ExecutionMode.LOCAL, vessels=["anchorage-fixture"]),
        eligible=False,
        reasons=["failed"],
        rank=(-1, 0, -1, -999.0),  # stale rank must be cleared
    )
    base = [c1, c2, c3]

    class CountingIterable:
        def __init__(self, items: list[CandidateResult]) -> None:
            self._items = items
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            return iter(self._items)

    counted = CountingIterable(base)
    from_list = scheduler.rank(base)
    from_tuple = scheduler.rank(tuple(base))
    from_gen = scheduler.rank(item for item in base)
    from_counted = scheduler.rank(counted)

    assert counted.iterations == 1
    assert [c.plan.plan_id for c in from_list] == ["compose:b", "local:a", "local:c"]
    assert [c.plan.plan_id for c in from_tuple] == [c.plan.plan_id for c in from_list]
    assert [c.plan.plan_id for c in from_gen] == [c.plan.plan_id for c in from_list]
    assert [c.plan.plan_id for c in from_counted] == [c.plan.plan_id for c in from_list]
    assert c3.rank is None
    assert c1.preemption_penalty == 1 and c1.preemption_preference == 1
    assert scheduler.rank([]) == []


def test_rank_documents_lexical_plan_id_tie_break() -> None:
    scheduler = Scheduler([anchorage()])
    local_plan = CandidateResult(
        plan=ExecutionPlan(plan_id="local:x", mode=ExecutionMode.LOCAL, vessels=["anchorage-fixture"]),
        eligible=True,
        reasons=[],
    )
    compose_plan = CandidateResult(
        plan=ExecutionPlan(plan_id="compose:x", mode=ExecutionMode.COMPOSE, vessels=["anchorage-fixture"]),
        eligible=True,
        reasons=[],
    )
    ranked = scheduler.rank([local_plan, compose_plan])
    assert [c.plan.plan_id for c in ranked] == ["compose:x", "local:x"]
