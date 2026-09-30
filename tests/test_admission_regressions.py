import pytest
from pydantic import ValidationError

from long_haul.models import Capacity, Resource, ResourceKind


@pytest.mark.parametrize('amount,unit,expected', [(1048576, 'bytes', 1), (1024, 'MiB', 1024), (1, 'GiB', 1024), (0, 'bytes', 0), (0.5, 'GiB', 512), (2**40, 'bytes', 2**20)])
def test_binary_capacity_conversion(amount, unit, expected):
    assert Capacity(kind='memory', amount=amount, unit=unit).as_mib() == expected
    assert Resource(id='r', kind=ResourceKind.MEMORY, capacities=[Capacity(kind='memory', amount=amount, unit=unit)]).capacity_mib() == expected


@pytest.mark.parametrize('kind,unit', [('memory', 'TOPS'), ('storage', 'watts'), ('compute', 'MiB'), ('power', 'bytes')])
def test_capacity_rejects_incompatible_units(kind, unit):
    with pytest.raises(ValidationError):
        Capacity(kind=kind, amount=1, unit=unit)


@pytest.mark.parametrize('amount', [-1, float('nan'), float('inf'), -float('inf')])
def test_capacity_rejects_invalid_amounts(amount):
    with pytest.raises(ValidationError):
        Capacity(kind='memory', amount=amount, unit='MiB')


@pytest.mark.parametrize('reverse', [False, True])
def test_resource_duplicate_kind_rejected_independent_of_order(reverse):
    capacities = [Capacity(kind='memory', amount=1, unit='GiB'), Capacity(kind='memory', amount=1, unit='MiB')]
    with pytest.raises(ValidationError, match='capacity kinds must be unique'):
        Resource(id='r', kind='gpu', capacities=capacities[::-1] if reverse else capacities)


def test_missing_memory_is_unknown_and_legacy_remains_compatible():
    assert Resource(id='r', kind='cpu').capacity_mib() is None
    assert Resource(id='r', kind='gpu', memory_mb=1024).capacity_mib() == 1024


@pytest.mark.parametrize('unit,factor', [('bytes', 1048576), ('MiB', 1), ('GiB', 1/1024)])
@pytest.mark.parametrize('required,eligible', [(512, True), (2048, False)])
def test_scheduler_normalizes_memory_requirements(unit, factor, required, eligible):
    from long_haul.models import InferenceProfile, ModelArtifact, Vessel
    from long_haul.runtime import ProfileValidation, RuntimeIdentity
    from long_haul.scheduler import MissionRequirements, Scheduler
    profile = InferenceProfile(id='p', runtime_id='fixture', strategy='resident', artifact=ModelArtifact(foundation='test'), participating_resources=['r'], requirements=[Capacity(kind='memory', amount=required * factor, unit=unit)])
    vessel = Vessel(id='v', name='v', class_name='station', resources=[Resource(id='r', kind='gpu', memory_mb=1024)])
    validation = ProfileValidation(runtime=RuntimeIdentity(runtime_id='fixture', build_id='a'), profile_id='p', artifact_key=profile.artifact.key, resources=['r'], strategy='resident', state='SUPPORTED', rationale='fixture')
    scheduler = Scheduler([vessel], validations=[validation], current_runtimes=[validation.runtime])
    result = scheduler.evaluate(MissionRequirements(id='m'), scheduler.generate([profile])[0], profile)
    assert result.eligible is eligible


def test_capacity_conversion_overflow_rejected():
    with pytest.raises(ValidationError, match='finite MiB range'):
        Capacity(kind='memory', amount=1e308, unit='GiB')
