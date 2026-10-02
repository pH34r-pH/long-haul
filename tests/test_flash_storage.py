"""Offline qualification of durable Flash queue/blob transitions."""
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from long_haul.work import (
    DurableFlashDelivery,
    EvidenceState,
    FlashStorageOptions,
    LeaseError,
    PredicateEvidence,
    VerificationError,
    WorkContract,
    WorkDisposition,
    evaluate_work,
)

FIXTURE = Path(__file__).parent / "fixtures/work/flash-f0-toy.json"


class StorageError(RuntimeError):
    def __init__(self, status_code=None):
        self.status_code = status_code


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def advance(self, value):
        self.now += value


class Blob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_blob(self, data, *, overwrite, etag=None, match_condition=None):
        current = self.store.rows.get(self.name)
        if overwrite and self.store.conflict_next_write:
            self.store.conflict_next_write = False
            raise StorageError(412)
        if not overwrite and current is not None:
            raise StorageError(409)
        if overwrite and (current is None or current[1] != etag):
            raise StorageError(412)
        version = (current[1] + 1) if current else 1
        self.store.rows[self.name] = (bytes(data), version)

    def download_blob(self):
        if self.name not in self.store.rows:
            raise StorageError(404)
        data, etag = self.store.rows[self.name]
        after_read = self.store.after_download

        class Download:
            properties = type("Properties", (), {"etag": etag})()

            def readall(inner_self):
                if after_read is not None:
                    after_read(self.name)
                return data

        return Download()


class Container:
    def __init__(self):
        self.rows = {}
        self.conflict_next_write = False
        self.after_download = None

    def get_blob_client(self, name):
        return Blob(self, name)

    def list_blobs(self, name_starts_with="", results_per_page=256):
        items = [type("Item", (), {"name": name}) for name in sorted(self.rows)
                 if name.startswith(name_starts_with)]
        return BlobPager(items, results_per_page)


class BlobPager:
    def __init__(self, items, page_size):
        self.items, self.page_size = items, page_size

    def by_page(self, continuation_token=None):
        return BlobPageIterator(self.items, self.page_size, continuation_token)


class BlobPageIterator:
    def __init__(self, items, page_size, continuation_token):
        self.items, self.page_size = items, page_size
        self.offset = int(continuation_token or 0)
        self.continuation_token = continuation_token

    def __iter__(self):
        return self

    def __next__(self):
        if self.offset >= len(self.items):
            raise StopIteration
        start = self.offset
        end = min(start + self.page_size, len(self.items))
        self.offset = end
        self.continuation_token = str(end) if end < len(self.items) else None
        return iter(self.items[start:end])


@dataclass
class Message:
    content: str
    id: str
    pop_receipt: str
    visible_at: float


class Queue:
    def __init__(self, clock):
        self.clock, self.messages, self.serial = clock, [], 0
        self.delete_error = None
        self.delete_after_error = False
        self.update_delay_seconds = 0
        self.after_update = None

    def send_message(self, content, **kwargs):
        self.serial += 1
        self.messages.append(Message(content, str(self.serial), f"r{self.serial}-0", self.clock()))

    def receive_message(self, visibility_timeout):
        for message in self.messages:
            if self.clock() >= message.visible_at:
                version = int(message.pop_receipt.rsplit("-", 1)[1]) + 1
                message.pop_receipt = f"r{message.id}-{version}"
                message.visible_at = self.clock() + visibility_timeout
                # Azure SDK receive results are detached snapshots, not the
                # mutable queue's backing record.
                return Message(message.content, message.id, message.pop_receipt, message.visible_at)
        return None

    def update_message(self, message, pop_receipt, visibility_timeout):
        current = next((item for item in self.messages if item.id == message.id), None)
        if current is None or pop_receipt != current.pop_receipt:
            raise StorageError(404)
        index = int(current.pop_receipt.rsplit("-", 1)[1]) + 1
        current.pop_receipt = f"r{current.id}-{index}"
        current.visible_at = self.clock() + visibility_timeout
        if self.update_delay_seconds:
            self.clock.advance(self.update_delay_seconds)
            self.update_delay_seconds = 0
        if self.after_update is not None:
            callback, self.after_update = self.after_update, None
            callback()
        return type("Update", (), {"pop_receipt": current.pop_receipt})()

    def delete_message(self, message, pop_receipt):
        if self.delete_error:
            error, self.delete_error = self.delete_error, None
            current = next((item for item in self.messages if item.id == message.id), None)
            if self.delete_after_error and current is not None and pop_receipt == current.pop_receipt:
                self.messages.remove(current)
                self.delete_after_error = False
                raise error
            raise error
        current = next((item for item in self.messages if item.id == message.id), None)
        if current is None or pop_receipt != current.pop_receipt:
            raise StorageError(404)
        self.messages.remove(current)


def contract(*, attempts=3, wall=60):
    value = json.loads(FIXTURE.read_text())["contract"]
    value["budget"]["max_attempts"] = attempts
    value["budget"]["wall_seconds"] = wall
    return WorkContract.model_validate(value)


def verifier(current, result):
    valid = type(result) is dict and set(result) == {"count"} and type(result["count"]) is int and result["count"] == 5
    evaluation = evaluate_work(current, [PredicateEvidence(
        predicate_id="count", state=EvidenceState.PASS if valid else EvidenceState.FAIL,
        source="test verifier",
    )])
    return evaluation.disposition is WorkDisposition.ACCEPTED


class Fixture:
    def __init__(self, *, clock=None, queue=None, container=None, notifier=None, tokens=None, **kwargs):
        self.clock = clock or Clock()
        self.queue = queue or Queue(self.clock)
        self.container = container or Container()
        self.notified = [] if notifier is None else notifier
        self.tokens = iter(tokens or [f"opaque-test-claim-token-{i:032d}" for i in range(20)])
        self.adapter = DurableFlashDelivery(
            self.queue, self.container, options=FlashStorageOptions(
                lease_seconds=10, token_factory=lambda: next(self.tokens),
            ), clock=self.clock,
            verifier=kwargs.get("verifier", verifier),
            notifier=lambda *args: self.notified.append(args),
        )

    def add_job(self, **kwargs):
        self.adapter.enqueue("job-1", contract(**kwargs))


def test_competing_claims_share_one_conditional_per_job_record_and_compact_queue_reference():
    f = Fixture()
    f.add_job()
    # Simulate duplicate producer delivery. Blob generations, not queue
    # visibility alone, choose the winner across two independent adapters.
    f.queue.send_message(f.queue.messages[0].content)
    assert len(f.queue.messages) == 2
    assert len(f.queue.messages[0].content) < 100
    assert "pop_receipt" not in f.queue.messages[0].content
    first = f.adapter.claim()
    assert first is not None
    competing = DurableFlashDelivery(
        f.queue, f.container,
        options=FlashStorageOptions(lease_seconds=10, token_factory=lambda: "opaque-test-competing-token-" + "x" * 32),
        clock=f.clock, verifier=verifier,
        notifier=lambda *_: None,
    )
    second = competing.claim()
    assert second is None
    assert first[0].lease_id == first[1]
    assert first[0].attempt_id == "job-1-attempt-1"
    assert sum(name.startswith("flash-jobs/v1/") for name in f.container.rows) == 1


def test_process_claim_map_expires_and_obeys_configured_capacity():
    f = Fixture()
    f.adapter.options = FlashStorageOptions(
        lease_seconds=10, token_factory=lambda: next(f.tokens), max_local_claims=1,
    )
    f.adapter.lease_seconds = f.adapter.visibility_seconds = 10
    f.add_job()
    _delivery, token = f.adapter.claim()
    f.adapter.enqueue("job-2", contract())
    assert f.adapter.claim() is None
    assert len(f.adapter._claims) == 1
    f.clock.advance(10)
    assert f.adapter.claim() is not None
    assert token not in f.adapter._claims


def test_notification_recovery_scan_respects_configured_bound():
    f = Fixture()
    f.add_job()
    f.adapter.enqueue("job-2", contract())
    for _ in range(2):
        _delivery, token = f.adapter.claim()
        assert f.adapter.complete(token, {"count": 5}) == {"count": 5}
    for job_id in ("job-1", "job-2"):
        state, etag = f.adapter._read(job_id)
        state.notification_pending = True
        state.notification_sent = False
        f.adapter._write(state, etag)
    f.notified.clear()
    bounded = DurableFlashDelivery(
        f.queue, f.container, options=FlashStorageOptions(lease_seconds=10, recovery_scan_limit=1),
        clock=f.clock, verifier=verifier, notifier=lambda *args: f.notified.append(args),
    )
    bounded.recover_notifications()
    assert len(f.notified) == 1


def test_notification_continuation_is_fair_across_process_restarts_beyond_each_page():
    f = Fixture()
    for number in range(5):
        f.adapter.enqueue(f"job-{number}", contract())
        state, etag = f.adapter._read(f"job-{number}")
        state.generation = 1
        state.accepted_generation = 1
        state.accepted_attempt_id = f"job-{number}-attempt-1"
        state.accepted_claim_hash = f"claim-hash-{number}"
        state.accepted_result_json = '{"count":5}'
        state.notification_pending = True
        f.adapter._write(state, etag)

    f.notified.clear()
    for _ in range(3):
        restarted = DurableFlashDelivery(
            f.queue, f.container,
            options=FlashStorageOptions(lease_seconds=10, recovery_scan_limit=2),
            clock=f.clock, verifier=verifier,
            notifier=lambda *args: f.notified.append(args),
        )
        restarted.recover_notifications()
    assert {item[0] for item in f.notified} == {f"job-{number}" for number in range(5)}
    assert len(f.notified) == 5
    cursor, _ = f.adapter._read_notification_cursor()
    assert cursor.continuation_token is None
    assert all(f.adapter._read(f"job-{number}")[0].notification_sent for number in range(5))


def test_accepted_claim_retry_uses_index_beyond_prior_scan_limit():
    f = Fixture()
    f.adapter.options = FlashStorageOptions(
        lease_seconds=10, token_factory=lambda: next(f.tokens), recovery_scan_limit=1,
    )
    for number in range(5):
        f.adapter.enqueue(f"job-{number}", contract())
    token_for_last = None
    for number in range(5):
        delivery, token = f.adapter.claim()
        assert delivery.job_id == f"job-{number}"
        f.adapter.complete(token, {"count": 5})
        if number == 4:
            token_for_last = token
    index_name = f.adapter._claim_index_name(f.adapter._token_hash(token_for_last))
    assert index_name in f.container.rows
    restarted = DurableFlashDelivery(
        f.queue, f.container, options=FlashStorageOptions(lease_seconds=10),
        clock=f.clock, verifier=verifier, notifier=lambda *_: None,
    )
    assert restarted.complete(token_for_last, {"count": 5}) == {"count": 5}


def test_download_content_and_etag_are_one_snapshot_when_acceptance_races_claim():
    f = Fixture()
    f.add_job()
    reads = 0

    def competing_acceptance(blob_name):
        nonlocal reads
        if blob_name != "flash-jobs/v1/job-1.json":
            return
        reads += 1
        if reads != 2:  # recovery scan is first; claim's state read is second
            return
        name = "flash-jobs/v1/job-1.json"
        raw, version = f.container.rows[name]
        accepted = json.loads(raw)
        accepted.update({
            "generation": 7,
            "attempt_number": 1,
            "lease_id": "newer-generation-hash",
            "lease_expires_at": f.clock() + 10,
            "accepted_generation": 7,
            "accepted_attempt_id": "job-1-attempt-1",
            "accepted_claim_hash": "accepted-claim-hash",
            "accepted_result_json": '{"count":5}',
            "notification_pending": True,
        })
        f.container.rows[name] = (json.dumps(accepted).encode(), version + 1)

    f.container.after_download = competing_acceptance
    assert f.adapter.claim() is None
    persisted, _ = f.adapter._read("job-1")
    assert persisted.generation == 7
    assert persisted.accepted_result_json == '{"count":5}'
    assert persisted.accepted_generation == 7


def test_rotating_pop_receipt_stays_server_side_and_old_generation_is_fenced_after_expiry():
    f = Fixture()
    f.add_job()
    first, token1 = f.adapter.claim()
    renewed = f.adapter.renew(token1)
    assert renewed.lease_expires_at == 110
    assert f.queue.messages[0].pop_receipt != "r1-0"
    f.clock.advance(10)
    second, token2 = f.adapter.claim()
    assert second.attempt_id != first.attempt_id
    with pytest.raises(LeaseError):
        f.adapter.renew(token1)
    assert f.adapter.complete(token2, {"count": 5}) == {"count": 5}


def test_duplicate_reference_cannot_terminalize_a_live_final_attempt():
    f = Fixture()
    f.add_job(attempts=1)
    f.queue.send_message(f.queue.messages[0].content)
    _first, token = f.adapter.claim()
    duplicate_adapter = DurableFlashDelivery(
        f.queue, f.container, options=FlashStorageOptions(lease_seconds=10),
        clock=f.clock, verifier=verifier, notifier=lambda *_: None,
    )
    assert duplicate_adapter.claim() is None
    assert f.adapter._read("job-1")[0].terminal_reason is None
    assert f.adapter.complete(token, {"count": 5}) == {"count": 5}
    f.clock.advance(10)
    assert duplicate_adapter.claim() is None  # accepted state settles the duplicate
    assert not f.queue.messages
    assert f.adapter._read("job-1")[0].terminal_reason is None


@pytest.mark.parametrize(("wall", "lease", "delay", "can_reclaim"), [
    (60, 10, 11, True),
    (5, 100, 6, False),
])
def test_delayed_queue_renew_cannot_revive_expired_lease_or_deadline(wall, lease, delay, can_reclaim):
    f = Fixture()
    f.adapter.lease_seconds = lease
    f.adapter.visibility_seconds = lease
    f.add_job(wall=wall)
    _delivery, token = f.adapter.claim()
    original_expiry = f.adapter._read("job-1")[0].lease_expires_at
    f.queue.update_delay_seconds = delay
    with pytest.raises(LeaseError, match="expired"):
        f.adapter.renew(token)
    assert f.adapter._read("job-1")[0].lease_expires_at == original_expiry
    context = f.adapter._claims[token]
    assert context.pop_receipt == f.queue.messages[0].pop_receipt
    if can_reclaim:
        next_delivery = f.adapter.claim()
        assert next_delivery is not None and next_delivery[0].attempt_id != _delivery.attempt_id
    else:
        assert f.adapter.claim() is None
        assert f.adapter._read("job-1")[0].terminal_reason == "wall_budget_expired"


def test_renew_rechecks_generation_after_queue_io_before_conditional_commit():
    f = Fixture()
    f.add_job()
    _delivery, token = f.adapter.claim()

    def competing_claim_generation():
        state, etag = f.adapter._read("job-1")
        state.generation += 1
        state.lease_id = "newer-generation-hash"
        f.adapter._write(state, etag)

    f.queue.after_update = competing_claim_generation
    with pytest.raises(LeaseError, match="stale"):
        f.adapter.renew(token)
    state, _ = f.adapter._read("job-1")
    assert state.generation == 2
    assert state.lease_id == "newer-generation-hash"
    assert f.adapter._claims[token].pop_receipt == f.queue.messages[0].pop_receipt


def test_verification_expiry_generation_and_wall_attempt_budgets_fence_finalization():
    f_clock = Clock()
    calls = 0

    def slow_once(current, result):
        nonlocal calls
        calls += 1
        if calls == 1:
            f_clock.advance(11)
        return verifier(current, result)

    f = Fixture(clock=f_clock, verifier=slow_once)
    f.add_job(attempts=2)
    first, token1 = f.adapter.claim()
    with pytest.raises(LeaseError):
        f.adapter.complete(token1, {"count": 5})
    assert f.adapter.completed_result("job-1") is None
    second, token2 = f.adapter.claim()
    assert second.attempt_id != first.attempt_id
    assert f.adapter.complete(token2, {"count": 5}) == {"count": 5}

    g = Fixture(clock=Clock())
    g.add_job(attempts=1)
    _delivery, token = g.adapter.claim()
    g.adapter.abandon(token)
    assert g.adapter.claim() is None  # exhausted attempt budget
    assert not g.queue.messages
    assert g.adapter._read("job-1")[0].terminal_reason == "attempt_budget_exhausted"

    h = Fixture(clock=Clock())
    h.add_job(attempts=3, wall=5)
    _delivery, token = h.adapter.claim()
    h.clock.advance(5)
    with pytest.raises(LeaseError):
        h.adapter.complete(token, {"count": 5})
    h.clock.advance(5)  # Queue visibility has expired; the deadline is still authoritative.
    assert h.adapter.claim() is None
    assert not h.queue.messages
    assert h.adapter._read("job-1")[0].terminal_reason == "wall_budget_expired"


def test_etag_conflict_prevents_acceptance_and_bad_verification_never_settles():
    f = Fixture()
    f.add_job()
    _delivery, token = f.adapter.claim()
    f.container.conflict_next_write = True  # another claimant changed the ETag before finalization
    with pytest.raises(LeaseError):
        f.adapter.complete(token, {"count": 5})
    assert f.adapter.completed_result("job-1") is None
    assert f.queue.messages

    g = Fixture()
    g.add_job()
    _delivery, token = g.adapter.claim()
    with pytest.raises(VerificationError):
        g.adapter.complete(token, {"count": 4, "accepted": True})
    assert g.adapter.completed_result("job-1") is None
    assert g.queue.messages


def test_acceptance_precedes_delete_lost_delete_response_and_restart_does_not_reexecute():
    f = Fixture()
    f.add_job()
    _delivery, token = f.adapter.claim()
    f.queue.delete_error = TimeoutError("response lost after server delete")
    # Acceptance is already durable when Queue settlement reports an ambiguous failure.
    with pytest.raises(TimeoutError):
        f.adapter.complete(token, {"count": 5})
    assert f.adapter.completed_result("job-1") == {"count": 5}
    assert len(f.queue.messages) == 1
    # Restart drops the token/receipt map. After queue visibility expiry it
    # observes acceptance and settles the trigger without re-running work.
    f.clock.advance(10)
    restarted = DurableFlashDelivery(
        f.queue, f.container, options=FlashStorageOptions(
            lease_seconds=10, token_factory=lambda: "opaque-test-claim-token-" + "x" * 32,
        ), clock=f.clock, verifier=lambda *_: pytest.fail("re-executed accepted work"),
        notifier=lambda *args: f.notified.append(args),
    )
    assert restarted.claim() is None
    assert not f.queue.messages
    assert f.adapter.completed_result("job-1") == {"count": 5}


def test_notification_recovery_is_idempotency_keyed_and_handles_crash_after_notify():
    f = Fixture()
    f.add_job()
    _delivery, token = f.adapter.claim()
    f.adapter.complete(token, {"count": 5})
    assert f.notified == [("job-1", "job-1-attempt-1", {"count": 5})]

    # Model a crash after external publish but before the notification flag CAS.
    state, etag = f.adapter._read("job-1")
    state.notification_pending = True
    state.notification_sent = False
    f.adapter._write(state, etag)
    restarted = DurableFlashDelivery(
        f.queue, f.container, options=FlashStorageOptions(lease_seconds=10),
        clock=f.clock, verifier=verifier,
        notifier=lambda *args: f.notified.append(args),
    )
    restarted.recover_notifications()
    assert f.notified[-1] == ("job-1", "job-1-attempt-1", {"count": 5})
    assert f.adapter._read("job-1")[0].notification_sent


def test_lost_completion_response_is_idempotent_and_accepted_result_immutable():
    f = Fixture()
    f.add_job()
    _delivery, token = f.adapter.claim()
    result = {"count": 5}
    assert f.adapter.complete(token, result) == result
    result["count"] = 50
    f.clock.advance(1000)
    assert f.adapter.complete(token, {"count": 5}) == {"count": 5}
    assert f.adapter.completed_result("job-1") == {"count": 5}
    with pytest.raises(LeaseError):
        f.adapter.complete(token, {"count": 4})


def test_completion_retry_after_process_restart_uses_only_hash_in_durable_state():
    f = Fixture()
    f.add_job()
    _delivery, token = f.adapter.claim()
    assert f.adapter.complete(token, {"count": 5}) == {"count": 5}
    state, _ = f.adapter._read("job-1")
    assert token not in json.dumps(state.model_dump())
    restarted = DurableFlashDelivery(
        f.queue, f.container, options=FlashStorageOptions(lease_seconds=10),
        clock=f.clock, verifier=verifier,
        notifier=lambda *args: f.notified.append(args),
    )
    assert restarted.complete(token, {"count": 5}) == {"count": 5}
    with pytest.raises(LeaseError):
        restarted.complete(token, {"count": 4})


def test_delete_response_lost_after_queue_deleted_keeps_completion_retryable():
    f = Fixture()
    f.add_job()
    _delivery, token = f.adapter.claim()
    f.queue.delete_error = TimeoutError("delete committed; response lost")
    f.queue.delete_after_error = True
    with pytest.raises(TimeoutError):
        f.adapter.complete(token, {"count": 5})
    assert not f.queue.messages
    restarted = DurableFlashDelivery(
        f.queue, f.container, options=FlashStorageOptions(lease_seconds=10),
        clock=f.clock, verifier=verifier,
        notifier=lambda *args: f.notified.append(args),
    )
    assert restarted.complete(token, {"count": 5}) == {"count": 5}
    assert f.adapter.completed_result("job-1") == {"count": 5}


def test_stale_queue_receipt_errors_and_job_identity_conflicts_fail_closed():
    f = Fixture()
    f.add_job()
    _delivery, token = f.adapter.claim()
    context = f.adapter._claims[token]
    context.pop_receipt = "stale"
    with pytest.raises(StorageError):
        f.adapter.renew(token)
    with pytest.raises(ValueError, match="different contract"):
        f.adapter.enqueue("job-1", contract(wall=61))
    with pytest.raises(ValueError, match="path-safe"):
        f.adapter.enqueue("../job", contract())
