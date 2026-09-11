from long_haul.fixtures.topology import anchorage
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import ProfileValidation, RuntimeIdentity, ValidationState
from long_haul.scheduler import MissionRequirements, Scheduler


def profile(): return InferenceProfile(id="p",runtime_id="fake",strategy="resident",artifact=ModelArtifact(foundation="x"),participating_resources=["ANC-G0"])
def validation(state): return ProfileValidation(runtime=RuntimeIdentity(runtime_id="fake",build_id="a"),profile_id="p",artifact_key="x",resources=["ANC-G0"],strategy="resident",state=state,rationale="test")
def test_unknown_is_excluded_unless_exploration_is_explicit():
    scheduler=Scheduler([anchorage()],validations=[validation(ValidationState.UNKNOWN)])
    candidate=scheduler.evaluate(MissionRequirements(id="x"),scheduler.generate([profile()])[0],profile())
    assert not candidate.eligible
    candidate=scheduler.evaluate(MissionRequirements(id="x",allow_unknown_runtime=True),scheduler.generate([profile()])[0],profile())
    assert candidate.eligible
def test_unsupported_never_executes():
    scheduler=Scheduler([anchorage()],validations=[validation(ValidationState.UNSUPPORTED)])
    candidate=scheduler.evaluate(MissionRequirements(id="x",allow_unknown_runtime=True),scheduler.generate([profile()])[0],profile())
    assert not candidate.eligible
