import pytest

from long_haul.work import (
    AcceptancePredicate,
    EvidenceState,
    EvaluationRunner,
    EvaluatorIdentity,
    PredicateKind,
    WorkBudget,
    WorkContract,
    WorkDisposition,
    evaluate_packet,
    reopen_regressions,
)


class ReadOnlyTarget:
    def __init__(self, fingerprint="abc", outcomes=None):
        self._fingerprint = fingerprint
        self.outcomes = outcomes or {}

    @property
    def fingerprint(self):
        return self._fingerprint

    def inspect(self, path):
        if path == "missing":
            raise FileNotFoundError(path)
        return "present"

    def run_check(self, command):
        return self.outcomes.get(tuple(command), (EvidenceState.UNKNOWN, "not run"))


def contract():
    return WorkContract(
        contract_id="wc-1",
        mission_id="mission-1",
        objective="bounded repair",
        allowed_paths=["src"],
        budget=WorkBudget(wall_seconds=60),
        success_predicates=[
            AcceptancePredicate(
                predicate_id="tests",
                kind=PredicateKind.TEST,
                description="tests pass",
                command=["pytest"],
            )
        ],
    )


def evaluator():
    return EvaluatorIdentity(evaluator_id="qa-fixture", version="1", runtime="pytest-fixture")


def test_runner_exposes_no_mutation_interface():
    target = ReadOnlyTarget()
    assert not hasattr(target, "write")
    assert not hasattr(target, "edit")
    runner = EvaluationRunner(target, evaluator())
    assert not hasattr(runner.target, "write")


def test_pass_fail_and_unknown_are_external_evidence():
    for state, expected in [
        (EvidenceState.PASS, WorkDisposition.ACCEPTED),
        (EvidenceState.FAIL, WorkDisposition.REJECTED),
        (EvidenceState.UNKNOWN, WorkDisposition.INCOMPLETE),
    ]:
        runner = EvaluationRunner(ReadOnlyTarget(outcomes={("pytest",): (state, state.value)}), evaluator())
        packet, result = runner.evaluate(contract(), "attempt-1", f"packet-{state.value}", "commit:abc")
        assert packet.candidate.fingerprint == "abc"
        assert packet.attempt_id == "attempt-1"
        assert result.disposition is expected


def test_changed_candidate_invalidates_evidence():
    runner = EvaluationRunner(ReadOnlyTarget(outcomes={("pytest",): (EvidenceState.PASS, "ok")}), evaluator())
    packet, _ = runner.evaluate(contract(), "attempt-1", "packet-1", "commit:abc")
    result = evaluate_packet(contract(), packet, candidate_fingerprint="def")
    assert result.disposition is WorkDisposition.INCOMPLETE
    assert "stale evidence" in result.reasons[0]


def test_regression_reopens_previously_verified_predicate():
    passed, _ = EvaluationRunner(
        ReadOnlyTarget("abc", {("pytest",): (EvidenceState.PASS, "ok")}), evaluator()
    ).evaluate(contract(), "attempt-1", "packet-1", "commit:abc")
    failed, result = EvaluationRunner(
        ReadOnlyTarget("def", {("pytest",): (EvidenceState.FAIL, "regression")}), evaluator()
    ).evaluate(contract(), "attempt-2", "packet-2", "commit:def")
    reopen_regressions(passed, failed)
    assert failed.regressions == ["tests"]
    assert passed.verified == ["tests"]
    assert result.disposition is WorkDisposition.REJECTED


def test_packet_rejects_cross_contract_use():
    runner = EvaluationRunner(ReadOnlyTarget(outcomes={("pytest",): (EvidenceState.PASS, "ok")}), evaluator())
    packet, _ = runner.evaluate(contract(), "attempt-1", "packet-1", "commit:abc")
    other = contract().model_copy(update={"contract_id": "wc-2"})
    with pytest.raises(ValueError):
        evaluate_packet(other, packet, "abc")
