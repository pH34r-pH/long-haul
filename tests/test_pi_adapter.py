from long_haul.adapters.pi import FakePiBackend, PiAdapter, PiEvent, PiEventKind
from long_haul.work import (
    AcceptancePredicate,
    PredicateKind,
    ProgressKind,
    WorkBudget,
    WorkContract,
    WorkSession,
)


def contract():
    return WorkContract(
        contract_id="wc-pi",
        mission_id="mission-pi",
        objective="repair bounded fixture",
        context="Only use supplied contract state.",
        allowed_paths=["src/fixture", "tests/fixture"],
        forbidden_paths=["src/fixture/secrets"],
        capabilities=["read", "edit", "test"],
        budget=WorkBudget(wall_seconds=90),
        success_predicates=[
            AcceptancePredicate(
                predicate_id="tests",
                kind=PredicateKind.TEST,
                description="fixture tests pass",
                command=["pytest", "tests/fixture"],
            )
        ],
    )


def test_contract_renders_only_approved_context_and_capabilities():
    backend = FakePiBackend([])
    session = WorkSession(contract_id="wc-pi")
    result = PiAdapter(backend).execute(contract(), session.session_id)
    assert result.request.objective == "repair bounded fixture"
    assert result.request.allowed_paths == ["src/fixture", "tests/fixture"]
    assert result.request.capabilities == ["read", "edit", "test"]
    assert "secrets" not in result.request.context


def test_structured_events_become_progress_steps_and_artifact():
    backend = FakePiBackend([
        PiEvent(kind=PiEventKind.TOOL, action="read_file", arguments={"path": "src/fixture/a.py"}, success=True),
        PiEvent(kind=PiEventKind.TOOL, action="edit_file", arguments={"path": "src/fixture/a.py"}, success=True, artifact_fingerprint="commit:def"),
        PiEvent(kind=PiEventKind.RESULT, action="test", success=True, result_fingerprint="pytest:0"),
    ])
    result = PiAdapter(backend).execute(contract(), WorkSession(contract_id="wc-pi").session_id)
    assert [step.progress for step in result.steps] == [
        ProgressKind.INFORMATION,
        ProgressKind.STATE_CHANGE,
        ProgressKind.VERIFICATION,
    ]
    assert result.artifact_fingerprint == "commit:def"


def test_interruption_is_resumable_failure_state():
    backend = FakePiBackend(
        [PiEvent(kind=PiEventKind.INTERRUPTION, action="process_interrupted", success=False)],
        interrupted=True,
        error="runner disconnected",
    )
    result = PiAdapter(backend).execute(contract(), WorkSession(contract_id="wc-pi").session_id)
    assert result.interrupted
    assert result.resumable
    assert result.error == "runner disconnected"


def test_fake_backend_is_hermetic_and_records_request():
    backend = FakePiBackend([])
    PiAdapter(backend).execute(contract(), WorkSession(contract_id="wc-pi").session_id)
    assert len(backend.requests) == 1
    assert backend.requests[0].contract_id == "wc-pi"
