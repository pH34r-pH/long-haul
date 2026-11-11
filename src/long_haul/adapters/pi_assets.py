"""Exact installed package/source identity; acquisition is a trusted separate step."""
from __future__ import annotations

import json
from pathlib import Path

from ..work.harness import HarnessIdentity
from ..work.repository_models import DigestReference, content_digest
from .harness_trace import _canonical

PACKAGE = "@earendil-works/pi-coding-agent"
VERSION = "1.5.2"
SOURCE = "b4afdddf44e059c67ccf5097e5317d913521b470"
TARBALL = "sha256:691bde186273b457d9d1c41d0fd47b57853b5e85ded1add1a1f6b78e66c261f4"
LOCK = "sha256:0c01bde5838e242aeb80022c15d6426a7a1ee5be7f0afe59d21109f8ac29366e"
INTEGRITY = "sha512-mUWI04rqF1Vd/m1q+Ps1XeWsTXEVvRUnsmRa83I3Dzua0Tx5bYS93imeqtIfrXf7PkkaR6ERz+AuQhIJlom5yA=="
CONTROL = Path(__file__).with_name("pi_control")


def runtime_fingerprint(root: Path) -> str:
    manifest = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            path.resolve(strict=True).relative_to(root.resolve())
            manifest.append((relative, "link", path.readlink().as_posix()))
        elif path.is_file():
            manifest.append((relative, "file", content_digest(path.read_bytes())))
        elif not path.is_dir():
            raise ValueError("unexpected installed runtime object")
    return content_digest(_canonical(manifest))


class PiRuntimeAssets:
    """Authenticated installation receipt, not evidence derived from a worker."""

    def __init__(self, root: Path, expected_fingerprint: str, audit_receipt: DigestReference):
        self.root = root.resolve(strict=True)
        self.fingerprint, self.audit_receipt = expected_fingerprint, audit_receipt
        self.verify()

    def verify(self):
        lock_path = self.root / "package-lock.json"
        lock = json.loads(lock_path.read_bytes())
        entry = lock["packages"]["node_modules/" + PACKAGE]
        installed = json.loads((self.root / "node_modules" / PACKAGE / "package.json").read_bytes())
        if (content_digest(lock_path.read_bytes()) != LOCK or entry["version"] != VERSION
                or entry["integrity"] != INTEGRITY or installed["name"] != PACKAGE or installed["version"] != VERSION
                or runtime_fingerprint(self.root) != self.fingerprint):
            raise ValueError("installed Pi runtime differs from exact admitted lock/package/tree")

    def identity(self, configuration: dict) -> HarnessIdentity:
        control = {p.name: content_digest(p.read_bytes()) for p in sorted(CONTROL.glob("*.mjs"))}
        config = {"worker": configuration, "control": control, "runtime_tree": self.fingerprint,
                  "lock": LOCK, "signature_provenance_receipt": self.audit_receipt.model_dump(mode="json")}
        return HarnessIdentity(
            harness_id="pi", package_name=PACKAGE, package_version=VERSION, protocol="pi-rpc-1.5.2",
            source=DigestReference(reference="git:earendil-works/pi:" + SOURCE,
                                   digest="sha256:da14b72e5d674959b64fc541e4a9241ac9fcdfa243431479119d3edb598b2231"),
            artifact=DigestReference(reference="urn:npm:earendil-works:pi-coding-agent:1.5.2", digest=TARBALL),
            configuration=DigestReference(reference="artifact:pi-worker-configuration", digest=content_digest(_canonical(config))),
        )

    def toolchain_reference(self):
        return DigestReference(reference="urn:long-haul:pi-toolchain:1.5.2", digest=content_digest(_canonical({
            "lock": LOCK, "tree": self.fingerprint, "package": PACKAGE, "version": VERSION, "source": SOURCE,
        })))
