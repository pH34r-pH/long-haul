#!/usr/bin/env python3
"""Validate the cross-repository governance profile with no third-party dependencies."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PROFILE_ID = "https://ph34r.dev/ns/long-haul/repository-alignment/v1"
RESOURCE_POLICY_ID = "https://ph34r.dev/ns/long-haul/resource-policy/v1"

PROFILES = {
    "research-source",
    "reproducibility-archive",
    "product-data",
    "control-plane",
    "prototype-device-protocol",
    "publication-distribution",
    "private-recovery-mirror",
}
STATUSES = {"active", "pre-alpha", "transitional", "private", "publication", "archive"}
DATA_CLASSES = {"public", "private", "personal", "sensitive", "restricted"}
POLICY_STATES = {"allow", "deny", "review", "not_applicable", "external_policy"}
PROMOTION_STATES = {"protected", "manual", "not_applicable"}
PRIORITIES = {"P0", "P1", "P2", "P3"}
WORK_STATES = {"implemented", "active", "planned", "deferred", "not_applicable"}
ACTIONS = {
    "read",
    "index",
    "search",
    "ai_augment",
    "evaluate",
    "data_mining",
    "ai_training",
    "generative_ai_training",
    "derive",
    "annotate",
    "distribute",
    "publish",
    "retain",
    "delete",
}


def expect_object(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return {}
    return value


def expect_string(value: Any, path: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path} must be a non-empty string")
        return ""
    return value.strip()


def expect_bool(value: Any, path: str, errors: list[str]) -> bool:
    if not isinstance(value, bool):
        errors.append(f"{path} must be a boolean")
        return False
    return value


def expect_enum(value: Any, allowed: set[str], path: str, errors: list[str]) -> str:
    text = expect_string(value, path, errors)
    if text and text not in allowed:
        errors.append(f"{path} must be one of {sorted(allowed)}")
    return text


def validate_profile(profile: dict[str, Any], repository: str | None) -> list[str]:
    errors: list[str] = []
    if profile.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    if profile.get("profile_id") != PROFILE_ID:
        errors.append(f"profile_id must equal {PROFILE_ID}")

    declared_repository = expect_string(profile.get("repository"), "repository", errors)
    if repository and declared_repository and declared_repository != repository:
        errors.append(
            f"repository declares {declared_repository!r}, but workflow is running in {repository!r}"
        )
    expect_enum(profile.get("repository_profile"), PROFILES, "repository_profile", errors)
    expect_enum(profile.get("status"), STATUSES, "status", errors)

    authority = expect_object(profile.get("authority"), "authority", errors)
    for key in (
        "source_of_truth",
        "task_authority",
        "acceptance_authority",
        "deployment_authority",
    ):
        expect_string(authority.get(key), f"authority.{key}", errors)

    data = expect_object(profile.get("data"), "data", errors)
    expect_enum(data.get("default_class"), DATA_CLASSES, "data.default_class", errors)
    external_untrusted = expect_bool(
        data.get("external_content_is_untrusted"),
        "data.external_content_is_untrusted",
        errors,
    )
    if not external_untrusted:
        errors.append("external content must be treated as untrusted reference data")
    contains_personal = expect_bool(
        data.get("contains_personal_data"),
        "data.contains_personal_data",
        errors,
    )
    contains_secrets = expect_bool(
        data.get("contains_secrets"),
        "data.contains_secrets",
        errors,
    )
    if contains_secrets:
        errors.append("Git repositories must not declare committed secrets as an accepted state")
    retention = expect_string(data.get("retention"), "data.retention", errors)
    deletion = expect_string(data.get("deletion"), "data.deletion", errors)
    expect_string(data.get("secret_storage"), "data.secret_storage", errors)
    if contains_personal and (
        retention.casefold() in {"none", "unspecified", "not applicable"}
        or deletion.casefold() in {"none", "unspecified", "not applicable"}
    ):
        errors.append("personal-data repositories require explicit retention and deletion behavior")

    resource_policy = expect_object(profile.get("resource_policy"), "resource_policy", errors)
    if resource_policy.get("conforms_to") != RESOURCE_POLICY_ID:
        errors.append(f"resource_policy.conforms_to must equal {RESOURCE_POLICY_ID}")
    actions = expect_object(resource_policy.get("actions"), "resource_policy.actions", errors)
    action_keys = set(actions)
    missing = sorted(ACTIONS - action_keys)
    extra = sorted(action_keys - ACTIONS)
    if missing:
        errors.append(f"resource_policy.actions is missing: {', '.join(missing)}")
    if extra:
        errors.append(f"resource_policy.actions has unknown keys: {', '.join(extra)}")
    for action in sorted(ACTIONS & action_keys):
        expect_enum(
            actions.get(action),
            POLICY_STATES,
            f"resource_policy.actions.{action}",
            errors,
        )
    for forbidden_alias in ("ai_allowed", "training_allowed", "public"):
        if forbidden_alias in resource_policy:
            errors.append(
                f"resource_policy.{forbidden_alias} is too broad; declare each action independently"
            )

    change = expect_object(profile.get("change_control"), "change_control", errors)
    independent = expect_bool(
        change.get("independent_acceptance"),
        "change_control.independent_acceptance",
        errors,
    )
    if not independent:
        errors.append("independent acceptance must remain enabled")
    candidate = expect_bool(
        change.get("candidate_before_promotion"),
        "change_control.candidate_before_promotion",
        errors,
    )
    promotion = expect_enum(
        change.get("production_promotion"),
        PROMOTION_STATES,
        "change_control.production_promotion",
        errors,
    )
    if promotion != "not_applicable" and not candidate:
        errors.append("deployable repositories require a candidate before promotion")
    expect_string(
        change.get("rollback_or_revocation"),
        "change_control.rollback_or_revocation",
        errors,
    )
    expect_bool(
        change.get("negative_results_retained"),
        "change_control.negative_results_retained",
        errors,
    )

    evidence = expect_object(profile.get("evidence"), "evidence", errors)
    for key in ("exact_source_identity", "artifact_fingerprints", "provenance_receipts"):
        expect_bool(evidence.get(key), f"evidence.{key}", errors)

    boundaries = expect_object(profile.get("boundaries"), "boundaries", errors)
    for key in (
        "external_text_cannot_grant_authority",
        "model_output_cannot_self_approve",
        "deployment_separate_from_acceptance",
    ):
        value = expect_bool(boundaries.get(key), f"boundaries.{key}", errors)
        if not value:
            errors.append(f"boundaries.{key} must remain true")

    stabilization = profile.get("stabilization")
    if not isinstance(stabilization, list) or not stabilization:
        errors.append("stabilization must be a non-empty array")
    else:
        identifiers: set[str] = set()
        for index, item in enumerate(stabilization):
            path = f"stabilization[{index}]"
            record = expect_object(item, path, errors)
            identifier = expect_string(record.get("id"), f"{path}.id", errors)
            if identifier in identifiers:
                errors.append(f"{path}.id duplicates {identifier!r}")
            identifiers.add(identifier)
            expect_enum(record.get("priority"), PRIORITIES, f"{path}.priority", errors)
            expect_enum(record.get("status"), WORK_STATES, f"{path}.status", errors)
            expect_string(record.get("summary"), f"{path}.summary", errors)

    return errors


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "governance/repository-profile.json")
    if not path.is_file():
        print(f"::error file={path}::governance profile is missing")
        return 1
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"::error file={path}::cannot parse governance profile: {exc}")
        return 1
    if not isinstance(profile, dict):
        print(f"::error file={path}::profile root must be an object")
        return 1

    errors = validate_profile(profile, os.environ.get("GITHUB_REPOSITORY"))
    if errors:
        for message in errors:
            print(f"::error file={path}::{message}")
        return 1

    actions = profile["resource_policy"]["actions"]
    restricted = sorted(
        action for action, state in actions.items() if state in {"deny", "review", "external_policy"}
    )
    print(
        "Repository governance profile valid: "
        f"{profile['repository']} ({profile['repository_profile']}); "
        f"restricted actions={','.join(restricted) or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
