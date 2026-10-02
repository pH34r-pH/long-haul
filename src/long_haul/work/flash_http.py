"""Offline HTTP boundary for the synthetic Flash delivery fixture.

This WSGI adapter is an executable protocol fixture, not a durable queue or a
production admission service. The controller supplies its synthetic F0 policy
inputs when constructing the adapter; no request can grant itself capability.
"""
from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from wsgiref.types import StartResponse, WSGIEnvironment

from pydantic import BaseModel, ConfigDict, ValidationError

from .flash_delivery import FlashDeliveryFixture, LeaseError, VerificationError

MAX_BODY_BYTES = 16 * 1024


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ClaimRequest(_StrictRequest):
    pass


class _LeaseRequest(_StrictRequest):
    lease_id: str


class _CompleteRequest(_LeaseRequest):
    result: Any


def _response(start_response: StartResponse, status: str, payload: dict[str, Any] | None = None,
              extra_headers: list[tuple[str, str]] | None = None):
    data = b"" if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    headers = [("Content-Length", str(len(data)))]
    if payload is not None:
        headers.append(("Content-Type", "application/json; charset=utf-8"))
    headers.extend(extra_headers or [])
    start_response(status, headers)
    return [data]


class FlashHTTPAdapter:
    """Small WSGI API around an injected in-memory delivery fixture.

    Routes are POST /v1/jobs/claim and POST /v1/jobs/{job_id}/
    {renew,complete,abandon}. Constructor policy values model a selected
    synthetic F0 controller decision and are never read from HTTP payloads.
    """

    def __init__(
        self,
        fixture: FlashDeliveryFixture,
        *,
        capability_state: str,
        observed_memory_mib: int | None,
        initialization_succeeded: bool = True,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.fixture = fixture
        self.max_body_bytes = max_body_bytes
        extension = fixture.contract.flash_execution
        self.admitted = bool(extension and extension.admits(
            capability_state=capability_state,
            observed_memory_mib=observed_memory_mib,
            initialization_succeeded=initialization_succeeded,
        ))

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse):
        method = environ.get("REQUEST_METHOD", "")
        path = environ.get("PATH_INFO", "")
        if method != "POST":
            return _response(start_response, "405 Method Not Allowed",
                             {"error": "method_not_allowed"}, [("Allow", "POST")])

        try:
            content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json")
            body = self._read_body(environ)
            if path == "/v1/jobs/claim":
                return self._claim(body, start_response)
            operation = self._operation_from_path(path)
            if operation is None:
                return _response(start_response, "404 Not Found", {"error": "not_found"})
            return self._operate(operation, body, start_response)
        except _BodyTooLarge:
            return _response(start_response, "413 Payload Too Large", {"error": "body_too_large"})
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, ValidationError) as exc:
            if isinstance(exc, LeaseError):
                return _response(start_response, "409 Conflict", {
                    "error": "lease_conflict", "message": "claim is stale, expired, or inactive",
                })
            if isinstance(exc, VerificationError):
                return _response(start_response, "422 Unprocessable Entity", {
                    "error": "invalid_result", "message": "result was rejected by verification",
                })
            return _response(start_response, "400 Bad Request", {
                "error": "invalid_request", "message": "request is malformed or invalid",
            })
        except Exception:  # noqa: BLE001 - injected verifier/storage failures are private.
            # Verifier and future storage exceptions may contain private
            # diagnostics. Keep details internal and return a fixed response.
            return _response(start_response, "500 Internal Server Error", {
                "error": "internal_error", "message": "request could not be completed",
            })

    def _claim(self, body: bytes, start_response: StartResponse):
        _ClaimRequest.model_validate(self._decode(body))
        if not self.admitted:
            return _response(start_response, "200 OK", {"status": "declined", "reason": "synthetic_f0_policy"})
        delivery = self.fixture.claim()
        if delivery is None:
            return _response(start_response, "200 OK", {"status": "no_work"})
        return _response(start_response, "200 OK", {
            "status": "claimed", "job_id": delivery.job_id,
            "contract_id": delivery.contract_id, "attempt_id": delivery.attempt_id,
            "lease_id": delivery.lease_id, "lease_expires_at": delivery.lease_expires_at,
        })

    def _operate(self, operation: str, body: bytes, start_response: StartResponse):
        data = self._decode(body)
        if operation == "complete":
            request = _CompleteRequest.model_validate(data)
            result = self.fixture.complete(request.lease_id, request.result)
            return _response(start_response, "200 OK", {"status": "completed", "result": result})
        request = _LeaseRequest.model_validate(data)
        if operation == "renew":
            delivery = self.fixture.renew(request.lease_id)
            return _response(start_response, "200 OK", {
                "status": "renewed", "lease_id": delivery.lease_id,
                "lease_expires_at": delivery.lease_expires_at,
            })
        if operation == "abandon":
            self.fixture.abandon(request.lease_id)
            return _response(start_response, "200 OK", {"status": "abandoned"})
        return _response(start_response, "404 Not Found", {"error": "not_found"})

    def _operation_from_path(self, path: str) -> str | None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["v1", "jobs"] or parts[2] != self.fixture.job_id:
            return None
        return parts[3]

    def _read_body(self, environ: WSGIEnvironment) -> bytes:
        raw_length = environ.get("CONTENT_LENGTH")
        if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
            raise ValueError("invalid Content-Length")
        length = int(raw_length)
        if length < 0:
            raise ValueError("invalid Content-Length")
        if length > self.max_body_bytes:
            raise _BodyTooLarge
        stream = environ.get("wsgi.input", BytesIO())
        body = stream.read(length)
        if len(body) != length:
            raise ValueError("request body length does not match Content-Length")
        return body

    @staticmethod
    def _decode(body: bytes) -> dict[str, Any]:
        if not body:
            raise ValueError("request body must be a JSON object")
        value = json.loads(body)
        if type(value) is not dict:
            raise ValueError("request body must be a JSON object")
        return value


class _BodyTooLarge(Exception):
    pass
