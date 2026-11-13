"""Synthetic per-file evaluator admission; trusted lineage is supplied by control."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from repository_evaluator_fixture import evaluation_input_admission, setup
from test_repository_evaluator import FixtureSandbox

from long_haul.resource_policy import ResourceAction, ResourceLifecycle
from long_haul.work.repository_evaluator import RepositoryEvaluationRunner
from long_haul.work.repository_input_admission import MAX_EVALUATION_LINEAGE_ENTRIES


def test_exact_candidate_admission_covers_unchanged_and_changed_files(tmp_path):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    assert {item.path for item in admission.files} == {"program.py", "README.md"}
    admission.authorize(snapshot, contract, profile, "attempt", datetime.now(UTC))


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "content", "candidate", "attempt"])
def test_candidate_admission_rejects_incomplete_or_mismatched_bindings(tmp_path, change):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    first = admission.files[0]
    if change == "missing":
        admission = replace(admission, files=admission.files[1:])
    elif change == "extra":
        admission = replace(admission, files=(*admission.files, replace(first, path="extra")))
    elif change == "duplicate":
        admission = replace(admission, files=(*admission.files, first))
    elif change == "content":
        admission = replace(admission, files=(replace(first, content_digest="sha256:" + "0" * 64), *admission.files[1:]))
    elif change == "candidate":
        admission = replace(admission, candidate_fingerprint="sha256:" + "0" * 64)
    with pytest.raises(PermissionError, match="candidate input admission"):
        admission.authorize(snapshot, contract, profile, "other" if change == "attempt" else "attempt", datetime.now(UTC))


@pytest.mark.parametrize("change", ["read", "evaluate", "retracted", "expired", "fingerprint"])
def test_candidate_admission_rechecks_each_current_policy(tmp_path, change):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    entry = admission.files[0]
    parent = entry.parents[0]
    if change in {"read", "evaluate"}:
        action = ResourceAction.READ if change == "read" else ResourceAction.EVALUATE
        parent.profile.permissions[0].actions = [value for value in parent.profile.permissions[0].actions if value != action]
    elif change == "retracted":
        parent.profile.lifecycle = ResourceLifecycle.RETRACTED
    elif change == "expired":
        parent.profile.privacy.retention_until = datetime.now(UTC) - timedelta(seconds=1)
    if change != "fingerprint":
        parent = replace(parent, expected_policy_fingerprint=parent.profile.fingerprint)
        admission = replace(admission, files=(replace(entry, parents=(parent,)), *admission.files[1:]))
    else:
        parent.profile.policy_assigner = "changed-without-readmission"
    with pytest.raises(PermissionError, match="candidate input admission"):
        admission.authorize(snapshot, contract, profile, "attempt", datetime.now(UTC))


def test_every_declared_lineage_parent_must_allow_evaluation(tmp_path):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    first, second = admission.files
    inherited = second.parents[0]
    inherited.profile.permissions[0].actions = [ResourceAction.READ]
    inherited = replace(inherited, expected_policy_fingerprint=inherited.profile.fingerprint)
    admission = replace(admission, files=(replace(first, parents=(*first.parents, inherited)), second))
    with pytest.raises(PermissionError, match="denies evaluation use"):
        admission.authorize(snapshot, contract, profile, "attempt", datetime.now(UTC))


@pytest.mark.parametrize("now", [None, datetime(2026, 1, 1)])
def test_candidate_admission_rejects_invalid_clock(tmp_path, now):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    with pytest.raises(PermissionError, match="candidate input admission"):
        admission.authorize(snapshot, contract, profile, "attempt", now)


def test_candidate_admission_bounds_total_lineage(tmp_path):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    first, second = admission.files
    oversized = replace(first, parents=first.parents * MAX_EVALUATION_LINEAGE_ENTRIES)
    with pytest.raises(PermissionError, match="lineage limits"):
        replace(admission, files=(oversized, second)).authorize(
            snapshot, contract, profile, "attempt", datetime.now(UTC)
        )


def test_conflicting_policies_for_same_resource_across_files_are_rejected(tmp_path):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    first, second = admission.files
    parent = second.parents[0]
    parent.profile.identity.resource_id = first.parents[0].profile.identity.resource_id
    parent.request.resource_id = parent.profile.identity.resource_id
    parent = replace(parent, expected_policy_fingerprint=parent.profile.fingerprint,
                     expected_request_fingerprint=parent.request.fingerprint)
    admission = replace(admission, files=(first, replace(second, parents=(parent,))))
    with pytest.raises(PermissionError, match="conflicting policy identities"):
        admission.authorize(snapshot, contract, profile, "attempt", datetime.now(UTC))


def test_runner_rechecks_admission_after_candidate_execution(tmp_path):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")

    class RevokingSandbox(FixtureSandbox):
        def execute(self, target, command):
            result = super().execute(target, command)
            admission.files[0].parents[0].profile.lifecycle = ResourceLifecycle.RETRACTED
            return result

    sandbox = RevokingSandbox(profile.environment)
    with pytest.raises(PermissionError, match="candidate input admission"):
        RepositoryEvaluationRunner(sandbox).evaluate(contract, snapshot, profile, attempt_id="attempt",
                                                     packet_id="packet", candidate_admission=admission)
    assert len(sandbox.calls) == 1


def test_runner_admits_complete_candidate_and_denies_unmodified_file_without_policy(tmp_path):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    assert not contract.permits_path("README.md")
    missing = replace(admission, files=tuple(entry for entry in admission.files if entry.path != "README.md"))
    sandbox = FixtureSandbox(profile.environment)
    runner = RepositoryEvaluationRunner(sandbox)
    with pytest.raises(PermissionError, match="incomplete"):
        runner.evaluate(contract, snapshot, profile, attempt_id="attempt", packet_id="denied",
                        candidate_admission=missing)
    assert not sandbox.calls
    _, result = runner.evaluate(contract, snapshot, profile, attempt_id="attempt", packet_id="accepted",
                                candidate_admission=admission)
    assert result.disposition.value == "ACCEPTED" and sandbox.calls


def test_runner_denies_unmodified_file_with_explicit_evaluation_prohibition(tmp_path):
    _, contract, snapshot, profile = setup(tmp_path, FixtureSandbox.identity)
    admission = evaluation_input_admission(contract, snapshot, profile, "attempt")
    entries = []
    for entry in admission.files:
        if entry.path == "README.md":
            parent = entry.parents[0]
            parent.profile.permissions[0].actions = [ResourceAction.READ]
            parent = replace(parent, expected_policy_fingerprint=parent.profile.fingerprint)
            entry = replace(entry, parents=(parent,))
        entries.append(entry)
    sandbox = FixtureSandbox(profile.environment)
    with pytest.raises(PermissionError, match="denies evaluation use"):
        RepositoryEvaluationRunner(sandbox).evaluate(
            contract, snapshot, profile, attempt_id="attempt", packet_id="denied",
            candidate_admission=replace(admission, files=tuple(entries)),
        )
    assert not sandbox.calls
