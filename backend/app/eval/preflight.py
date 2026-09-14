"""Refuse early, with the fix in the message.

A benchmark that dies forty minutes in because Docker had 7 GiB is a wasted
five-hour window. Every probe is injectable so the rules are tested without
Docker; the real probes shell out.
"""
from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .config import eval_root

REQUIRED_SWEBENCH = "5.0.2"
DOCKER_MEM_REFUSE_GIB = 8.0
DOCKER_MEM_WARN_GIB = 12.0
DISK_REFUSE_GIB = 60.0

_BINARY_FOR_PROVIDER = {"claude": "claude", "codex": "codex"}


@dataclass
class Probes:
    docker_up: Callable[[], bool]
    docker_mem_gib: Callable[[], float]
    free_disk_gib: Callable[[], float]
    swebench_version: Callable[[], str | None]
    binaries_ok: Callable[[Sequence[str]], list[str]]  # returns the MISSING names


@dataclass
class PreflightReport:
    ok: bool
    refusals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def preflight(*, providers: Sequence[str], probes: Probes) -> PreflightReport:
    refusals: list[str] = []
    warnings: list[str] = []
    if not probes.docker_up():
        refusals.append("Docker daemon is not reachable; start Docker Desktop.")
    else:
        mem = probes.docker_mem_gib()
        if mem < DOCKER_MEM_REFUSE_GIB:
            refusals.append(
                f"Docker has {mem:.1f} GiB; scoring needs at least "
                f"{DOCKER_MEM_REFUSE_GIB:.0f} GiB — raise it in Docker Desktop → Resources."
            )
        elif mem < DOCKER_MEM_WARN_GIB:
            warnings.append(
                f"Docker has {mem:.1f} GiB; SWE-bench recommends 16 GB and we warn below "
                f"{DOCKER_MEM_WARN_GIB:.0f} GiB — builds may be slow or fail."
            )
    disk = probes.free_disk_gib()
    if disk < DISK_REFUSE_GIB:
        refusals.append(
            f"{disk:.0f} GiB free; images and build layers need at least {DISK_REFUSE_GIB:.0f} GiB."
        )
    version = probes.swebench_version()
    if version != REQUIRED_SWEBENCH:
        refusals.append(
            f"swebench {version or 'is not installed'}; this runner is pinned to "
            f"{REQUIRED_SWEBENCH} — `.venv/bin/pip install -e '.[eval]'`."
        )
    wanted = [_BINARY_FOR_PROVIDER[p] for p in providers if p in _BINARY_FOR_PROVIDER]
    for name in probes.binaries_ok(wanted):
        refusals.append(f"the `{name}` binary is not on PATH or not logged in.")
    return PreflightReport(ok=not refusals, refusals=refusals, warnings=warnings)


# ── real probes ────────────────────────────────────────────────────────────

def _docker_info() -> dict | None:
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def real_probes() -> Probes:
    info = _docker_info()

    def docker_up() -> bool:
        return info is not None

    def docker_mem_gib() -> float:
        return float(info.get("MemTotal", 0)) / 2**30 if info else 0.0

    def free_disk_gib() -> float:
        root = eval_root()
        root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(root).free / 2**30

    def swebench_version() -> str | None:
        try:
            return importlib.metadata.version("swebench")
        except importlib.metadata.PackageNotFoundError:
            return None

    def binaries_ok(names: Sequence[str]) -> list[str]:
        # Presence on PATH only. "Logged in" is the binary's own business — we
        # never read its credential store to find out (the invariant).
        return [n for n in names if shutil.which(n) is None]

    return Probes(docker_up, docker_mem_gib, free_disk_gib, swebench_version, binaries_ok)
