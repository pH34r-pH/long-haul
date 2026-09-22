from long_haul.adapters.pi_rpc import (
    PI_PACKAGE_VERSION,
    PI_UPSTREAM_REVISION,
    attempt_step_from_pi_event,
    pi_prompt,
    pi_tools_for_contract,
)
from long_haul.work import (
    AcceptancePredicate,
    PredicateKind,
    ProgressKind,
    TaskDigest,
    WorkBudget,
    WorkContract,
)


def contract(capabilities):
    return WorkContract(
        contract_id="wc-pi",
        mission_id="mission-pi",
        objective="repair the bounded fixture",
        context="preserve public behavior",
        allowed_paths=["fixture"],
        capabilities=capabilities,
        budget=WorkBudget(wall_seconds=60),
        success_predicates=[
            AcceptancePredicate(
                predicate_id="tests",
                kind=PredicateKind.TEST,
                description="fixture tests pass",
                command=["pytest"],
            )
        ],
    )


def test_pi_pin_is_explicit():
    assert PI_PACKAGE_VERSION == "0.87.0"
    assert len(PI_UPSTREAM_REVISION) == 40


def test_capabilities_map_to_native_pi_allowlist():
    assert pi_tools_for_contract(contract([])) == []
    assert pi_tools_for_contract(contract(["read"])) == ["read"]
    assert pi_tools_for_contract(contract(["read", "edit", "test"])) == ["read", "bash", "edit", "write"]


def test_prompt_uses_contract_and_bounded_digest():
    item = contract(["read"])
    digest = TaskDigest(contract_id=item.contract_id, text="Remaining subgoals:\n- inspect failure", max_chars=200)
    rendered = pi_prompt(item, digest)
    assert "repair the bounded fixture" in rendered
    assert "preserve public behavior" in rendered
    assert "inspect failure" in rendered


def test_actual_upstream_tool_end_shape_translates_to_attempt_step():
    event = {
        "type": "tool_execution_end",
        "toolCallId": "call-1",
        "toolName": "edit",
        "result": {"content": [{"type": "text", "text": "done"}], "details": {}},
        "isError": False,
    }
    step = attempt_step_from_pi_event(event, "session-1:call-1")
    assert step.action == "edit"
    assert step.progress is ProgressKind.STATE_CHANGE
    assert step.arguments == {}


def test_non_completion_event_is_ignored():
    assert attempt_step_from_pi_event({"type": "tool_execution_start", "toolName": "read"}, "1") is None
