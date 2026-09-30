from datetime import UTC, datetime, timedelta

import pytest

from long_haul.benchmarks import BenchmarkObservation
from long_haul.benchmarks.store import BenchmarkStore, Workload
from long_haul.models import (
    ExecutionMode,
    InferenceProfile,
    ModelArtifact,
    Resource,
    Vessel,
)
from long_haul.runtime import ProfileValidation, RuntimeIdentity, ValidationState
from long_haul.scheduler import MissionRequirements, Scheduler


def fixture():
    profile = InferenceProfile(id='p', runtime_id='r', strategy='resident', artifact=ModelArtifact(foundation='m', revision='1', quantization='Q4'), participating_resources=['gpu'], options={'layers': 1})
    runtime = RuntimeIdentity(runtime_id='r', version='1', build_id='b', backends=['cpu'])
    validation = ProfileValidation(runtime=runtime, profile_id='p', artifact_key=profile.artifact.key, resources=['gpu'], strategy='resident', options=profile.options, state='SUPPORTED', rationale='fixture')
    workload = Workload(name='w', prompt_tokens=10, output_tokens=20, cold=True)
    mission = MissionRequirements(id='m', workload=workload, evidence_not_before=datetime(2000, 1, 1, tzinfo=UTC))
    observation = BenchmarkObservation(id='b', profile=profile, runtime=runtime, resources=['gpu'], plan_mode=ExecutionMode.LOCAL, workload=workload, provenance='measured', decode_tps=20)
    vessel = Vessel(id='v', name='v', class_name='station', resources=[Resource(id='gpu', kind='gpu', memory_mb=100)])
    return profile, runtime, validation, mission, observation, vessel


def evaluate(observations=None, *, update_profile=None, update_runtime=None, update_mission=None, update_validation=None):
    profile, runtime, validation, mission, observation, vessel = fixture()
    if update_profile:
        profile = update_profile(profile)
    if update_runtime:
        runtime = runtime.model_copy(update=update_runtime)
    if update_mission:
        mission = MissionRequirements(id='m', **update_mission)
    if update_validation:
        validation = validation.model_copy(update=update_validation)
    scheduler = Scheduler([vessel], benchmarks=observations if observations is not None else [observation], validations=[validation], current_runtimes=[runtime])
    result = scheduler.evaluate(mission, scheduler.generate([profile])[0], profile)
    return result


@pytest.mark.parametrize('change', [{'revision': '2'}, {'quantization': 'Q8'}, {'adapters': ['a']}, {'auxiliary_artifacts': ['x']}])
def test_reused_id_cannot_certify_changed_artifact(change):
    result = evaluate(update_profile=lambda p: p.model_copy(update={'artifact': p.artifact.model_copy(update=change)}))
    assert not result.eligible
    assert result.evidence is None


@pytest.mark.parametrize('change', [{'version': '2'}, {'build_id': 'c'}, {'backends': ['gpu']}, {'version': None, 'build_id': None}])
def test_current_runtime_identity_is_required(change):
    assert not evaluate(update_runtime=change).eligible


@pytest.mark.parametrize('change', [{'strategy': 'split'}, {'options': {'layers': 2}}, {'participating_resources': ['other']}])
def test_reused_id_cannot_certify_changed_profile(change):
    assert not evaluate(update_profile=lambda p: p.model_copy(update=change)).eligible


@pytest.mark.parametrize('change', [{'runtime': None}, {'error': 'failed'}, {'provenance': 'estimated'}, {'decode_tps': float('nan')}, {'decode_tps': float('inf')}, {'decode_tps': -1}, {'resources': ['gpu', 'other']}, {'workload': Workload(name='w', cold=False)}, {'timestamp': datetime(1999, 1, 1, tzinfo=UTC)}, {'timestamp': datetime.now(UTC) + timedelta(days=1)}])
def test_incompatible_observations_retained_without_ranking_advantage(change):
    observation = fixture()[4].model_copy(update=change)
    result = evaluate([observation])
    assert result.eligible  # feasibility may still permit explicit exploration
    assert result.evidence is None


def test_absent_workload_or_cutoff_retains_uncertainty():
    for values in ({}, {'workload': fixture()[3].workload}):
        assert evaluate(update_mission=values or {'workload': None}).evidence is None


def test_distinct_repeated_observations_fail_closed_in_both_orders():
    observation = fixture()[4]
    other = observation.model_copy(update={'id': 'a', 'decode_tps': 999})
    for values in ([observation, other], [other, observation]):
        assert evaluate(values).evidence is None


def test_identical_duplicate_observations_deduplicate_deterministically():
    observation = fixture()[4]
    other = observation.model_copy(update={'id': 'a'})
    for values in ([observation, other], [other, observation]):
        assert evaluate(values).evidence.id == 'b'


def test_store_bound_query_keeps_history_and_excludes_incompatible_records(tmp_path):
    profile, runtime, _, mission, observation, _ = fixture()
    store = BenchmarkStore(tmp_path / 'history.jsonl')
    store.append(observation)
    store.append(observation.model_copy(update={'id': 'old', 'runtime': None}))
    store.append(observation.model_copy(update={'id': 'failed', 'error': 'failed'}))
    assert len(store.query('p')) == 3
    assert [b.id for b in store.query_compatible(profile, runtime, mission.workload, mission.evidence_not_before)] == ['b']


def test_nonfinite_options_rejected():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        InferenceProfile.model_validate(fixture()[0].model_dump() | {'options': {'x': float('nan')}})


def test_conflicting_current_runtimes_fail_closed_independent_of_order():
    profile, runtime, validation, mission, observation, vessel = fixture()
    changed = runtime.model_copy(update={'build_id': 'other'})
    for identities in ([runtime, changed], [changed, runtime]):
        scheduler = Scheduler([vessel], benchmarks=[observation], validations=[validation], current_runtimes=identities)
        result = scheduler.evaluate(mission, scheduler.generate([profile])[0], profile)
        assert not result.eligible
        assert result.evidence is None


def test_missing_performance_metric_is_insufficient():
    assert evaluate([fixture()[4].model_copy(update={'decode_tps': None})]).evidence is None
    assert evaluate([fixture()[4].model_copy(update={'decode_tps': 0})]).evidence is not None


@pytest.mark.parametrize('amount', [-1, float('nan'), float('inf')])
def test_mission_memory_lower_bound_is_validated(amount):
    with pytest.raises(ValueError, match='finite and nonnegative'):
        MissionRequirements(id='m', minimum_memory_mib=amount)


def test_invalid_historical_capacity_is_not_rewritten(tmp_path):
    import json

    from pydantic import ValidationError

    observation = fixture()[4].model_dump(mode='json')
    observation['profile']['requirements'] = [{'kind': 'memory', 'amount': 100, 'unit': 'TOPS'}]
    path = tmp_path / 'history.jsonl'
    original = json.dumps(observation) + '\n'
    path.write_text(original)
    with pytest.raises(ValidationError, match='incompatible capacity'):
        BenchmarkStore(path).query()
    assert path.read_text() == original


def test_repeated_memory_requirements_use_maximum_not_sum():
    from long_haul.models import Capacity
    profile, runtime, validation, mission, _, vessel = fixture()
    for requirements in ([90, 10], [10, 90]):
        candidate_profile = profile.model_copy(update={'requirements': [Capacity(kind='memory', amount=amount, unit='MiB') for amount in requirements]})
        scheduler = Scheduler([vessel], validations=[validation], current_runtimes=[runtime])
        result = scheduler.evaluate(mission, scheduler.generate([candidate_profile])[0], candidate_profile)
        assert result.eligible


def test_historical_byte_requirements_remain_byte_encoded(tmp_path):
    from long_haul.models import Capacity

    observation = fixture()[4]
    profile = observation.profile.model_copy(update={'requirements': [Capacity(kind='memory', amount=1048576, unit='bytes')]})
    store = BenchmarkStore(tmp_path / 'history.jsonl')
    store.append(observation.model_copy(update={'profile': profile}))
    original = store.path.read_bytes()
    requirement = store.query()[0].profile.requirements[0]
    assert requirement.unit == 'bytes'
    assert requirement.as_mib() == 1
    assert store.path.read_bytes() == original


def test_conflicting_validations_fail_closed_independent_of_order():
    profile, runtime, validation, mission, _, vessel = fixture()
    other = validation.model_copy(update={'state': ValidationState.UNSUPPORTED})
    for records in ([validation, other], [other, validation]):
        scheduler = Scheduler([vessel], validations=records, current_runtimes=[runtime])
        assert not scheduler.evaluate(mission, scheduler.generate([profile])[0], profile).eligible


def test_unrelated_storage_does_not_supply_unknown_memory():
    from long_haul.models import Capacity

    profile, runtime, validation, mission, _, vessel = fixture()
    profile = profile.model_copy(update={'requirements': [Capacity(kind='memory', amount=1, unit='MiB')]})
    vessel.resources = [Resource(id='gpu', kind='gpu', capacities=[Capacity(kind='storage', amount=1, unit='GiB')])]
    scheduler = Scheduler([vessel], validations=[validation], current_runtimes=[runtime])
    result = scheduler.evaluate(mission, scheduler.generate([profile])[0], profile)
    assert not result.eligible
    assert 'named resource memory capacity unknown' in result.reasons


@pytest.mark.parametrize('changed', [True, 1.0])
def test_type_changed_options_cannot_reuse_validation_or_evidence(changed, tmp_path):
    profile, runtime, validation, mission, observation, vessel = fixture()
    changed_profile = profile.model_copy(update={'options': {'layers': changed}})
    scheduler = Scheduler([vessel], benchmarks=[observation], validations=[validation], current_runtimes=[runtime])
    result = scheduler.evaluate(mission, scheduler.generate([changed_profile])[0], changed_profile)
    assert not result.eligible
    assert result.evidence is None
    store = BenchmarkStore(tmp_path / 'typed-options.jsonl')
    store.append(observation)
    assert store.query_compatible(changed_profile, runtime, mission.workload, mission.evidence_not_before) == []


def test_nested_options_object_order_is_irrelevant_but_array_order_matters(tmp_path):
    profile, runtime, validation, mission, observation, vessel = fixture()
    original = {'nested': {'a': True, 'b': 1.0}, 'ordered': [1, 2]}
    reordered = {'ordered': [1, 2], 'nested': {'b': 1.0, 'a': True}}
    profile = profile.model_copy(update={'options': original})
    validation = validation.model_copy(update={'options': original})
    observation = observation.model_copy(update={'profile': profile})
    scheduler = Scheduler([vessel], benchmarks=[observation], validations=[validation], current_runtimes=[runtime])
    equivalent = profile.model_copy(update={'options': reordered})
    result = scheduler.evaluate(mission, scheduler.generate([equivalent])[0], equivalent)
    assert result.eligible
    assert result.evidence is not None
    store = BenchmarkStore(tmp_path / 'nested-options.jsonl')
    store.append(observation)
    assert len(store.query_compatible(equivalent, runtime, mission.workload, mission.evidence_not_before)) == 1
    reversed_array = profile.model_copy(update={'options': original | {'ordered': [2, 1]}})
    result = scheduler.evaluate(mission, scheduler.generate([reversed_array])[0], reversed_array)
    assert not result.eligible
    assert result.evidence is None
    assert store.query_compatible(reversed_array, runtime, mission.workload, mission.evidence_not_before) == []
