"""Pytest platform markers for POSIX-only isolation tests (#319)."""
from __future__ import annotations

import sys

import pytest

# Modules whose fixtures or helpers depend on POSIX os.defpath Git isolation,
# pipe select(), bash harnesses, or O_NOFOLLOW/fstat inode identity.
_POSIX_ONLY_MODULES: dict[str, str] = {
    "tests/test_native_receiver_script.py": "requires POSIX bash shell receiver harness",
    "tests/test_pi_derivation.py": "requires POSIX worker_candidate O_NOFOLLOW/fstat identity",
    "tests/test_pi_input.py": "requires POSIX worker_candidate O_NOFOLLOW/fstat identity",
    "tests/test_pi_recovery.py": "requires POSIX worker_candidate O_NOFOLLOW/fstat identity",
    "tests/test_pi_worker.py": "requires POSIX worker_candidate fstat identity, symlinks, and pipe select()",
    "tests/test_repository_context.py": "requires POSIX os.defpath Git context provider",
    "tests/test_repository_evaluator.py": "requires POSIX os.defpath Git provider and pipe select()",
    "tests/test_worker_candidate.py": "requires POSIX os.defpath Git provider and O_NOFOLLOW/fstat identity",
}

_POSIX_ONLY_TESTS: dict[tuple[str, str], str] = {
    ("tests/test_offline_package.py", "test_bundle_receipt_covers_exact_regular_members"): "requires POSIX wheel/member packaging semantics",
    ("tests/test_offline_package.py", "test_bundle_rejects_missing_project_wheel_and_symlinks"): "requires unprivileged POSIX symlinks",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    is_windows = sys.platform == "win32"
    for item in items:
        rel_path = item.path.relative_to(config.rootpath).as_posix()
        reason = _POSIX_ONLY_MODULES.get(rel_path) or _POSIX_ONLY_TESTS.get(
            (rel_path, item.originalname or item.name)
        )
        if reason is not None:
            item.add_marker(pytest.mark.posix_only(reason=reason))
            if is_windows:
                item.add_marker(pytest.mark.skip(reason=f"POSIX-only ({reason}); run on Linux or WSL"))
        elif is_windows and item.get_closest_marker("posix_only") is not None:
            marker = item.get_closest_marker("posix_only")
            marker_reason = (marker.kwargs.get("reason") if marker else None) or "POSIX-only test"
            item.add_marker(pytest.mark.skip(reason=f"POSIX-only ({marker_reason}); run on Linux or WSL"))
