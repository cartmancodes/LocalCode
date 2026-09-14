"""Extension discovery and loading (pi: ``core/extensions/loader.ts``).

Discovery order, like pi's:

1. user-global ``<agent_dir>/extensions/*.py`` and ``<agent_dir>/extensions/*/index.py``
2. project-local ``<cwd>/.localcode/extensions/…`` — only when the project is trusted
3. explicit paths (``-e path``)

An extension module exposes ``setup(api)`` (sync or async). ``default`` and
``extension`` are accepted aliases. A module that fails to import or whose
``setup`` raises is recorded as an :class:`ExtensionError` and skipped; the
rest still load.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import CONFIG_DIR_NAME, get_default_agent_dir
from .api import ExtensionAPI
from .runner import ExtensionRunner, LoadedExtension
from .types import ExtensionError

FACTORY_NAMES = ("setup", "default", "extension")


@dataclass
class LoadExtensionsResult:
    extensions: list[LoadedExtension] = field(default_factory=list)
    errors: list[ExtensionError] = field(default_factory=list)


def _scan_dir(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        if child.name.startswith((".", "_")):
            continue
        if child.is_file() and child.suffix == ".py":
            found.append(child)
        elif child.is_dir() and (child / "index.py").is_file():
            found.append(child / "index.py")
    return found


def discover_extension_paths(
    *,
    cwd: str | Path,
    agent_dir: str | Path | None = None,
    include_project: bool = True,
    extra_paths: Iterable[str | Path] = (),
) -> list[Path]:
    base = Path(agent_dir) if agent_dir else get_default_agent_dir()
    paths = _scan_dir(base / "extensions")
    if include_project:
        paths += _scan_dir(Path(cwd).expanduser().resolve() / CONFIG_DIR_NAME / "extensions")
    for extra in extra_paths:
        p = _normalize(extra)
        if p.is_dir():
            paths += _scan_dir(p)
        elif p.is_file():
            paths.append(p)
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(rp)
    return unique


def _module_name(path: Path) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    return f"localcode_ext_{digest}"


def _extension_name(path: Path) -> str:
    return path.parent.name if path.name == "index.py" else path.stem


def _normalize(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _import_factory(p: Path) -> Any:
    """Blocking part of loading: import the module, find ``setup``."""
    spec = importlib.util.spec_from_file_location(_module_name(p), p)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create import spec for {p}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    factory = next(
        (getattr(module, n) for n in FACTORY_NAMES if callable(getattr(module, n, None))),
        None,
    )
    if factory is None:
        raise ImportError("extension module has no setup(api) function")
    return factory


async def load_extension(path: str | Path, runner: ExtensionRunner) -> LoadedExtension | None:
    p = _normalize(path)
    ext = LoadedExtension(path=str(p), name=_extension_name(p))
    try:
        factory = _import_factory(p)
        result = factory(ExtensionAPI(ext, runner))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — a bad extension must not take the session down
        runner.record_error(str(p), "load", exc)
        sys.modules.pop(_module_name(p), None)
        return None
    runner.add(ext)
    return ext


async def load_extensions(
    paths: Iterable[str | Path], runner: ExtensionRunner
) -> LoadExtensionsResult:
    result = LoadExtensionsResult()
    before = len(runner.errors)
    for path in paths:
        ext = await load_extension(path, runner)
        if ext is not None:
            result.extensions.append(ext)
    result.errors = list(runner.errors[before:])
    return result
