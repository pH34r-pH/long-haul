"""Azure Queue/Blob backed Flash delivery with offline injectable clients.

Queue messages contain only a job reference. A versioned per-job blob is the
conditional state authority; queue receipts are retained only in this process.
The adapter intentionally does not claim a transaction across Queue and Blob.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from .contracts import WorkContract
from .flash_delivery import Delivery, LeaseError, VerificationError

STATE_VERSION = 1


def _new_claim_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class FlashStorageOptions:
    lease_seconds: int
    token_factory: Callable[[], str] = _new_claim_token

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")


class _Blob(Protocol):
    def upload_blob(self, data: bytes, **kwargs: Any) -> Any: ...
    def download_blob(self) -> Any: ...
    def get_blob_properties(self) -> Any: ...


class _Container(Protocol):
    def get_blob_client(self, name: str) -> _Blob: ...
    def list_blobs(self) -> Iterable[Any]: ...


class _Queue(Protocol):
    def send_message(self, content: str, **kwargs: Any) -> Any: ...
    def receive_message(self, **kwargs: Any) -> Any: ...
    def update_message(self, message: Any, pop_receipt: str, **kwargs: Any) -> Any: ...
    def delete_message(self, message: Any, pop_receipt: str) -> Any: ...


class _JobState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = STATE_VERSION
    job_id: str
    contract: dict[str, Any]
    started_at: float
    attempt_number: int = 0
    generation: int = 0
    lease_id: str | None = None
    lease_expires_at: float | None = None
    accepted_generation: int | None = None
    accepted_attempt_id: str | None = None
    accepted_claim_hash: str | None = None
    accepted_result_json: str | None = None
    terminal_reason: str | None = None
    notification_pending: bool = False
    notification_sent: bool = False

    def validate_version(self) -> _JobState:
        if self.schema_version != STATE_VERSION:
            raise ValueError("unsupported durable state version")
        return self


@dataclass
class _ClaimContext:
    token: str
    job_id: str
    attempt_id: str
    generation: int
    queue_message: Any
    pop_receipt: str
    delivery: Delivery


class DurableFlashDelivery:
    """Durable queue adapter; clients are Azure SDK clients or offline fakes.

    ``notifier(job_id, accepted_attempt_id, result)`` must be idempotent on
    ``accepted_attempt_id``. Notifications are retried after restart, so
    duplicate delivery is possible by design.
    """

    def __init__(self, queue: _Queue, container: _Container, *,
                 options: FlashStorageOptions, clock: Callable[[], float],
                 verifier: Callable[[WorkContract, Any], bool],
                 notifier: Callable[[str, str, Any], None]) -> None:
        self.queue = queue
        self.container = container
        self.options = options
        self.lease_seconds = options.lease_seconds
        self.visibility_seconds = options.lease_seconds
        self.clock = clock
        self.verifier = verifier
        self.notifier = notifier
        self.token_factory = options.token_factory
        self._claims: dict[str, _ClaimContext] = {}

    @staticmethod
    def _blob_name(job_id: str) -> str:
        # Restricting the ID to a single path segment prevents blob-name tricks.
        if not job_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in job_id):
            raise ValueError("job_id must be an opaque path-safe identifier")
        return f"flash-jobs/v1/{job_id}.json"

    def enqueue(self, job_id: str, contract: WorkContract) -> None:
        state = _JobState(job_id=job_id, contract=contract.model_dump(mode="json"), started_at=self.clock())
        blob = self.container.get_blob_client(self._blob_name(job_id))
        try:
            blob.upload_blob(self._encode(state), overwrite=False)
        except Exception as exc:  # already-created is an idempotent producer retry
            if self._status(exc) not in (409, 412):
                raise
            existing, _ = self._read(job_id)
            if existing.contract != state.contract:
                raise ValueError("job_id already exists with a different contract") from exc
            if existing.accepted_result_json is not None:
                return
        self.queue.send_message(json.dumps({"v": 1, "job_id": job_id}, separators=(",", ":")))

    def claim(self) -> tuple[Delivery, str] | None:
        self.recover_notifications()
        message = self.queue.receive_message(visibility_timeout=self.visibility_seconds)
        if message is None:
            return None
        payload = self._message(message)
        job_id = payload["job_id"]
        state, etag = self._read(job_id)
        if state.accepted_result_json is not None:
            self._delete(message)
            self.recover_notifications()
            return self.claim()
        if state.terminal_reason is not None:
            self._delete(message)
            return None
        now = self.clock()
        work_contract = WorkContract.model_validate(state.contract)
        deadline = state.started_at + work_contract.budget.wall_seconds
        if state.attempt_number >= work_contract.budget.max_attempts or now >= deadline:
            state.terminal_reason = "attempt_budget_exhausted" if state.attempt_number >= work_contract.budget.max_attempts else "wall_budget_expired"
            try:
                self._write(state, etag)
            except LeaseError:
                return None
            # The durable terminal record precedes trigger settlement.
            self._delete(message)
            return None
        if state.lease_expires_at is not None and now < state.lease_expires_at:
            return None
        generation = state.generation + 1
        attempt = state.attempt_number + 1
        token = self._unique_token()
        expires = min(now + self.lease_seconds, deadline)
        state.generation = generation
        state.attempt_number = attempt
        state.lease_id = self._token_hash(token)
        state.lease_expires_at = expires
        try:
            self._write(state, etag)
        except LeaseError:
            return None
        attempt_id = f"{job_id}-attempt-{attempt}"
        delivery = Delivery(job_id, work_contract.contract_id, attempt_id, token, expires)
        receipt = getattr(message, "pop_receipt", None)
        if not receipt:
            raise RuntimeError("queue message omitted pop receipt")
        self._claims[token] = _ClaimContext(token, job_id, attempt_id, generation,
                                             message, receipt, delivery)
        return delivery, token
    def renew(self, token: str) -> Delivery:
        context, state, etag = self._current(token)
        deadline = state.started_at + WorkContract.model_validate(state.contract).budget.wall_seconds
        expiry = min(self.clock() + self.lease_seconds, deadline)
        if self.clock() >= context.delivery.lease_expires_at or expiry <= self.clock():
            raise LeaseError("lease expired")
        updated = self.queue.update_message(context.queue_message, context.pop_receipt,
                                            visibility_timeout=max(1, int(expiry - self.clock())))
        new_receipt = getattr(updated, "pop_receipt", None)
        if not new_receipt:
            raise RuntimeError("queue update omitted rotated pop receipt")
        # Record receipt rotation immediately after Queue returns it. If the
        # following Blob CAS fails, this process still owns the usable receipt.
        context.pop_receipt = new_receipt
        state.lease_expires_at = expiry
        self._write(state, etag)
        context.delivery = Delivery(context.delivery.job_id, context.delivery.contract_id,
                                    context.delivery.attempt_id, token, expiry)
        return context.delivery

    def abandon(self, token: str) -> None:
        context, state, etag = self._current(token)
        state.lease_id = None
        state.lease_expires_at = None
        self._write(state, etag)
        # Queue update rotates the private receipt and makes the same compact
        # reference visible. A crash before this leaves natural lease expiry.
        updated = self.queue.update_message(context.queue_message, context.pop_receipt,
                                            visibility_timeout=0)
        context.pop_receipt = getattr(updated, "pop_receipt", context.pop_receipt)
        self._claims.pop(token, None)

    def complete(self, token: str, result: object) -> object:
        context = self._claims.get(token)
        result_json = self._canonical_result(result)
        if context is None:
            accepted = self._find_accepted_claim(token)
            if accepted is None or accepted.accepted_result_json != result_json:
                raise LeaseError("stale or unknown claim")
            self.recover_notifications()
            return json.loads(accepted.accepted_result_json)
        state, etag = self._read(context.job_id)
        if state.accepted_result_json is not None:
            if (state.accepted_generation == context.generation
                    and state.accepted_claim_hash == self._token_hash(token)
                    and state.accepted_result_json == result_json):
                self._settle(context)
                self.recover_notifications()
                return json.loads(state.accepted_result_json)
            raise LeaseError("job already accepted under another generation or result")
        self._require_claim(context, state)
        contract = WorkContract.model_validate(state.contract)
        normalized = json.loads(result_json)
        if not self.verifier(contract, normalized):
            raise VerificationError("trusted verifier rejected result")
        # Re-read after external verification: expiry, newer generations, and
        # ETag changes fence slow verifiers before the acceptance commit point.
        state, etag = self._read(context.job_id)
        self._require_claim(context, state)
        state.accepted_generation = context.generation
        state.accepted_attempt_id = context.attempt_id
        state.accepted_claim_hash = self._token_hash(token)
        state.accepted_result_json = result_json
        state.notification_pending = True
        state.notification_sent = False
        self._write(state, etag)  # durable acceptance precedes queue settlement
        self._settle(context)
        self.recover_notifications()
        return json.loads(result_json)

    def completed_result(self, job_id: str) -> Any | None:
        state, _ = self._read(job_id)
        return None if state.accepted_result_json is None else json.loads(state.accepted_result_json)

    def recover_notifications(self) -> None:
        """Replay accepted-but-unnotified work; notifier deduplicates by attempt ID."""
        for item in self.container.list_blobs(name_starts_with="flash-jobs/v1/"):
            name = item.name if hasattr(item, "name") else item["name"]
            if not name.endswith(".json"):
                continue
            job_id = name.rsplit("/", 1)[-1][:-5]
            state, etag = self._read(job_id)
            if not state.notification_pending or state.notification_sent:
                continue
            attempt = state.accepted_attempt_id
            result = state.accepted_result_json
            if attempt is None or result is None:
                continue
            self.notifier(job_id, attempt, json.loads(result))
            # CAS means concurrent recovery workers may both notify; stable key
            # lets the downstream outbox suppress duplicates.
            state.notification_pending = False
            state.notification_sent = True
            try:
                self._write(state, etag)
            except LeaseError:
                continue
            except Exception as exc:
                if self._status(exc) not in (409, 412):
                    raise

    def _settle(self, context: _ClaimContext) -> None:
        try:
            self.queue.delete_message(context.queue_message, context.pop_receipt)
        except Exception as exc:
            # If the response was lost, a repeated delete may report stale
            # receipt; accepted state remains authoritative and is recoverable.
            if self._status(exc) not in (404, 400, 412):
                raise
        # Keep the current token as a short-lived in-process idempotency key;
        # repeat completion after a lost response returns the accepted bytes.

    def _current(self, token: str) -> tuple[_ClaimContext, _JobState, str]:
        context = self._claims.get(token)
        if context is None:
            raise LeaseError("stale or unknown claim")
        state, etag = self._read(context.job_id)
        self._require_claim(context, state)
        return context, state, etag

    def _require_claim(self, context: _ClaimContext, state: _JobState) -> None:
        if (state.generation != context.generation or state.lease_id != self._token_hash(context.token)
                or state.accepted_result_json is not None):
            raise LeaseError("stale claim generation")
        if self.clock() >= min(state.lease_expires_at or 0,
                               state.started_at + WorkContract.model_validate(state.contract).budget.wall_seconds):
            raise LeaseError("lease expired")

    def _read(self, job_id: str) -> tuple[_JobState, str]:
        blob = self.container.get_blob_client(self._blob_name(job_id))
        raw = blob.download_blob().readall()
        etag = blob.get_blob_properties().etag
        state = _JobState.model_validate_json(raw)
        state.validate_version()
        if state.job_id != job_id:
            raise ValueError("job state identifier mismatch")
        return state, etag

    def _write(self, state: _JobState, etag: str) -> None:
        blob = self.container.get_blob_client(self._blob_name(state.job_id))
        try:
            from azure.core import MatchConditions  # type: ignore[import-not-found]
            condition: Any = MatchConditions.IfNotModified
        except ImportError:
            condition = "IfNotModified"
        try:
            blob.upload_blob(self._encode(state), overwrite=True, etag=etag,
                             match_condition=condition)
        except Exception as exc:
            if self._status(exc) == 412:
                raise LeaseError("durable state changed during conditional update") from exc
            raise

    @staticmethod
    def _encode(state: _JobState) -> bytes:
        return state.model_dump_json(exclude_none=False).encode("utf-8")

    def _message(self, message: Any) -> dict[str, Any]:
        content = getattr(message, "content", None)
        if not isinstance(content, str):
            raise TypeError("queue message content is invalid")
        value = json.loads(content)
        if type(value) is not dict or set(value) != {"v", "job_id"} or value["v"] != 1:
            raise ValueError("queue message reference is invalid")
        self._blob_name(value["job_id"])
        return value

    def _delete(self, message: Any) -> None:
        receipt = getattr(message, "pop_receipt", None)
        if receipt:
            self.queue.delete_message(message, receipt)

    def _unique_token(self) -> str:
        token = self.token_factory()
        if not isinstance(token, str) or len(token) < 32 or token in self._claims:
            raise RuntimeError("claim token factory must return unique opaque tokens")
        return token

    def _find_accepted_claim(self, token: str) -> _JobState | None:
        token_hash = self._token_hash(token)
        for item in self.container.list_blobs(name_starts_with="flash-jobs/v1/"):
            name = item.name if hasattr(item, "name") else item["name"]
            if name.endswith(".json"):
                job_id = name.rsplit("/", 1)[-1][:-5]
                state, _ = self._read(job_id)
                if state.accepted_claim_hash == token_hash:
                    return state
        return None

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_result(result: object) -> str:
        try:
            encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
            json.loads(encoded)
            return encoded
        except (TypeError, ValueError, OverflowError) as exc:
            raise VerificationError("result must be finite JSON data") from exc

    @staticmethod
    def _status(exc: Exception) -> int | None:
        return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
