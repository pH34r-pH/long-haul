"""Immutable identity projections for repository work and bounded context."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)


def content_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def repository_path(value: str) -> str:
    path = PurePosixPath(value)
    if (not value or len(value) > 1024 or value != path.as_posix() or path.is_absolute()
            or any(part in {"..", ".git"} for part in path.parts)
            or any(char in value for char in "\\\x00\r\n:")):
        raise ValueError("expected a normalized repository-relative path")
    return value


class FrozenProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def fingerprint(self) -> str:
        return content_digest(json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode())


class DigestReference(FrozenProjection):
    reference: str = Field(pattern=r"^(urn|artifact|git):[A-Za-z0-9._:/+@-]+$", max_length=512)
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RepositoryMaterial(FrozenProjection):
    path: str
    kind: Literal["submodule", "lfs", "generated"]
    identity: str = Field(pattern=r"^(sha256:)?[0-9a-f]{40}([0-9a-f]{24})?$")
    _path = field_validator("path")(repository_path)

    @model_validator(mode="after")
    def exact_material(self):
        import re
        pattern = r"[0-9a-f]{40}|[0-9a-f]{64}" if self.kind == "submodule" else r"sha256:[0-9a-f]{64}"
        if not re.fullmatch(pattern, self.identity):
            raise ValueError("material kind requires an exact Git or SHA-256 identity")
        return self


class RepositoryIdentity(FrozenProjection):
    repository_id: str = Field(pattern=r"^[A-Za-z0-9_.:/-]+$", max_length=256)
    visibility: Literal["public", "private"]
    data_class: Literal["public", "private", "personal", "sensitive", "restricted"]
    commit: str = Field(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")
    tree: str = Field(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")
    materials: tuple[RepositoryMaterial, ...] = Field(default=(), max_length=256)

    @field_validator("materials")
    @classmethod
    def normalized_materials(cls, values):
        if len({(v.path, v.kind) for v in values}) != len(values):
            raise ValueError("material path/kind identities must be unique")
        return tuple(sorted(values, key=lambda v: (v.path, v.kind)))


class RepositoryWorkExtensionV1(FrozenProjection):
    schema_version: Literal[1] = 1
    profile: Literal["repository-work-v1"] = "repository-work-v1"
    source: RepositoryIdentity
    task: DigestReference
    environment: DigestReference
    toolchain: DigestReference
    context_manifest: DigestReference
    public_evaluation: DigestReference
    sealed_evaluator: DigestReference | None = None
    candidate_form: Literal["patch", "commit", "tree", "artifact"] = "tree"
    dependency_policy: Literal["offline", "locked-artifacts"] = "offline"
    cache_policy: Literal["disabled", "content-addressed"] = "disabled"
    dependency_cache: DigestReference | None = None
    network_policy: Literal["none", "admitted-profile"] = "none"
    network_profile: DigestReference | None = None
    partition_references: tuple[DigestReference, ...] = ()

    @model_validator(mode="after")
    def network_identity(self):
        if (self.network_policy == "admitted-profile") != (self.network_profile is not None):
            raise ValueError("network policy must match its admitted profile reference")
        if (self.cache_policy == "content-addressed") != (self.dependency_cache is not None):
            raise ValueError("cache policy must match its immutable cache identity")
        return self


class ContextBudget(FrozenProjection):
    max_bytes: int = Field(default=8192, ge=0, le=1048576)
    max_items: int = Field(default=16, ge=0, le=1000)
    max_tokens: int | None = Field(default=None, ge=0)
    max_delivery_bytes: int = Field(default=16384, ge=512, le=1048576)


class ContextLookup(FrozenProjection):
    path: str
    kind: Literal["range", "lexical", "syntax", "symbol"] = "range"
    query: str | None = Field(default=None, min_length=1, max_length=256)
    reason: str = Field(default="task evidence", min_length=1, max_length=256)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    _path = field_validator("path")(repository_path)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end line precedes start line")
        if self.kind != "range" and self.query is None:
            raise ValueError("query is required for lexical/syntax/symbol lookup")
        return self


class ContextItem(FrozenProjection):
    lookup: ContextLookup
    source_kind: Literal["source", "test", "build", "config", "documentation", "issue", "pr", "generated"]
    source_reference: DigestReference
    resource_id: str = Field(min_length=1, max_length=512)
    authority_class: Literal["repository-reference", "external-source", "generated-content"]
    data_class: Literal["public", "private", "personal", "sensitive", "restricted"]
    policy_fingerprint: str = Field(pattern=r"^resource-policy-v1:[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^resource-use-v1:[0-9a-f]{64}$")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    excerpt_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    byte_count: int = Field(ge=0)
    symbol: str | None = None
    truncated: bool = False
    omitted_byte_count: int = Field(default=0, ge=0)
    policy_evaluator_implementation: str | None = Field(
        default=None, pattern=r"^resource-policy-evaluator-v1:sha256:[0-9a-f]{64}$"
    )

    @model_serializer(mode="wrap")
    def serialize_legacy_identity(self, handler):
        result = handler(self)
        if self.policy_evaluator_implementation is None:
            result.pop("policy_evaluator_implementation", None)
        return result

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_line < self.start_line:
            raise ValueError("end line precedes start line")
        return self


class ContextOmission(FrozenProjection):
    lookup: ContextLookup
    reason: Literal["byte_budget", "item_budget", "token_budget_unavailable", "policy_denied",
                    "missing", "unavailable", "invalid_source", "no_match"]
    omitted_matches: int | None = Field(default=None, ge=0)


class ContextUseBinding(FrozenProjection):
    caller_id: str = Field(min_length=1, max_length=256)
    crew_id: str = Field(min_length=1, max_length=256)
    contract_id: str = Field(min_length=1, max_length=256)
    mission_id: str = Field(min_length=1, max_length=256)
    purpose: str = Field(min_length=1, max_length=256)


class ContextSelection(FrozenProjection):
    task: DigestReference
    environment: DigestReference
    budget: ContextBudget = Field(default_factory=ContextBudget)
    lookups: tuple[ContextLookup, ...] = Field(max_length=128)
    use: ContextUseBinding
    created_by: DigestReference
    created_at: datetime
    admitted_data_classes: tuple[Literal["public", "private", "personal", "sensitive", "restricted"], ...] = ("public",)
    admitted_authority_classes: tuple[Literal["repository-reference", "external-source", "generated-content"], ...] = ("repository-reference",)

    @field_validator("lookups")
    @classmethod
    def ordered_lookups(cls, values):
        if len({v.fingerprint for v in values}) != len(values):
            raise ValueError("duplicate context lookups")
        return tuple(sorted(values, key=lambda v: v.fingerprint))

    @field_validator("created_at")
    @classmethod
    def aware_creation(cls, value):
        if value.tzinfo is None:
            raise ValueError("creation time must be timezone-aware")
        from datetime import UTC
        return value.astimezone(UTC)


class ContextProviderIdentity(FrozenProjection):
    name: str
    version: str
    tool_versions: tuple[str, ...]
    implementation: DigestReference
    configuration: tuple[tuple[str, str], ...] = ()


class RepositoryDocument(FrozenProjection):
    reference: DigestReference
    text: str

    @model_validator(mode="after")
    def exact_content(self):
        if content_digest(self.text.encode()) != self.reference.digest:
            raise ValueError("source content fingerprint mismatch")
        return self


class RepositoryContextManifestV1(FrozenProjection):
    schema_version: Literal[1] = 1
    profile: Literal["repository-context-v1"] = "repository-context-v1"
    source: RepositoryIdentity
    selection: ContextSelection
    provider: ContextProviderIdentity
    items: tuple[ContextItem, ...] = Field(default=(), max_length=1000)
    omissions: tuple[ContextOmission, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def bounded(self):
        budget = self.selection.budget
        if len(self.items) > budget.max_items or sum(i.byte_count for i in self.items) > budget.max_bytes:
            raise ValueError("manifest exceeds its context budget")
        return self

    def assert_bound(self, extension: RepositoryWorkExtensionV1) -> None:
        if (self.source != extension.source or self.selection.task != extension.task
                or self.selection.environment != extension.environment
                or self.fingerprint != extension.context_manifest.digest):
            raise ValueError("stale or mismatched repository/task/environment/context identity")


class RepositoryContextView(FrozenProjection):
    manifest: RepositoryContextManifestV1
    excerpts: tuple[str, ...]

    @model_validator(mode="after")
    def exact_excerpts(self):
        if len(self.excerpts) != len(self.manifest.items):
            raise ValueError("context excerpts do not match manifest items")
        for item, text in zip(self.manifest.items, self.excerpts):
            data = text.encode()
            if len(data) != item.byte_count or content_digest(data) != item.excerpt_digest:
                raise ValueError("context excerpt fingerprint mismatch")
        return self


class RepositoryContextSummary(FrozenProjection):
    manifest: DigestReference
    source_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    omissions: int = Field(ge=0)
    truncated_items: int = Field(ge=0)
