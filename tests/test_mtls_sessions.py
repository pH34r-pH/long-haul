"""Exercise the prototype through real local TLS handshakes, not fake principals."""
import hashlib
import shutil
import socket
import ssl
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from long_haul.mcp.mtls_sessions import MutualTLSSessions, SessionIdentity


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("real mTLS fixture requires openssl")
    root = tmp_path_factory.mktemp("mtls")

    def run(*args):
        subprocess.run(["openssl", *args], cwd=root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=fixture-ca",
        "-keyout", "ca.key", "-out", "ca.pem")
    for name, subject in (("server", "localhost"), ("a", "client-a"), ("b", "client-b"), ("next", "client-next")):
        run("req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={subject}",
            "-keyout", f"{name}.key", "-out", f"{name}.csr")
        run("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-days", "1", "-out", f"{name}.pem")
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=rogue",
        "-keyout", "rogue.key", "-out", "rogue.pem")
    return root


def fingerprint(root: Path, name: str) -> str:
    der = ssl.PEM_cert_to_DER_cert((root / f"{name}.pem").read_text())
    return hashlib.sha256(der).hexdigest()


def contexts(root: Path, client: str):
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(root / "server.pem", root / "server.key")
    server.load_verify_locations(root / "ca.pem")
    server.verify_mode = ssl.CERT_REQUIRED
    peer = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(root / "ca.pem"))
    peer.load_cert_chain(root / f"{client}.pem", root / f"{client}.key")
    return server, peer


def connect(server, client):
    first, second = socket.socketpair()
    server_sock = server.wrap_socket(first, server_side=True, do_handshake_on_connect=False)
    client_sock = client.wrap_socket(second, server_hostname="localhost", do_handshake_on_connect=False)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(server_sock.do_handshake), executor.submit(client_sock.do_handshake)]
        for future in futures:
            future.result(timeout=10)
    return server_sock, client_sock


def test_real_tls_sessions_are_distinct_revocable_and_replaceable(certificates):
    server, client_a = contexts(certificates, "a")
    _, client_b = contexts(certificates, "b")
    _, client_next = contexts(certificates, "next")
    sessions = MutualTLSSessions(server)
    now = datetime.now(UTC)
    a = SessionIdentity("crew-a", "session-a", "embodiment-a", now + timedelta(minutes=5))
    b = SessionIdentity("crew-b", "session-b", "embodiment-b", now + timedelta(minutes=5))
    replacement = SessionIdentity("crew-a", "session-next", "embodiment-next", now + timedelta(minutes=5))
    sessions.activate(a, fingerprint(certificates, "a"), now - timedelta(seconds=1))
    sessions.activate(b, fingerprint(certificates, "b"), now - timedelta(seconds=1))
    sa, ca = connect(server, client_a)
    sb, cb = connect(server, client_b)
    sn, cn = connect(server, client_next)
    try:
        assert sessions.authenticate(sa) == a
        assert sessions.authenticate(sb) == b
        with pytest.raises(ValueError, match="unknown, revoked, or expired"):
            sessions.authenticate(sn)
        sessions.activate(replacement, fingerprint(certificates, "next"), now - timedelta(seconds=1), replaces="session-a")
        with pytest.raises(ValueError, match="unknown, revoked, or expired"):
            sessions.authenticate(sa)
        assert sessions.authenticate(sn) == replacement
        assert sessions.authenticate(sb) == b
        sessions.revoke("session-b")
        with pytest.raises(ValueError, match="unknown, revoked, or expired"):
            sessions.authenticate(sb)
        assert sessions.authenticate(sn) == replacement
        with pytest.raises(ValueError, match="cannot be reused"):
            sessions.activate(b, fingerprint(certificates, "b"), now - timedelta(seconds=1))
    finally:
        for sock in (sa, ca, sb, cb, sn, cn):
            sock.close()


def test_expiry_is_rechecked_for_existing_connection(certificates):
    server, client = contexts(certificates, "a")
    sessions = MutualTLSSessions(server)
    now = datetime.now(UTC)
    identity = SessionIdentity("crew", "session", "embodiment", now + timedelta(minutes=5))
    sessions.activate(identity, fingerprint(certificates, "a"), now - timedelta(seconds=1))
    server_sock, client_sock = connect(server, client)
    try:
        assert sessions.authenticate(server_sock) == identity
        with patch("long_haul.mcp.mtls_sessions.datetime", wraps=datetime) as mocked:
            mocked.now.return_value = now + timedelta(minutes=6)
            with pytest.raises(ValueError, match="expired"):
                sessions.authenticate(server_sock)
    finally:
        server_sock.close()
        client_sock.close()


def test_invalid_replacement_preserves_current_session_and_reused_ids_are_denied(certificates):
    server, client = contexts(certificates, "a")
    sessions = MutualTLSSessions(server)
    now = datetime.now(UTC)
    current = SessionIdentity("crew", "session-a", "old", now + timedelta(minutes=5))
    replacement = SessionIdentity("crew", "session-next", "new", now + timedelta(minutes=5))
    sessions.activate(current, fingerprint(certificates, "a"), now)
    server_sock, client_sock = connect(server, client)
    try:
        with pytest.raises(ValueError, match="current session"):
            sessions.activate(replacement, fingerprint(certificates, "next"), now, replaces="wrong")
        with pytest.raises(ValueError, match="cannot be reused"):
            sessions.activate(replacement, fingerprint(certificates, "a"), now, replaces="session-a")
        assert sessions.authenticate(server_sock) == current
        sessions.revoke("session-a")
        with pytest.raises(ValueError, match="cannot be reused"):
            sessions.activate(current, fingerprint(certificates, "next"), now)
    finally:
        server_sock.close()
        client_sock.close()


def test_requires_server_verified_connection_and_bounded_session(certificates):
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with pytest.raises(ValueError, match="requiring client certificates"):
        MutualTLSSessions(insecure)
    server, client = contexts(certificates, "a")
    sessions = MutualTLSSessions(server)
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="bounded lifetime"):
        sessions.activate(SessionIdentity("crew", "session", "embodiment", now + timedelta(hours=1)),
                          fingerprint(certificates, "a"), now)
    with pytest.raises(ValueError, match="configured mTLS server"):
        sessions.authenticate(object())
    server_sock, client_sock = connect(server, client)
    try:
        with pytest.raises(ValueError, match="unknown, revoked, or expired"):
            sessions.authenticate(server_sock)
        with pytest.raises(ValueError, match="configured mTLS server"):
            sessions.authenticate(client_sock)
    finally:
        server_sock.close()
        client_sock.close()
    other_server, other_client = contexts(certificates, "a")
    other_sock, other_peer = connect(other_server, other_client)
    try:
        with pytest.raises(ValueError, match="configured mTLS server"):
            sessions.authenticate(other_sock)
    finally:
        other_sock.close()
        other_peer.close()


@pytest.mark.parametrize("client_name", [None, "rogue"])
def test_tls_handshake_rejects_missing_or_untrusted_client_certificate(certificates, client_name):
    server, _ = contexts(certificates, "a")
    client = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(certificates / "ca.pem"))
    if client_name:
        client.load_cert_chain(certificates / f"{client_name}.pem", certificates / f"{client_name}.key")
    first, second = socket.socketpair()
    first.settimeout(5)
    second.settimeout(5)
    server_sock = server.wrap_socket(first, server_side=True, do_handshake_on_connect=False)
    client_sock = client.wrap_socket(second, server_hostname="localhost", do_handshake_on_connect=False)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            server_future = executor.submit(server_sock.do_handshake)
            client_future = executor.submit(client_sock.do_handshake)
            with pytest.raises(ssl.SSLError):
                server_future.result(timeout=10)
            try:
                client_future.result(timeout=10)
            except ssl.SSLError:
                pass  # TLS 1.3 can report the server's rejection later on the client.
    finally:
        server_sock.close()
        client_sock.close()
