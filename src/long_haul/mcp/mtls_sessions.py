"""Local mTLS session mapping prototype; this is not a capability grant.

The owner of this object is trusted control-plane code. It must not expose
registration, its SSLContext, or its session table to model-controlled code.
This process-local mapping is not durable and does not implement issuance,
auditing, attestation, or the SPIFFE Workload API.
"""
from __future__ import annotations

import hashlib
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock


@dataclass(frozen=True)
class SessionIdentity:
    crew_id: str
    session_id: str
    embodiment_id: str
    expires_at: datetime


@dataclass(frozen=True)
class _Binding:
    identity: SessionIdentity
    fingerprint: str
    starts_at: datetime


class MutualTLSSessions:
    """Map a verified client certificate to an operator-admitted short session.

    The context and bindings belong to the trusted server process. A returned
    identity is a snapshot, not a transferable credential: the transport must
    recheck the live connection and mapping for every request. The capability
    gateway remains deny-only until its separate policy and audit gates exist.
    """

    MAX_SESSION = timedelta(minutes=15)

    def __init__(self, context: ssl.SSLContext) -> None:
        if context.protocol != ssl.PROTOCOL_TLS_SERVER or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("a server context requiring client certificates is required")
        self._context = context
        self._lock = RLock()
        self._bindings: dict[str, _Binding] = {}
        self._crew_sessions: dict[str, str] = {}
        self._used_fingerprints: set[str] = set()
        self._used_session_ids: set[str] = set()

    def activate(
        self,
        identity: SessionIdentity,
        fingerprint: str,
        starts_at: datetime,
        *,
        replaces: str | None = None,
    ) -> None:
        """Admit a previously provisioned client certificate from trusted code.

        Fingerprints are derived from the exact certificate by the trusted
        provisioner, never from request arguments. Replacement revokes the old
        session atomically and requires a previously unused certificate.
        """
        _validate_binding(identity, fingerprint, starts_at)
        with self._lock:
            old = self._crew_sessions.get(identity.crew_id)
            if old != replaces:
                raise ValueError("active session replacement must name the current session")
            if identity.session_id in self._used_session_ids or fingerprint in self._used_fingerprints:
                raise ValueError("session identity and certificate cannot be reused")
            if old is not None:
                del self._bindings[old]
            self._bindings[identity.session_id] = _Binding(identity, fingerprint, starts_at)
            self._crew_sessions[identity.crew_id] = identity.session_id
            self._used_fingerprints.add(fingerprint)
            self._used_session_ids.add(identity.session_id)

    def revoke(self, session_id: str) -> None:
        """Terminate one active session without affecting other crew."""
        with self._lock:
            binding = self._bindings.pop(session_id, None)
            if binding is None:
                raise KeyError("session is not active")
            del self._crew_sessions[binding.identity.crew_id]

    def authenticate(self, connection: ssl.SSLSocket) -> SessionIdentity:
        """Check an established server-side mTLS connection and current mapping."""
        if (type(connection) is not ssl.SSLSocket or not connection.server_side
                or connection.context is not self._context or self._context.verify_mode != ssl.CERT_REQUIRED):
            raise ValueError("connection is not from the configured mTLS server")
        certificate = connection.getpeercert(binary_form=True)
        details = connection.getpeercert()
        if not certificate or not details or not details.get("notBefore") or not details.get("notAfter"):
            raise ValueError("verified peer certificate is unavailable")
        fingerprint = hashlib.sha256(certificate).hexdigest()
        now = datetime.now(UTC)
        certificate_starts = datetime.fromtimestamp(ssl.cert_time_to_seconds(details["notBefore"]), UTC)
        certificate_expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(details["notAfter"]), UTC)
        if not timedelta(0) < certificate_expires - certificate_starts <= self.MAX_SESSION:
            raise ValueError("client certificate lifetime exceeds the allowed bound")
        with self._lock:
            binding = next((entry for entry in self._bindings.values() if entry.fingerprint == fingerprint), None)
            if (binding is None or not max(binding.starts_at, certificate_starts)
                    <= now < min(binding.identity.expires_at, certificate_expires)):
                raise ValueError("peer session is unknown, revoked, or expired")
            return binding.identity


def _validate_binding(identity: SessionIdentity, fingerprint: str, starts_at: datetime) -> None:
    if not all(isinstance(value, str) and value.strip() for value in
               (identity.crew_id, identity.session_id, identity.embodiment_id)):
        raise ValueError("session requires nonempty crew, session, and embodiment identities")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("session requires an exact lowercase SHA-256 certificate fingerprint")
    expires = identity.expires_at
    if starts_at.utcoffset() is None or expires.utcoffset() is None:
        raise ValueError("session times must be timezone-aware")
    if not starts_at < expires or expires - starts_at > MutualTLSSessions.MAX_SESSION:
        raise ValueError("session must have a positive bounded lifetime")
