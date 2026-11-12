"""Experimental authorized seed delivery to a verified workspace holder.

The admission callback and policy registry belong to trusted control. This
component does not authenticate the scheduler or provide atomic revocation.
"""
from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..work.repository_models import content_digest
from ..work.repository_sandbox import _control_environment
from .pi_workspace_cleanup import WorkspaceCleanupRequired, _container, _daemon
from .pi_workspace_copy import copy_docker_workspace
from .pi_workspace_fence import WorkspaceFenceGrant
from .pi_workspace_holder import HolderProfile, verify_workspace_holder
from .pi_workspace_intent import load_workspace_intent
from .pi_workspace_seed import build_workspace_seed


@dataclass(frozen=True)
class HolderSeeder:
    root: Path = field(repr=False)
    token: str = field(repr=False)
    intent_fingerprint: str = field(repr=False)
    control: Callable = field(repr=False)
    docker_path: Path = field(repr=False)
    holder_id: str = field(repr=False)
    profile: HolderProfile = field(repr=False)
    admit: Callable = field(repr=False)
    grant: WorkspaceFenceGrant = field(repr=False)

    def _check(self, intent, seed_digest):
        if (not isinstance(self.profile, HolderProfile) or not isinstance(self.grant, WorkspaceFenceGrant)
                or not self.grant.belongs(self.root, self.token, self.intent_fingerprint)
                or not self.grant.admit_seed(seed_digest) or not callable(self.admit)
                or self.admit(seed_digest) is not True):
            raise ValueError("workspace seed lacks current admission")
        if load_workspace_intent(self.root, self.token, self.intent_fingerprint) != intent:
            raise ValueError("workspace intent changed during seed delivery")
        _daemon(self.control, intent.daemon_id)
        if _container(self.control, intent.worker_name, intent.labels("worker")) is not None:
            raise ValueError("workspace worker already exists during seeding")
        verify_workspace_holder(self.control, intent, self.profile, self.holder_id, running=True)

    def seed(self, targets, contents):
        """Copy an exact admitted archive once, then require exact file readback."""
        try:
            intent = load_workspace_intent(self.root, self.token, self.intent_fingerprint)
            targets, contents = set(targets), dict(contents)
            payload = build_workspace_seed(targets, contents)
            digest = content_digest(payload)  # Private control binding; never a public event.
            if not isinstance(self.grant, WorkspaceFenceGrant) or not self.grant.belongs(self.root, self.token, self.intent_fingerprint):
                raise ValueError("workspace seed lacks its held local fence")
            self.grant.begin_seed(digest)
            self._check(intent, digest)
            if copy_docker_workspace(self.docker_path, self.holder_id, 15):
                raise ValueError("workspace holder was not empty before seed")
            self._check(intent, digest)
            with tempfile.TemporaryDirectory(prefix="long-haul-docker-config-") as config:
                subprocess.run([str(self.docker_path), "--config", config, "cp", "-a", "-", self.holder_id + ":/workspace"],
                               input=payload, env=_control_environment(), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15, check=True)
            self._check(intent, digest)
            if copy_docker_workspace(self.docker_path, self.holder_id, 15) != contents:
                raise ValueError("workspace seed readback differs from admitted bytes")
            self._check(intent, digest)
            self.grant.finish_seed(digest)
        except Exception:  # noqa: BLE001 -- ambiguous provider effects require recovery and a held lease.
            raise WorkspaceCleanupRequired() from None
