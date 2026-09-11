from pathlib import Path

import pytest

from long_haul.benchmarks import BenchmarkObservation
from long_haul.decisions import DecisionEngine, DecisionRecord
from long_haul.events import Event, EventStore, materialize
from long_haul.fixtures.topology import anchorage, kestrel, tailscale
from long_haul.models import (
    DecisionPosition,
    Embodiment,
    ExecutionMode,
    InferenceProfile,
    ModelArtifact,
    Position,
)
from long_haul.registry import load_crew, load_vessel
from long_haul.runtime import ProfileValidation, RuntimeIdentity, ValidationState
from long_haul.scheduler import MissionRequirements, Scheduler


def profile(id="resident", resources=None, strategy="resident"):
    resources = resources or ["ANC-G0"]
    return InferenceProfile(id=id,runtime_id="fixture",strategy=strategy,artifact=ModelArtifact(foundation="test",quantization="Q4"),participating_resources=resources)

def test_manifests_and_compositional_artifacts():
    root=Path(__file__).parents[1]
    a=load_vessel(root/"fleet/anchorage.yaml"); k=load_vessel(root/"fleet/kestrel.yaml")
    assert any(r.kind.value=="storage" for r in a.resources) and any(r.kind.value=="storage" for r in k.resources)
    assert profile().artifact.key == "test@Q4"
    assert len({ExecutionMode.LOCAL,ExecutionMode.POOL,ExecutionMode.PIPELINE,ExecutionMode.COMPOSE}) == 4

def test_event_replay_preserves_identity_across_embodiment_replacement(tmp_path):
    crew=load_crew(Path(__file__).parents[1]/"crew/NAV-01.yaml"); store=EventStore(tmp_path/"events.jsonl")
    store.append(Event(event_type="embodiment_change",actor="NAV-01",payload={"crew_id":"NAV-01","embodiment":Embodiment(vessel="anchorage",resource="ANC-G0",runtime="fixture",artifact=ModelArtifact(foundation="new-model")).model_dump()}))
    rebuilt=materialize(store.events(),[crew])
    assert rebuilt.crew["NAV-01"].crew_id == "NAV-01"
    assert rebuilt.crew["NAV-01"].embodiment.artifact.foundation == "new-model"
    assert "embodiment_change" in store.export_jsonl()

def test_decision_protocol_blocks_erasure_and_preference_veto():
    engine=DecisionEngine(); record=DecisionRecord(id="d",proposal="x",participants={"NAV-01","ENG-01"})
    engine.capture_initial(record,DecisionPosition(participant="NAV-01",position=Position.SUPPORT))
    with pytest.raises(ValueError): engine.resolve(record,"consent","do x")
    with pytest.raises(ValueError): DecisionRecord(id="bad",proposal="x",participants={"a"},initial_positions=[DecisionPosition(participant="a",position=Position.BLOCK,category="preference",rationale="prefer it")])
    engine.capture_initial(record,DecisionPosition(participant="ENG-01",position=Position.STAND_ASIDE,category="resource",rationale="feasible but constrained"))
    assert engine.resolve(record,"recorded_disagreement","do x").initial_positions[1].position is Position.STAND_ASIDE

def test_scheduler_respects_link_and_measured_evidence():
    resident=profile("resident",["ANC-G0"]); split=profile("split",["ANC-G0","KST-G0"],"pipeline_parallel")
    observation=BenchmarkObservation(profile=resident,plan_mode=ExecutionMode.LOCAL,resources=["ANC-G0"],workload={"name":"fixture"},provenance="measured",decode_tps=9)
    validation=ProfileValidation(runtime=RuntimeIdentity(runtime_id="fixture"),profile_id="resident",artifact_key=resident.artifact.key,resources=resident.participating_resources,strategy="resident",state=ValidationState.SUPPORTED,rationale="fixture")
    split_validation=ProfileValidation(runtime=RuntimeIdentity(runtime_id="fixture"),profile_id="split",artifact_key=split.artifact.key,resources=split.participating_resources,strategy="pipeline_parallel",state=ValidationState.SUPPORTED,rationale="fixture")
    scheduler=Scheduler([anchorage(),kestrel()],[tailscale("relay")],[observation],[validation,split_validation])
    mission=MissionRequirements(id="m",requires_synchronous_cross_node=True)
    results=[scheduler.evaluate(mission,p,resident if p.inference_profile_id=="resident" else split) for p in scheduler.generate([resident,split])]
    assert any(any("no comparable benchmark evidence" in reason for reason in r.reasons) for r in results)
    pipeline=next(r for r in results if r.plan.mode is ExecutionMode.PIPELINE and r.plan.inference_profile_id=="split")
    assert not pipeline.eligible and any("direct Tailscale" in x for x in pipeline.reasons)
    assert scheduler.select(MissionRequirements(id="local",allow_modes={ExecutionMode.LOCAL}),[resident]).evidence.provenance == "measured"
