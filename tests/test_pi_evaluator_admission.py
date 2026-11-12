"""Synthetic host admission; real isolation is qualified in the Pi/gVisor job."""
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pi_worker_fixture import public_evaluator_admission
from repository_evaluator_fixture import setup
from test_private_evaluator_commitments import private_evaluation_fixture
from test_repository_evaluator import FixtureSandbox

from long_haul.adapters.pi_evaluator_admission import EvaluatorWorkerAdmission
from long_haul.resource_policy import ResourceLifecycle


def public_fixture(tmp_path):
    _, contract, _, profile = setup(tmp_path, FixtureSandbox.identity)
    attempt = SimpleNamespace(session_id="fixture-session", crew_id="fixture-crew",
                              repository_id=contract.repository_work.source.repository_id)
    now = datetime.now(UTC)
    return contract, attempt, now, public_evaluator_admission(profile, contract, attempt, now)


def test_public_legacy_profile_requires_exact_current_distribution_permission(tmp_path):
    contract, attempt, now, admission = public_fixture(tmp_path)
    admission.authorize(contract, attempt, now)
    assert "[-5, 2]" not in repr(admission)
    policy = admission.policy.model_copy(deep=True)
    policy.permissions[0].actions = [action for action in policy.permissions[0].actions if action != "distribute"]
    denied = replace(admission, policy=policy, expected_policy_fingerprint=policy.fingerprint)
    with pytest.raises(PermissionError, match="not explicitly admitted"):
        denied.authorize(contract, attempt, now)
    policy.lifecycle = ResourceLifecycle.RETRACTED
    with pytest.raises(PermissionError, match="stale or mismatched"):
        denied.authorize(contract, attempt, now)
    with pytest.raises(PermissionError, match="stale or mismatched"):
        admission.authorize(contract, replace_attempt(attempt, "another-session"), now)
    extension = contract.repository_work.model_copy(update={"sealed_evaluator":
        contract.repository_work.sealed_evaluator.model_copy(update={"reference": "urn:unknown-version"})})
    with pytest.raises(PermissionError, match="legacy profile version"):
        admission.authorize(contract.model_copy(update={"repository_work": extension}), attempt, now)


def replace_attempt(attempt, session_id):
    return SimpleNamespace(session_id=session_id, crew_id=attempt.crew_id, repository_id=attempt.repository_id)


def test_private_keyed_evaluator_requires_exact_contract_binding(tmp_path):
    _, profile, contract, _, _ = private_evaluation_fixture(tmp_path)
    admission = EvaluatorWorkerAdmission(profile)
    attempt = SimpleNamespace(session_id="session")
    admission.authorize(contract, attempt, datetime.now(UTC))
    extension = contract.repository_work.model_copy(update={"sealed_evaluator":
        contract.repository_work.sealed_evaluator.model_copy(update={"reference": "urn:legacy"})})
    with pytest.raises(PermissionError, match="keyed profile version"):
        admission.authorize(contract.model_copy(update={"repository_work": extension}), attempt, datetime.now(UTC))
