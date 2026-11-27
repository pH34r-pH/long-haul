"""Lossless offline interchange for the Long-Haul authoring projection."""

from __future__ import annotations

import json
from typing import Any

from .resource_policy_models import (
    RESOURCE_POLICY_PROFILE_ID,
    ResourcePolicyProfile,
    canonical_json,
)

_ORDERED_FIELDS = (
    "conforms_to",
    "permissions",
    "prohibitions",
    "provenance",
    "quality",
    "license_references",
    "rights_references",
    "spdx_references",
    "attestation_references",
    "derived_from",
    "actions",
    "purposes",
    "principal_ids",
    "crew_ids",
    "mission_ids",
    "contract_ids",
    "repository_ids",
    "audience_ids",
    "duties",
    "used",
    "generated",
    "data_categories",
    "data_subjects",
    "processing_operations",
    "legal_basis",
    "consent_references",
    "recipients",
    "storage_locations",
    "risk_references",
    "measure_references",
)
_CONTEXT = {
    "@version": 1.1,
    "@vocab": "https://ph34r.dev/ns/long-haul/",
    "lh": "https://ph34r.dev/ns/long-haul/",
    "dct": "http://purl.org/dc/terms/",
    "profile_id": {"@id": "dct:conformsTo", "@type": "@id"},
    **{
        field: {"@id": f"lh:{field}", "@container": "@list"}
        for field in _ORDERED_FIELDS
    },
}
_RECORD_TYPE = "lh:ResourcePolicyRecord"
_DOCUMENT_FIELDS = {"@context", "@id", "@type", "fingerprint"}


def export_policy_jsonld(profile: ResourcePolicyProfile) -> str:
    """Serialize the complete authoring record using a fixed inline context."""
    fingerprint = profile.fingerprint
    _, digest = fingerprint.split(":", 1)
    return canonical_json(
        {
            "@context": _CONTEXT,
            "@id": f"{RESOURCE_POLICY_PROFILE_ID}/record/{digest}",
            "@type": _RECORD_TYPE,
            "fingerprint": fingerprint,
            **profile.model_dump(mode="json", exclude_none=True),
        }
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in resource policy JSON-LD")
        result[key] = value
    return result


def import_policy_jsonld(
    data: str | bytes, *, expected_fingerprint: str
) -> ResourcePolicyProfile:
    """Read the compact record against a fingerprint from independently reviewed control.

    The import binds content identity. Caller and policy authority still belong to
    the existing admission boundary.
    """
    document = json.loads(data, object_pairs_hook=_unique_object)
    if not isinstance(document, dict) or document.get("@context") != _CONTEXT:
        raise ValueError("resource policy JSON-LD requires the fixed inline context")
    if document.get("fingerprint") != expected_fingerprint:
        raise ValueError(
            "resource policy JSON-LD differs from the expected fingerprint"
        )
    fields = {
        key: value for key, value in document.items() if key not in _DOCUMENT_FIELDS
    }
    profile = ResourcePolicyProfile.model_validate(fields)
    if canonical_json(document) != export_policy_jsonld(profile):
        raise ValueError("resource policy JSON-LD does not match the canonical record")
    return profile
