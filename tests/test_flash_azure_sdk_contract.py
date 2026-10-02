"""Contract-test the pinned Azure Storage SDK clients with an injected transport."""
from __future__ import annotations

import base64
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree

from azure.core.credentials import AzureNamedKeyCredential
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.core.utils import CaseInsensitiveDict
from azure.storage.blob import ContainerClient
from azure.storage.queue import QueueClient

from long_haul.work import (
    AcceptancePredicate,
    DurableFlashDelivery,
    FlashStorageOptions,
    PredicateKind,
    WorkBudget,
    WorkContract,
)


class _DownloadBody:
    def __init__(self, body: bytes, response: _StorageResponse) -> None:
        self.body_bytes = body
        self.content_length = len(body)
        self.properties = None
        self.response = response

    def __iter__(self):
        yield self.body_bytes


class _StorageResponse(HttpResponse):
    def __init__(self, request, status: int, headers: dict[str, str], body: bytes) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers)
        self.content_type = headers.get("Content-Type")
        self.reason = "OK"
        self.body_bytes = body
        self.content_length = len(body)
        self.location_mode = None

    def body(self) -> bytes:
        return self.body_bytes

    def read(self) -> bytes:
        return self.body_bytes

    def stream_download(self, pipeline, **kwargs):
        return _DownloadBody(self.body_bytes, self)


class _QueuedMessage:
    def __init__(self, message_id: str, content: str) -> None:
        self.message_id = message_id
        self.content = content
        self.receipt_number = 0
        self.visible_at = 0.0


class _OfflineAzureTransport(HttpTransport):
    """Small Azure REST double that records SDK calls; never opens a socket."""

    def __init__(self) -> None:
        self.blobs: dict[str, tuple[bytes, int]] = {}
        self.messages: list[_QueuedMessage] = []
        self.requests = []
        self._message_id = 0
        self._clock = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def send(self, request, **kwargs):
        self.requests.append(request)
        parsed = urlsplit(request.url)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if ".blob." in parsed.netloc:
            return self._blob(request, path, query)
        if ".queue." in parsed.netloc:
            return self._queue(request, path, query)
        raise AssertionError(f"unexpected service URL: {request.url}")

    def _blob(self, request, path: str, query: dict[str, list[str]]):
        if query.get("comp") == ["list"]:
            prefix = query.get("prefix", [""])[0]
            names = sorted(name for name in self.blobs if name.startswith(prefix))
            blobs = "".join(self._listed_blob(name) for name in names)
            body = ("<EnumerationResults><Blobs>" + blobs
                    + "</Blobs><NextMarker></NextMarker></EnumerationResults>").encode()
            return self._response(request, 200, {"Content-Type": "application/xml"}, body)

        name = path.split("/", 2)[-1]
        current = self.blobs.get(name)
        if request.method == "GET":
            if current is None:
                return self._error(request, 404, "BlobNotFound")
            body, version = current
            etag = self._etag(version)
            start, end = self._range(request.headers.get("x-ms-range"), len(body))
            payload = body[start:end + 1]
            headers = {
                "Content-Length": str(len(payload)),
                "Content-Range": f"bytes {start}-{end}/{len(body)}",
                "ETag": etag,
                "Last-Modified": "Fri, 02 Oct 2026 20:00:00 GMT",
                "Accept-Ranges": "bytes",
                "x-ms-blob-type": "BlockBlob",
            }
            return self._response(request, 206, headers, payload)
        if request.method == "PUT":
            if_match = request.headers.get("If-Match")
            if if_match and (current is None or if_match != self._etag(current[1])):
                return self._error(request, 412, "ConditionNotMet")
            if request.headers.get("If-None-Match") == "*" and current is not None:
                return self._error(request, 412, "ConditionNotMet")
            version = 1 if current is None else current[1] + 1
            self.blobs[name] = (self._request_body(request), version)
            return self._response(request, 201, {"ETag": self._etag(version)}, b"")
        raise AssertionError(f"unexpected Blob operation {request.method} {path}")

    def _queue(self, request, path: str, query: dict[str, list[str]]):
        if path.endswith("/messages") and request.method != "GET":
            xml = ElementTree.fromstring(self._request_body(request))
            content = xml.findtext("MessageText", default="")
            self._message_id += 1
            message = _QueuedMessage(str(self._message_id), content)
            self.messages.append(message)
            return self._response(request, 201, {
                "Content-Type": "application/xml",
                "x-ms-message-id": message.message_id,
                "x-ms-popreceipt": self._receipt(message),
                "x-ms-insertion-time": "Fri, 02 Oct 2026 20:00:00 GMT",
                "x-ms-expiration-time": "Sat, 03 Oct 2026 20:00:00 GMT",
                "x-ms-time-next-visible": "Fri, 02 Oct 2026 20:00:00 GMT",
            }, (
                "<QueueMessagesList><QueueMessage><MessageId>" + message.message_id + "</MessageId>"
                "<InsertionTime>Fri, 02 Oct 2026 20:00:00 GMT</InsertionTime>"
                "<ExpirationTime>Sat, 03 Oct 2026 20:00:00 GMT</ExpirationTime>"
                "<PopReceipt>" + self._receipt(message) + "</PopReceipt>"
                "<TimeNextVisible>Fri, 02 Oct 2026 20:00:00 GMT</TimeNextVisible>"
                "</QueueMessage></QueueMessagesList>"
            ).encode())
        if path.endswith("/messages") and request.method == "GET":
            message = next((item for item in self.messages if item.visible_at <= self._clock), None)
            if message is None:
                return self._response(request, 200, {"Content-Type": "application/xml"}, b"<QueueMessagesList />")
            message.receipt_number += 1
            timeout = int(query.get("visibilitytimeout", ["30"])[0])
            message.visible_at = self._clock + timeout
            body = (
                "<QueueMessagesList><QueueMessage>"
                f"<MessageId>{message.message_id}</MessageId>"
                "<InsertionTime>Fri, 02 Oct 2026 20:00:00 GMT</InsertionTime>"
                "<ExpirationTime>Sat, 03 Oct 2026 20:00:00 GMT</ExpirationTime>"
                f"<PopReceipt>{self._receipt(message)}</PopReceipt>"
                "<TimeNextVisible>Fri, 02 Oct 2026 20:01:00 GMT</TimeNextVisible>"
                "<DequeueCount>1</DequeueCount>"
                f"<MessageText>{message.content}</MessageText>"
                "</QueueMessage></QueueMessagesList>"
            ).encode()
            return self._response(request, 200, {"Content-Type": "application/xml"}, body)

        parts = path.rstrip("/").split("/")
        message_id = parts[-1]
        receipt = query.get("popreceipt", [""])[0]
        message = next((item for item in self.messages if item.message_id == message_id), None)
        if message is None or receipt != self._receipt(message):
            return self._error(request, 404, "PopReceiptMismatch")
        if request.method == "PUT":
            message.receipt_number += 1
            timeout = int(query.get("visibilitytimeout", ["0"])[0])
            message.visible_at = self._clock + timeout
            return self._response(request, 204, {
                "x-ms-popreceipt": self._receipt(message),
                "x-ms-time-next-visible": "Fri, 02 Oct 2026 20:01:00 GMT",
            }, b"")
        if request.method == "DELETE":
            self.messages.remove(message)
            return self._response(request, 204, {}, b"")
        raise AssertionError(f"unexpected Queue operation {request.method} {path}")

    def _listed_blob(self, name: str) -> str:
        _, version = self.blobs[name]
        return (
            "<Blob><Name>" + name + "</Name><Properties>"
            "<Last-Modified>Fri, 02 Oct 2026 20:00:00 GMT</Last-Modified>"
            "<Etag>" + self._etag(version) + "</Etag>"
            "<Content-Length>0</Content-Length><BlobType>BlockBlob</BlobType>"
            "</Properties></Blob>"
        )

    @staticmethod
    def _range(value: str | None, length: int) -> tuple[int, int]:
        if value and value.startswith("bytes="):
            start, end = value.removeprefix("bytes=").split("-", 1)
            return int(start), min(int(end), length - 1)
        return 0, length - 1

    @staticmethod
    def _request_body(request) -> bytes:
        body = request.body
        if body is None:
            return b""
        if isinstance(body, bytes):
            return body
        if isinstance(body, str):
            return body.encode()
        return body.read()

    @staticmethod
    def _etag(version: int) -> str:
        return f'"offline-{version}"'

    @staticmethod
    def _receipt(message: _QueuedMessage) -> str:
        return f"receipt-{message.message_id}-{message.receipt_number}"

    @staticmethod
    def _response(request, status: int, headers: dict[str, str], body: bytes):
        complete_headers = {"x-ms-request-id": "offline-request", "x-ms-version": "2025-05-05"}
        complete_headers.update(headers)
        return _StorageResponse(request, status, complete_headers, body)

    def _error(self, request, status: int, code: str):
        body = ("<Error><Code>" + code + "</Code><Message>offline</Message></Error>").encode()
        return self._response(request, status, {
            "Content-Type": "application/xml", "x-ms-error-code": code,
        }, body)


def _contract() -> WorkContract:
    return WorkContract(
        contract_id="sdk-contract-v1",
        mission_id="sdk-mission",
        objective="Exercise the pinned storage SDK clients offline",
        allowed_paths=["src"],
        budget=WorkBudget(wall_seconds=120, max_attempts=2),
        success_predicates=[AcceptancePredicate(
            predicate_id="result", kind=PredicateKind.ARTIFACT,
            description="The injected SDK result is accepted", artifact_path="out/result.json",
        )],
    )


def test_pinned_azure_sdk_clients_execute_flash_storage_contract_over_injected_transport():
    transport = _OfflineAzureTransport()
    key = base64.b64encode(b"synthetic-offline-test-key").decode()
    credential = AzureNamedKeyCredential("offlineaccount", key)
    container = ContainerClient(
        account_url="https://offlineaccount.blob.core.windows.net",
        container_name="flash",
        credential=credential,
        transport=transport,
    )
    queue = QueueClient(
        account_url="https://offlineaccount.queue.core.windows.net",
        queue_name="flash",
        credential=credential,
        transport=transport,
    )
    clock = lambda: 100.0
    notifications = []
    adapter = DurableFlashDelivery(
        queue, container,
        options=FlashStorageOptions(lease_seconds=20, token_factory=lambda: "offline-sdk-claim-token-" + "x" * 32),
        clock=clock,
        verifier=lambda _contract, result: result == {"ok": True},
        notifier=lambda *args: notifications.append(args),
    )

    adapter.enqueue("sdk-job", _contract())
    delivery, token = adapter.claim()
    assert delivery.job_id == "sdk-job"
    renewed = adapter.renew(token)
    assert renewed.lease_expires_at == delivery.lease_expires_at
    assert adapter.complete(token, {"ok": True}) == {"ok": True}
    assert notifications == [("sdk-job", "sdk-job-attempt-1", {"ok": True})]

    queue_updates = [r for r in transport.requests if r.method == "PUT" and "/messages/" in r.url]
    queue_deletes = [r for r in transport.requests if r.method == "DELETE" and "/messages/" in r.url]
    blob_conditionals = [r for r in transport.requests if r.method == "PUT" and "/flash-jobs/" in r.url]
    assert queue_updates and "popreceipt=receipt-1-1" in queue_updates[0].url
    assert queue_deletes and "popreceipt=receipt-1-2" in queue_deletes[0].url
    assert blob_conditionals and blob_conditionals[-1].headers.get("If-Match") is not None
    assert all("offlineaccount" in request.url for request in transport.requests)
    assert not transport.messages
