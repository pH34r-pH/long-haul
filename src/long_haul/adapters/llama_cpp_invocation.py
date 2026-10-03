"""Explicit completion controls for the existing llama.cpp adapter.

These are invocation settings, not device, tokenizer, or scientific qualification.
The selected executable must implement the upstream completion interface.
"""
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

_INTEGER_FLAGS = {
    "seed": ("--seed", 0, 2**32 - 2),  # UINT32_MAX is upstream's random-seed sentinel.
    "context_size": ("--ctx-size", 1, 2**31 - 1),
    "threads": ("--threads", 1, 2**31 - 1),
    "threads_batch": ("--threads-batch", 1, 2**31 - 1),
}
_PREPARED_FLAGS = (
    "--no-conversation", "--no-display-prompt", "--no-escape",
    "--no-context-shift", "--offline", "--perf",
)


def prepared_completion(options: Mapping[str, object]) -> bool:
    enabled = options.get("prepared_completion", False)
    if type(enabled) is not bool:
        raise ValueError("prepared_completion requires a boolean")
    return enabled


def _integer_argument(name: str, value: object) -> list[str]:
    flag, minimum, maximum = _INTEGER_FLAGS[name]
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid explicit llama.cpp option: {name}")
    return [flag, str(value)]


def _prepared_arguments(options: Mapping[str, object]) -> list[str]:
    if not {"seed", "context_size", "threads"} <= options.keys():
        raise ValueError("prepared completion requires seed, context_size and threads")
    layers = options.get("gpu_layers", 0)
    if type(layers) is not int or not 0 <= layers <= 2**31 - 1:
        raise ValueError("prepared completion requires a nonnegative integer gpu_layers")
    if type(options.get("no_warmup", True)) is not bool:
        raise ValueError("no_warmup requires a boolean")
    return list(_PREPARED_FLAGS)


def invocation_arguments(options: Mapping[str, object]) -> list[str]:
    """Validate explicit controls without coercion or silently using defaults."""
    flags = _prepared_arguments(options) if prepared_completion(options) else []
    arguments: list[str] = []
    for name in _INTEGER_FLAGS:
        if name in options:
            arguments.extend(_integer_argument(name, options[name]))
    return arguments + flags


def prompt_arguments(prompt: str, options: Mapping[str, object],
                     path: Path | None, output_tokens: int) -> list[str]:
    if not prepared_completion(options):
        return ["-p", prompt]
    if path is None:
        raise ValueError("prepared completion requires a closed prompt file")
    if output_tokens >= options["context_size"]:
        raise ValueError("prepared completion output cap leaves no prompt context")
    return ["-bf", str(path)]


def invocation_error(options: Mapping[str, object]) -> str | None:
    try:
        invocation_arguments(options)
    except ValueError as exc:
        return str(exc)
    return None


def execution_environment(options: Mapping[str, object]) -> dict[str, str]:
    """Ignore ambient LLAMA_ARG_* overrides for an explicitly prepared call.

    This is configuration isolation, not credential or process sandboxing.
    In particular, the caller/Fleet still owns process credentials and devices.
    """
    env = os.environ.copy()
    if prepared_completion(options):
        env = {key: value for key, value in env.items()
               if not key.upper().startswith("LLAMA_ARG_")}
    visible_devices = options.get("cuda_visible_devices")
    if visible_devices is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(visible_devices)
    return env


@contextmanager
def prompt_file(prompt: str, options: Mapping[str, object]) -> Iterator[Path | None]:
    """Keep prepared UTF-8 bytes out of argv and remove them after the process.

    The file is closed before launch (including on Windows). This does not apply
    a chat template, tokenize, normalize line endings, or establish memory limits.
    """
    if not prepared_completion(options):
        yield None
        return
    if "\x00" in prompt:
        raise ValueError("prepared prompt must not contain NUL")
    with TemporaryDirectory(prefix="long-haul-prompt-") as directory:
        path = Path(directory) / "prompt.txt"
        path.write_bytes(prompt.encode("utf-8"))
        yield path
