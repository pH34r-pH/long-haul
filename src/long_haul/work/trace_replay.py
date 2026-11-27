"""Read-only progress replay for independently pinned repository trajectories."""
from __future__ import annotations

import json
import math
import platform
import sys
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from .harness import verify_semantic_trace
from .progress import ProgressMonitor, ProgressPolicy
from .repository_models import content_digest


def _implementation() -> dict[str, object]:
    root = Path(__file__).parent
    return {
        "python": platform.python_version(),
        "pydantic": version("pydantic"),
        "pydantic_core": version("pydantic-core"),
        "source_lf_sha256": {
            name: content_digest((root / name).read_bytes().replace(b"\r\n", b"\n"))
            for name in ("trace_replay.py", "progress.py", "harness.py", "repository_models.py")
        },
    }


def _summarize(completed, policy: ProgressPolicy) -> dict[str, object]:
    monitor = ProgressMonitor(policy)
    signals = []
    measured, duration = 0, 0.0
    for event in completed:
        if event.duration_seconds is not None:
            measured += 1
            duration += event.duration_seconds
        signal = monitor.observe(event.attempt_step())
        if signal is not None:
            signals.append({
                "sequence": event.sequence,
                "reason": signal.reason,
                "would_terminate": signal.terminate_attempt,
                "observed_duration_seconds": event.duration_seconds,
            })
    return {
        "completed_actions": len(completed),
        "outcomes": dict(Counter(event.outcome.value for event in completed)),
        "progress_kinds": dict(Counter(event.progress.value for event in completed)),
        "completion_durations": {
            "observed": measured,
            "missing": len(completed) - measured,
            "observed_sum_seconds": duration if measured and math.isfinite(duration) else None,
        },
        "signals": signals,
        "first_termination_signal": next(
            (signal["sequence"] for signal in signals if signal["would_terminate"]), None,
        ),
    }


def replay_trajectory(
    result_bytes: bytes, trace_bytes: bytes, *, expected_result_fingerprint: str,
    max_bytes: int, policy: ProgressPolicy | None = None,
) -> dict[str, object]:
    """Project observed actions and hypothetical interventions without executing them."""
    retained, events = verify_semantic_trace(
        result_bytes, trace_bytes, expected_result_fingerprint=expected_result_fingerprint, max_bytes=max_bytes,
    )
    completed = tuple(event for event in events if event.phase == "end")
    normalizations = {event.normalization.digest for event in completed}
    if len(normalizations) > 1:
        raise ValueError("completed actions use different trace normalization identities")
    selected_policy = ProgressPolicy() if policy is None else ProgressPolicy.model_validate(
        policy.model_dump(), strict=True,
    )
    return {
        "schema": "long-haul-repository-progress-replay/v1",
        "result_fingerprint": retained.fingerprint,
        "trace": retained.trace.model_dump(mode="json"),
        "harness_fingerprint": retained.attempt.harness.fingerprint,
        "normalization_digest": next(iter(normalizations), None),
        "terminal": retained.terminal,
        "candidate_disposition": retained.candidate_disposition,
        "acceptance": retained.acceptance,
        "observed_events": len(events),
        "event_phases": dict(Counter(event.phase for event in events)),
        "policy": selected_policy.model_dump(mode="json"),
        "implementation": _implementation(),
        "replay": _summarize(completed, selected_policy),
    }


def _read(path: Path, budget: int) -> bytes:
    if not path.is_file():
        raise ValueError("trajectory replay inputs must be regular files")
    with path.open("rb") as source:
        data = source.read(budget + 1)
    if len(data) > budget:
        raise ValueError("trajectory replay inputs exceed the caller byte budget")
    return data


def _policy_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


def _parse_policy(raw: bytes) -> ProgressPolicy:
    try:
        encoded = json.loads(raw, object_pairs_hook=_policy_object)
        if not isinstance(encoded, dict) or set(encoded) - ProgressPolicy.model_fields.keys():
            raise ValueError
        return ProgressPolicy.model_validate(encoded, strict=True)
    except (ValueError, RecursionError):
        raise ValueError("invalid progress policy JSON") from None


def replay_trajectory_files(
    result: Path, trace: Path, *, expected_result_fingerprint: str,
    max_bytes: int, policy: Path | None = None,
) -> dict[str, object]:
    """Apply an aggregate read budget to the result, trace and optional policy."""
    if type(max_bytes) is not int or not 0 < max_bytes < sys.maxsize:
        raise ValueError("trajectory replay requires a positive byte budget")
    policy_bytes = _read(policy, max_bytes) if policy is not None else b""
    selected_policy = _parse_policy(policy_bytes) if policy is not None else None
    available = max_bytes - len(policy_bytes)
    result_bytes = _read(result, available)
    trace_bytes = _read(trace, available - len(result_bytes))
    return replay_trajectory(
        result_bytes, trace_bytes, expected_result_fingerprint=expected_result_fingerprint,
        max_bytes=available, policy=selected_policy,
    )
