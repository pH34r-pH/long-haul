"""Azure Queue/Blob backed Flash delivery with offline injectable clients.

Queue messages contain only a job reference. A versioned per-job blob is the
conditional state authority; queue receipts are retained only in this process.
The adapter intentionally does not claim a transaction across Queue and Blob.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from .contracts import WorkContract
from .flash_delivery import Delivery, LeaseError, VerificationError

STATE_VERSION = 1
_SETTLED_REFERENCE = object()
_LOGGER = logging.getLogger(__name__)


def _new_claim_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class FlashStorageOptions:
    lease_seconds: int
    token_factory: Callable[[], str] = _new_claim_token
    recovery_scan_limit: int = 256
    accepted_trigger_drain_limit: int = 8
    max_local_claims: int = 128

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if min(self.recovery_scan_limit, self.accepted_trigger_drain_limit,
               self.max_local_claims) < 1:
            raise ValueError("storage scan and claim limits must be positive")


class _Blob(Protocol):
    def upload_blob(self, data: bytes, **kwargs: Any) -> Any: ...
    def download_blob(self) -> Any: ...


class _BlobDownload(Protocol):
    properties: Any

    def readall(self) -> bytes: ...


class _Container(Protocol):
    def get_blob_client(self, name: str) -> _Blob: ...
    def list_blobs(self, **kwargs: Any) -> Any: ...


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


class _ClaimIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = STATE_VERSION
    job_id: str
    generation: int
    attempt_id: str
    claim_hash: str
    expires_at: float


class _NotificationCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = STATE_VERSION
    continuation_token: str | None = None


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

    @staticmethod
    def _claim_index_name(claim_hash: str) -> str:
        return f"flash-claim-index/v1/{claim_hash}.json"

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
        self._prune_claims()
        if len(self._claims) >= self.options.max_local_claims:
            return None
        self.recover_notifications()
        for _ in range(self.options.accepted_trigger_drain_limit):
            message = self.queue.receive_message(visibility_timeout=self.visibility_seconds)
            if message is None:
                return None
            result = self._claim_message(message)
            if result is _SETTLED_REFERENCE:
                continue
            return result
        return None

    def _claim_message(self, message: Any) -> tuple[Delivery, str] | None | object:
        payload = self._message(message)
        job_id = payload["job_id"]
        state, etag = self._read(job_id)
        if state.accepted_result_json is not None:
            self._delete(message)
            return _SETTLED_REFERENCE
        if state.terminal_reason is not None:
            self._delete(message)
            return None
        now = self.clock()
        work_contract = WorkContract.model_validate(state.contract)
        deadline = state.started_at + work_contract.budget.wall_seconds
        if (state.lease_expires_at is not None
                and now < min(state.lease_expires_at, deadline)):
            return None
        if state.attempt_number >= work_contract.budget.max_attempts or now >= deadline:
            state.terminal_reason = "attempt_budget_exhausted" if state.attempt_number >= work_contract.budget.max_attempts else "wall_budget_expired"
            try:
                self._write(state, etag)
            except LeaseError:
                return None
            # The durable terminal record precedes trigger settlement.
            self._delete(message)
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
        # Queue I/O may block past the old lease/deadline. Re-read the
        # conditional state and check the clock after that I/O before renewal.
        renewed_at = self.clock()
        current, etag = self._read(context.job_id)
        self._require_claim(context, current)
        expiry = min(renewed_at + self.lease_seconds, deadline)
        if expiry <= renewed_at:
            raise LeaseError("lease expired")
        current.lease_expires_at = expiry
        self._write(current, etag)
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
        claim_hash = self._token_hash(token)
        self._write_claim_index(_ClaimIndex(
            job_id=context.job_id,
            generation=context.generation,
            attempt_id=context.attempt_id,
            claim_hash=claim_hash,
            expires_at=state.started_at + contract.budget.wall_seconds,
        ))
        # Index creation is a separate Blob operation and may block long enough
        # for the claim lease or work deadline to expire. Re-read both state and
        # clock after that I/O; only this fresh snapshot may be used for the
        # acceptance CAS. A crash after the index write leaves a harmless index
        # entry because retry lookup validates it against accepted job state.
        state, etag = self._read(context.job_id)
        self._require_claim(context, state)
        state.accepted_generation = context.generation
        state.accepted_attempt_id = context.attempt_id
        state.accepted_claim_hash = claim_hash
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
        """Process one durable continuation page; repeated calls cycle fairly."""
        cursor, cursor_etag = self._read_notification_cursor()
        listings = self.container.list_blobs(
            name_starts_with="flash-jobs/v1/",
            results_per_page=self.options.recovery_scan_limit,
            maxresults=self.options.recovery_scan_limit,
        )
        pages = listings.by_page(continuation_token=cursor.continuation_token)
        try:
            page = next(pages)
        except StopIteration:
            cursor.continuation_token = None
            self._write_notification_cursor(cursor, cursor_etag)
            return
        for item in page:
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
            try:
                self.notifier(job_id, attempt, json.loads(result))
            except Exception:  # noqa: BLE001 - notifier implementations are injected
                # Leave this record pending and keep the durable cursor moving;
                # an unhealthy destination must not pin every later job behind
                # one repeatedly failing notification.
                _LOGGER.warning("notification delivery failed for job %s; leaving it pending", job_id)
            else:
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
        cursor.continuation_token = pages.continuation_token
        self._write_notification_cursor(cursor, cursor_etag)

    def _read_notification_cursor(self) -> tuple[_NotificationCursor, str]:
        name = "flash-meta/v1/notification-cursor.json"
        blob = self.container.get_blob_client(name)
        try:
            download: _BlobDownload = blob.download_blob()
        except Exception as exc:
            if self._status(exc) != 404:
                raise
            try:
                blob.upload_blob(self._encode(_NotificationCursor()), overwrite=False)
            except Exception as create_error:
                if self._status(create_error) not in (409, 412):
                    raise
            download = blob.download_blob()
        raw = download.readall()
        cursor = _NotificationCursor.model_validate_json(raw)
        if cursor.schema_version != STATE_VERSION:
            raise ValueError("unsupported notification cursor version")
        return cursor, download.properties.etag

    def _write_notification_cursor(self, cursor: _NotificationCursor, etag: str) -> None:
        blob = self.container.get_blob_client("flash-meta/v1/notification-cursor.json")
        try:
            self._conditional_upload(blob, self._encode(cursor), etag)
        except LeaseError:
            # Another recovery worker advanced the durable cursor.
            return

    def _write_claim_index(self, index: _ClaimIndex) -> None:
        blob = self.container.get_blob_client(self._claim_index_name(index.claim_hash))
        try:
            blob.upload_blob(self._encode(index), overwrite=False)
        except Exception as exc:
            if self._status(exc) not in (409, 412):
                raise
            download: _BlobDownload = blob.download_blob()
            existing = _ClaimIndex.model_validate_json(download.readall())
            if existing != index:
                raise LeaseError("claim token index conflicts with accepted generation") from exc

    def _settle(self, context: _ClaimContext) -> None:
        try:
            self.queue.delete_message(context.queue_message, context.pop_receipt)
        except Exception as exc:
            # If the response was lost, a repeated delete may report stale
            # receipt; accepted state remains authoritative and is recoverable.
            if self._status(exc) not in (404, 400, 412):
                raise
        self._claims.pop(context.token, None)

    def _current(self, token: str) -> tuple[_ClaimContext, _JobState, str]:
        context = self._claims.get(token)
        if context is None:
            raise LeaseError("stale or unknown claim")
        state, etag = self._read(context.job_id)
        self._require_claim(context, state)
        return context, state, etag

    def _require_claim(self, context: _ClaimContext, state: _JobState) -> None:
        if (state.generation != context.generation or state.lease_id != self._token_hash(context.token)
                or state.accepted_result_json is not None or state.terminal_reason is not None):
            raise LeaseError("stale claim generation")
        if self.clock() >= min(state.lease_expires_at or 0,
                               state.started_at + WorkContract.model_validate(state.contract).budget.wall_seconds):
            raise LeaseError("lease expired")

    def _read(self, job_id: str) -> tuple[_JobState, str]:
        blob = self.container.get_blob_client(self._blob_name(job_id))
        download: _BlobDownload = blob.download_blob()
        raw = download.readall()
        # StorageStreamDownloader.properties is returned with this download
        # response; a separate get_blob_properties call could pair old bytes
        # with a newer ETag and make a stale conditional write appear current.
        etag = download.properties.etag
        state = _JobState.model_validate_json(raw)
        state.validate_version()
        if state.job_id != job_id:
            raise ValueError("job state identifier mismatch")
        return state, etag

    def _write(self, state: _JobState, etag: str) -> None:
        blob = self.container.get_blob_client(self._blob_name(state.job_id))
        self._conditional_upload(blob, self._encode(state), etag)

    def _conditional_upload(self, blob: _Blob, payload: bytes, etag: str) -> None:
        try:
            from azure.core import MatchConditions  # type: ignore[import-not-found]
            condition: Any = MatchConditions.IfNotModified
        except ImportError:
            condition = "IfNotModified"
        try:
            blob.upload_blob(payload, overwrite=True, etag=etag,
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
        blob = self.container.get_blob_client(self._claim_index_name(token_hash))
        try:
            download: _BlobDownload = blob.download_blob()
        except Exception as exc:
            if self._status(exc) == 404:
                return None
            raise
        index = _ClaimIndex.model_validate_json(download.readall())
        if index.claim_hash != token_hash or index.schema_version != STATE_VERSION:
            return None
        state, _ = self._read(index.job_id)
        if (state.accepted_claim_hash != token_hash
                or state.accepted_generation != index.generation
                or state.accepted_attempt_id != index.attempt_id):
            return None
        return state

    def _prune_claims(self) -> None:
        now = self.clock()
        expired = [token for token, context in self._claims.items()
                   if now >= context.delivery.lease_expires_at]
        for token in expired:
            del self._claims[token]

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
