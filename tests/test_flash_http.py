"""HTTP-boundary tests for the offline, synthetic Flash delivery adapter."""
import io
import json
from pathlib import Path

import pytest

from long_haul.work import (
    EvidenceState,
    FlashDeliveryFixture,
    FlashHTTPAdapter,
    PredicateEvidence,
    WorkContract,
    WorkDisposition,
    evaluate_work,
)

FIXTURE = Path(__file__).parent / "fixtures/work/flash-f0-toy.json"


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def build(clock=None, *, contract=None, verifier=None, lease_seconds=10,
          capability_state="available", observed_memory_mib=64):
    payload = json.loads(FIXTURE.read_text())
    contract = contract or WorkContract.model_validate(payload["contract"])
    expected = payload["expected_result"]

    def default_verify(current_contract, result):
        matches = (type(result) is dict and set(result) == {"count"}
                   and type(result["count"]) is int and result["count"] == expected["count"])
        evaluation = evaluate_work(current_contract, [PredicateEvidence(
            predicate_id="count", state=EvidenceState.PASS if matches else EvidenceState.FAIL,
            source="independent deterministic toy verifier",
        )])
        return evaluation.disposition is WorkDisposition.ACCEPTED

    clock = clock or FakeClock()
    fixture = FlashDeliveryFixture(
        job_id="toy-job-1", contract=contract, lease_seconds=lease_seconds,
        clock=clock, id_factory=lambda value: value, verifier=verifier or default_verify,
    )
    return FlashHTTPAdapter(
        fixture, capability_state=capability_state,
        observed_memory_mib=observed_memory_mib,
    ), fixture, clock


def request(app, path, value=None, *, raw=None, method="POST", content_type="application/json"):
    body = raw if raw is not None else json.dumps(value, allow_nan=False).encode()
    status_headers = {}
    environ = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }

    def start_response(status, headers):
        status_headers["status"] = status
        status_headers["headers"] = dict(headers)

    response = b"".join(app(environ, start_response))
    parsed = json.loads(response) if response else None
    return int(status_headers["status"].split()[0]), parsed


def claim(app):
    status, body = request(app, "/v1/jobs/claim", {})
    assert status == 200 and body["status"] == "claimed"
    return body


def lease_request(app, action, delivery, **extra):
    return request(app, f"/v1/jobs/{delivery['job_id']}/{action}", {
        "lease_id": delivery["lease_id"], **extra,
    })


def test_claim_no_work_invalid_admission_and_http_request_validation():
    app, _, _ = build(capability_state="unknown")
    assert request(app, "/v1/jobs/claim", {}) == (200, {"reason": "synthetic_f0_policy", "status": "declined"})
    app, _, _ = build()
    assert request(app, "/v1/jobs/claim", {"controller_override": True})[0] == 400
    assert request(app, "/v1/jobs/claim", raw=b"{")[0] == 400
    assert request(app, "/v1/jobs/claim", {})[0] == 200
    assert request(app, "/v1/jobs/claim", {}) == (200, {"status": "no_work"})
    assert request(app, "/v1/jobs/claim", {}, content_type="text/plain")[0] == 400
    assert request(app, "/v1/jobs/claim", {}, method="GET")[0] == 405
    assert request(app, "/v1/jobs/other/renew", {})[0] == 404


def test_malformed_and_oversized_payloads_are_rejected_at_http_boundary():
    app, _, _ = build()
    assert request(app, "/v1/jobs/claim", raw=b"[]")[0] == 400
    oversized = b" " * (16 * 1024 + 1)
    assert request(app, "/v1/jobs/claim", raw=oversized)[0] == 413
    delivery = claim(app)
    assert request(app, f"/v1/jobs/{delivery['job_id']}/renew", {"lease_id": 4})[0] == 400
    assert request(app, f"/v1/jobs/{delivery['job_id']}/complete", {
        "lease_id": delivery["lease_id"], "result": {"count": 5}, "accepted": True,
    })[0] == 400


def test_http_renew_abandon_redelivery_stale_fencing_and_invalid_result():
    app, _, _ = build()
    first = claim(app)
    assert lease_request(app, "renew", first)[0] == 200
    assert lease_request(app, "abandon", first) == (200, {"status": "abandoned"})
    second = claim(app)
    status, body = lease_request(app, "renew", first)
    assert status == 409 and body["error"] == "lease_conflict"
    status, body = lease_request(app, "complete", second, result={"count": 4, "accepted": True})
    assert status == 422 and body["error"] == "invalid_result"
    status, body = lease_request(app, "complete", second, result={"count": 5})
    assert status == 200 and body == {"result": {"count": 5}, "status": "completed"}


def test_http_lost_completion_response_retry_and_accepted_result_is_immutable():
    app, fixture, clock = build()
    delivery = claim(app)
    path = f"/v1/jobs/{delivery['job_id']}/complete"
    body = {"lease_id": delivery["lease_id"], "result": {"count": 5}}
    # First response is received by the transport but deliberately discarded by the caller.
    assert request(app, path, body)[0] == 200
    clock.advance(1000)
    assert request(app, path, body) == (200, {"result": {"count": 5}, "status": "completed"})
    body["result"]["count"] = 900
    assert fixture.completed_result == {"count": 5}
    assert request(app, "/v1/jobs/claim", {}) == (200, {"status": "no_work"})


@pytest.mark.parametrize("expiry_kind", ["lease", "wall"])
def test_expiry_during_http_verification_does_not_commit_or_exceed_budgets(expiry_kind):
    clock = FakeClock()
    payload = json.loads(FIXTURE.read_text())
    contract = WorkContract.model_validate(payload["contract"])
    contract.budget.max_attempts = 2
    lease_seconds = 5 if expiry_kind == "lease" else 100
    contract.budget.wall_seconds = 100 if expiry_kind == "lease" else 5
    calls = 0

    def slow_verify(current_contract, result):
        nonlocal calls
        calls += 1
        if calls == 1:
            clock.advance(6)
        return type(result) is dict and result == {"count": 5}

    app, fixture, _ = build(clock, contract=contract, lease_seconds=lease_seconds, verifier=slow_verify)
    first = claim(app)
    status, body = lease_request(app, "complete", first, result={"count": 5})
    assert status == 409 and body["error"] == "lease_conflict"
    assert fixture.completed_result is None
    second_status, second = request(app, "/v1/jobs/claim", {})
    if expiry_kind == "wall":
        assert (second_status, second) == (200, {"status": "no_work"})
    else:
        assert second_status == 200 and second["attempt_id"] != first["attempt_id"]
        assert lease_request(app, "complete", second, result={"count": 5})[0] == 200


def test_attempt_budget_exhaustion_returns_truthful_no_work():
    payload = json.loads(FIXTURE.read_text())
    contract = WorkContract.model_validate(payload["contract"])
    contract.budget.max_attempts = 1
    app, _, _ = build(contract=contract)
    delivery = claim(app)
    assert lease_request(app, "abandon", delivery)[0] == 200
    assert request(app, "/v1/jobs/claim", {}) == (200, {"status": "no_work"})
