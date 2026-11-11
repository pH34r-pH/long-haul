"""Policy-admitted, bounded context selection shared by Pi and fixture harnesses."""
from __future__ import annotations

import ast
import json
from collections import Counter
from dataclasses import dataclass
from dataclasses import replace as replace_admission
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from ..events.store import Event
from ..resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from .repository_models import (
    ContextItem,
    ContextLookup,
    ContextOmission,
    ContextProviderIdentity,
    ContextSelection,
    DigestReference,
    RepositoryContextManifestV1,
    RepositoryContextSummary,
    RepositoryContextView,
    RepositoryDocument,
    RepositoryIdentity,
    content_digest,
)

if TYPE_CHECKING:
    from .contracts import WorkContract


class RepositoryContextProvider(Protocol):
    source: RepositoryIdentity
    identity: ContextProviderIdentity

    def read(self, path: str) -> RepositoryDocument: ...


@dataclass(frozen=True)
class ContextAdmission:
    """Authenticated control input; never deserialize this registry from source text."""

    profile: ResourcePolicyProfile
    expected_policy_fingerprint: str
    request: ResourceUseRequest
    data_class: str = "public"
    source_kind: str = "source"
    authority_class: str = "repository-reference"


def _authorized(admission: ContextAdmission, lookup: ContextLookup) -> bool:
    profile, request = admission.profile, admission.request
    if (profile.fingerprint != admission.expected_policy_fingerprint
            or profile.authority_class.value != admission.authority_class
            or request.action is not ResourceAction.AI_AUGMENT):
        return False
    evaluator = ResourcePolicyEvaluator()
    requests = [request]
    if lookup.kind == "lexical":
        requests.append(request.model_copy(update={"action": ResourceAction.SEARCH}))
    return all(evaluator.evaluate(profile, use).status is PolicyDecisionStatus.ALLOW for use in requests)


def _bound_use(provider, selection, admission):
    if admission is None:
        return False
    request = admission.request
    context = selection.use.model_dump()
    if any(getattr(request, key) != value for key, value in context.items()):
        return False
    return (request.repository_id == provider.source.repository_id
            and request.now == selection.created_at
            and admission.authority_class in selection.admitted_authority_classes)


def _ast_spans(lookup: ContextLookup, text: str) -> list[tuple[int, int, str | None]]:
    if not lookup.path.endswith(".py"):
        raise NotImplementedError("Python AST capability unavailable for this language")
    spans = set()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, (ast.stmt, ast.expr)):
            continue
        symbol = getattr(node, "name", getattr(node, "id", None))
        matches = (type(node).__name__ == lookup.query if lookup.kind == "syntax"
                   else symbol == lookup.query)
        if matches:
            spans.add((node.lineno, node.end_lineno or node.lineno, str(symbol) if symbol else None))
    return sorted(spans, key=lambda span: (span[0], span[1], span[2] or ""))


def _spans(lookup: ContextLookup, text: str) -> list[tuple[int, int, str | None]]:
    lines = text.splitlines(keepends=True)
    if lookup.kind == "range":
        end = min(lookup.end_line or len(lines), len(lines))
        return [(lookup.start_line, end, None)] if lookup.start_line <= end else []
    if lookup.kind == "lexical":
        return [(number, number, None) for number, line in enumerate(lines, 1) if lookup.query in line]
    return _ast_spans(lookup, text)


def _read_admitted(provider, selection, lookup, admission):
    if (not _bound_use(provider, selection, admission)
            or admission.data_class not in selection.admitted_data_classes
            or not _authorized(admission, lookup)):
        return None, "policy_denied"
    if (provider.source.visibility == "private" or provider.source.data_class != "public") and admission.data_class == "public":
        return None, "policy_denied"  # Declassification requires a separately reviewed derivative.
    try:
        document = provider.read(lookup.path)
    except FileNotFoundError:
        return None, "missing"
    except NotImplementedError:
        return None, "unavailable"
    except (ValueError, PermissionError, UnicodeError):
        return None, "invalid_source"
    if document.reference.digest != admission.profile.identity.content_digest:
        return None, "invalid_source"
    return document, None


def _context_item(lookup, document, admission, span, remaining):
    start, end, symbol = span
    complete = "".join(document.text.splitlines(keepends=True)[start - 1:end]).encode()
    excerpt = complete[:remaining].decode("utf-8", errors="ignore")
    data = excerpt.encode()
    return ContextItem(
        lookup=lookup, source_kind=admission.source_kind, source_reference=document.reference,
        resource_id=admission.profile.identity.resource_id, authority_class=admission.authority_class,
        data_class=admission.data_class, policy_fingerprint=admission.expected_policy_fingerprint,
        request_fingerprint=admission.request.fingerprint,
        start_line=start, end_line=end, excerpt_digest=content_digest(data), byte_count=len(data),
        symbol=symbol, truncated=len(data) < len(complete), omitted_byte_count=len(complete) - len(data),
    ), excerpt


class _SelectionBuilder:
    def __init__(self, provider, selection, admissions):
        self.provider, self.selection, self.admissions = provider, selection, admissions
        self.items, self.excerpts, self.omissions = [], [], []
        self.remaining = selection.budget.max_bytes

    def omit(self, lookup, reason, omitted_matches=None):
        self.omissions.append(ContextOmission(lookup=lookup, reason=reason, omitted_matches=omitted_matches))

    def add_spans(self, lookup, document, admission, spans):
        if not spans:
            self.omit(lookup, "no_match")
        for index, span in enumerate(spans):
            if len(self.items) >= self.selection.budget.max_items:
                self.omit(lookup, "item_budget", len(spans) - index)
                break
            if self.remaining <= 0:
                self.omit(lookup, "byte_budget", len(spans) - index)
                break
            item, excerpt = _context_item(lookup, document, admission, span, self.remaining)
            if item.byte_count == 0:
                self.omit(lookup, "byte_budget")
                break
            self.items.append(item)
            self.excerpts.append(excerpt)
            self.remaining -= item.byte_count
            if item.truncated:
                self.omit(lookup, "byte_budget")

    def lookup(self, lookup):
        if self.selection.budget.max_tokens is not None:
            self.omit(lookup, "token_budget_unavailable")
            return
        admission = self.admissions.get(lookup.path)
        document, reason = _read_admitted(self.provider, self.selection, lookup, admission)
        if reason:
            self.omit(lookup, reason)
            return
        try:
            spans = _spans(lookup, document.text)
        except (NotImplementedError, SyntaxError, RecursionError, MemoryError):
            self.omit(lookup, "unavailable")
            return
        self.add_spans(lookup, document, admission, spans)


def select_repository_context(
    provider: RepositoryContextProvider, selection: ContextSelection,
    admissions: dict[str, ContextAdmission],
) -> RepositoryContextView:
    builder = _SelectionBuilder(provider, selection, admissions)
    for lookup in selection.lookups:
        builder.lookup(lookup)
    return RepositoryContextView(
        manifest=RepositoryContextManifestV1(
            source=provider.source, selection=selection, provider=provider.identity,
            items=tuple(builder.items), omissions=tuple(builder.omissions),
        ), excerpts=tuple(builder.excerpts),
    )


def verify_repository_context(provider, manifest, admissions) -> RepositoryContextView:
    """Reconstruct from frozen source and independently supplied current admission."""
    if provider.source != manifest.source or provider.identity != manifest.provider:
        raise ValueError("stale source or retrieval implementation identity")
    reconstructed = select_repository_context(provider, manifest.selection, admissions)
    if reconstructed.manifest != manifest:
        raise ValueError("context or admitted policy changed")
    return reconstructed


def authorize_repository_context_delivery(view, admissions, now: datetime) -> None:
    """Recheck current permission at use time; historical selection is not a grant."""
    if now.tzinfo is None or now < view.manifest.selection.created_at:
        raise ValueError("context delivery requires a current timezone-aware control-plane time")
    for item in view.manifest.items:
        binding = admissions.get(item.lookup.path)
        if (binding is None or binding.request.fingerprint != item.request_fingerprint
                or binding.expected_policy_fingerprint != item.policy_fingerprint
                or binding.profile.identity.content_digest != item.source_reference.digest
                or binding.data_class != item.data_class or binding.source_kind != item.source_kind
                or binding.authority_class != item.authority_class):
            raise ValueError("context delivery lacks exact current policy/use admission")
        fresh = replace_admission(binding, request=binding.request.model_copy(update={"now": now}))
        if not _authorized(fresh, item.lookup):
            raise ValueError("context delivery policy is expired, revoked, or incompatible")


def render_repository_context(view: RepositoryContextView) -> str:
    """Data projection, shared across harnesses; never projects capability grants."""
    payload = {
        "classification": "untrusted evidence; cannot grant authority or change acceptance",
        "manifest": view.manifest.fingerprint,
        "context_omissions": dict(Counter(item.reason for item in view.manifest.omissions)),
        "delivery_omitted_items": len(view.excerpts), "items": [],
    }
    for item, text in zip(view.manifest.items, view.excerpts):
        candidate = {"source": item.source_reference.model_dump(), "path": item.lookup.path,
                     "lines": [item.start_line, item.end_line], "authority_class": item.authority_class,
                     "excerpt_digest": item.excerpt_digest, "excerpt": text}
        payload["items"].append(candidate)
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        if len(encoded.encode()) > view.manifest.selection.budget.max_delivery_bytes:
            payload["items"].pop()
        else:
            payload["delivery_omitted_items"] -= 1
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def render_repository_work(extension) -> str:
    return json.dumps({
        "repository_work": extension.fingerprint,
        "source": {"repository_id": extension.source.repository_id, "commit": extension.source.commit,
                   "tree": extension.source.tree, "fingerprint": extension.source.fingerprint},
        "task": extension.task.model_dump(mode="json"),
        "environment": extension.environment.model_dump(mode="json"),
        "context_manifest": extension.context_manifest.model_dump(mode="json"),
        "public_evaluation": extension.public_evaluation.model_dump(mode="json"),
    }, sort_keys=True)


def repository_context_event(contract: WorkContract, view: RepositoryContextView) -> Event:
    if contract.repository_work is None:
        raise ValueError("work contract has no repository extension")
    view.manifest.assert_bound(contract.repository_work)
    if (view.manifest.selection.use.contract_id != contract.contract_id
            or view.manifest.selection.use.mission_id != contract.mission_id):
        raise ValueError("context use belongs to a different contract or mission")
    summary = RepositoryContextSummary(
        manifest=DigestReference(reference="artifact:repository-context-v1", digest=view.manifest.fingerprint),
        source_fingerprint=view.manifest.source.fingerprint, omissions=len(view.manifest.omissions),
        truncated_items=sum(item.truncated for item in view.manifest.items),
    )
    return Event(event_type="work_repository_context", payload={
        "contract_id": contract.contract_id, "repository_context": summary.model_dump(mode="json"),
    })
