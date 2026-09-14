"""Packages — bundles of extensions, skills and prompts (pi: ``package-manager.ts``).

A package is a directory with a manifest declaring what it provides:

    // package.json (npm-style) or localcode.json (standalone)
    {
      "name": "my-package",
      "localcode": {
        "extensions": ["./extensions"],
        "skills": ["./skills"],
        "prompts": ["./prompts"]
      }
    }

Without a manifest the convention directories ``extensions/``, ``skills/``
and ``prompts/`` are discovered automatically. Manifest entries accept globs
and ``!`` exclusions.

Install sources, in pi's spelling:

    localcode install npm:@scope/pkg@1.2.3
    localcode install git:github.com/user/repo@v1
    localcode install https://github.com/user/repo
    localcode install /absolute/path/to/package

User scope (default) installs under ``<agent_dir>/{npm,git}/``; ``--local``
records the package in ``<cwd>/.localcode/settings.json`` instead. Path
sources are never copied — they are referenced where they are, so a package
you are developing stays live.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import CONFIG_DIR_NAME, get_default_agent_dir

MANIFEST_KEY = "localcode"
MANIFEST_FILES = ("package.json", "localcode.json")
RESOURCE_KINDS = ("extensions", "skills", "prompts")
CONVENTION_DIRS = {"extensions": "extensions", "skills": "skills", "prompts": "prompts"}

Scope = Literal["user", "project"]
SourceType = Literal["npm", "git", "path"]


class PackageError(RuntimeError):
    pass


@dataclass
class PackageSource:
    """A parsed install spec."""

    type: SourceType
    name: str  # npm package name, ``host/user/repo``, or an absolute path
    ref: str | None = None  # npm version or git ref
    url: str | None = None  # full clone URL for git sources

    @property
    def spec(self) -> str:
        if self.type == "path":
            return self.name
        base = f"{self.type}:{self.name}"
        return f"{base}@{self.ref}" if self.ref else base

    @property
    def install_dir_name(self) -> str:
        """Directory name under ``<agent_dir>/<type>/`` — one slot per package
        name, so re-installing at a new ref replaces rather than accumulates."""
        return re.sub(r"[^A-Za-z0-9._-]+", "-", self.name).strip("-")


@dataclass
class PackageResources:
    extensions: list[Path] = field(default_factory=list)
    skills: list[Path] = field(default_factory=list)
    prompts: list[Path] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Path]:
        return getattr(self, kind)  # type: ignore[no-any-return]


@dataclass
class InstalledPackage:
    source: str  # the spec as written, e.g. "npm:@foo/tools@1.2.3"
    path: str  # where it lives on disk
    scope: Scope
    name: str = ""
    filters: dict[str, list[str]] = field(default_factory=dict)

    def to_settings(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"source": self.source}
        if self.source.startswith(("npm:", "git:")):
            entry["path"] = self.path
        entry.update(self.filters)
        return entry


# ── spec parsing ───────────────────────────────────────────────────────────


def parse_source(spec: str) -> PackageSource:
    """``npm:pkg@ver`` | ``git:host/user/repo@ref`` | URL | path → PackageSource."""
    raw = spec.strip()
    if not raw:
        raise PackageError("empty package spec")

    if raw.startswith("npm:"):
        body = raw[4:]
        name, ref = _split_ref(body, scoped=body.startswith("@"))
        if not name:
            raise PackageError(f"npm spec has no package name: {spec!r}")
        return PackageSource("npm", name, ref)

    if raw.startswith("git:") or raw.startswith(("https://", "http://", "git@")):
        body = raw[4:] if raw.startswith("git:") else raw
        name, ref = _split_ref(body, scoped=False)
        url = name if name.startswith(("https://", "http://", "git@")) else f"https://{name}"
        display = re.sub(r"^(https?://|git@)", "", name).replace(":", "/").removesuffix(".git")
        return PackageSource("git", display, ref, url)

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return PackageSource("path", str(path))


def _split_ref(body: str, *, scoped: bool) -> tuple[str, str | None]:
    """Split ``name@ref``, tolerating a leading ``@scope/`` on npm names."""
    idx = body.find("@", 1 if scoped else 0)
    if idx <= 0:
        return body, None
    return body[:idx], body[idx + 1 :] or None


# ── manifest + resource discovery ──────────────────────────────────────────


def read_manifest(package_dir: str | Path) -> dict[str, list[str]] | None:
    """Return the ``localcode`` manifest block, or ``None`` when absent."""
    root = Path(package_dir)
    for filename in MANIFEST_FILES:
        candidate = root / filename
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        block = data.get(MANIFEST_KEY)
        if (
            filename == "localcode.json"
            and block is None
            and any(k in data for k in RESOURCE_KINDS)
        ):
            block = data  # a bare localcode.json may hold the resource keys directly
        if not isinstance(block, dict):
            continue
        return {
            kind: [str(p) for p in block[kind] if isinstance(p, str)]
            for kind in RESOURCE_KINDS
            if isinstance(block.get(kind), list)
        }
    return None


def _match_patterns(root: Path, patterns: list[str]) -> list[Path]:
    """Resolve manifest patterns: directories expand, globs expand, ``!`` and
    ``-`` exclude, ``+`` force-includes."""
    included: list[Path] = []
    excluded: set[Path] = set()
    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        negate = pattern.startswith(("!", "-"))
        force = pattern.startswith("+")
        if negate or force:
            pattern = pattern[1:].strip()
        pattern = pattern.removeprefix("./")
        matches: list[Path] = []
        target = root / pattern
        if target.is_dir():
            matches = [target]
        elif target.is_file():
            matches = [target]
        else:
            matches = sorted(p for p in root.glob(pattern) if p.exists())
        if negate:
            for m in matches:
                excluded.add(m.resolve())
        else:
            included.extend(matches)
    seen: set[Path] = set()
    out: list[Path] = []
    for path in included:
        resolved = path.resolve()
        if resolved in excluded or resolved in seen:
            continue
        # A file inside an excluded directory is excluded too.
        if any(str(resolved).startswith(str(ex) + "/") for ex in excluded):
            continue
        seen.add(resolved)
        out.append(path)
    return out


def discover_resources(
    package_dir: str | Path, filters: dict[str, list[str]] | None = None
) -> PackageResources:
    """What a package provides: manifest paths if present, else conventions.

    ``filters`` (from settings.json) override the manifest for that kind; an
    empty list disables the kind entirely.
    """
    root = Path(package_dir)
    resources = PackageResources()
    if not root.is_dir():
        return resources
    manifest = read_manifest(root)
    for kind in RESOURCE_KINDS:
        patterns: list[str] | None = None
        if filters is not None and kind in filters:
            patterns = filters[kind]
        elif manifest is not None and kind in manifest:
            patterns = manifest[kind]
        if patterns is not None:
            paths = _match_patterns(root, patterns) if patterns else []
        else:
            convention = root / CONVENTION_DIRS[kind]
            paths = [convention] if convention.is_dir() else []
        resources.by_kind(kind).extend(paths)
    return resources


def resource_paths(packages: list[InstalledPackage], kind: str) -> list[str]:
    """Flatten one resource kind across packages, in install order."""
    out: list[str] = []
    for pkg in packages:
        for path in discover_resources(pkg.path, pkg.filters or None).by_kind(kind):
            out.append(str(path))
    return out


def extension_files(packages: list[InstalledPackage]) -> list[str]:
    """Extension *files* from packages — a declared directory expands to the
    ``.py`` modules inside it, matching how the loader scans a directory."""
    files: list[str] = []
    for path_str in resource_paths(packages, "extensions"):
        path = Path(path_str)
        if path.is_file() and path.suffix == ".py":
            files.append(str(path))
        elif path.is_dir():
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                if child.name.startswith((".", "_")):
                    continue
                if child.is_file() and child.suffix == ".py":
                    files.append(str(child))
                elif child.is_dir() and (child / "index.py").is_file():
                    files.append(str(child / "index.py"))
    return files


# ── settings ───────────────────────────────────────────────────────────────


def settings_path(scope: Scope, *, cwd: str | Path, agent_dir: str | Path | None = None) -> Path:
    if scope == "project":
        return Path(cwd) / CONFIG_DIR_NAME / "settings.json"
    base = Path(agent_dir) if agent_dir else get_default_agent_dir()
    return base / "settings.json"


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_packages(
    *, cwd: str | Path, agent_dir: str | Path | None = None, include_project: bool = True
) -> list[InstalledPackage]:
    """Every installed package, user scope first then project scope."""
    out: list[InstalledPackage] = []
    scopes: list[Scope] = ["user", "project"] if include_project else ["user"]
    for scope in scopes:
        settings = _read_settings(settings_path(scope, cwd=cwd, agent_dir=agent_dir))
        for entry in settings.get("packages", []) or []:
            pkg = _package_from_entry(entry, scope, cwd=cwd, agent_dir=agent_dir)
            if pkg is not None:
                out.append(pkg)
    return out


def _package_from_entry(
    entry: Any, scope: Scope, *, cwd: str | Path, agent_dir: str | Path | None
) -> InstalledPackage | None:
    if isinstance(entry, str):
        entry = {"source": entry}
    if not isinstance(entry, dict) or not entry.get("source"):
        return None
    source = str(entry["source"])
    path = entry.get("path")
    if not path:
        try:
            parsed = parse_source(source)
        except PackageError:
            return None
        path = (
            parsed.name
            if parsed.type == "path"
            else str(install_root(parsed, agent_dir=agent_dir) / parsed.install_dir_name)
        )
    filters = {k: list(v) for k, v in entry.items() if k in RESOURCE_KINDS and isinstance(v, list)}
    return InstalledPackage(
        source=source,
        path=str(path),
        scope=scope,
        name=str(entry.get("name") or Path(str(path)).name),
        filters=filters,
    )


def install_root(source: PackageSource, *, agent_dir: str | Path | None = None) -> Path:
    base = Path(agent_dir) if agent_dir else get_default_agent_dir()
    return base / source.type


# ── install / remove / update ──────────────────────────────────────────────


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        raise PackageError(f"{cmd[0]} is not installed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PackageError(f"{' '.join(cmd)} timed out") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        raise PackageError(f"{' '.join(cmd)} failed:\n" + "\n".join(tail))


def fetch_package(source: PackageSource, *, agent_dir: str | Path | None = None) -> Path:
    """Materialise a package on disk and return its directory.

    Path sources are referenced in place; npm and git sources are fetched
    into ``<agent_dir>/{npm,git}/<name>/``.
    """
    if source.type == "path":
        path = Path(source.name)
        if not path.is_dir():
            raise PackageError(f"not a directory: {path}")
        return path

    root = install_root(source, agent_dir=agent_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / source.install_dir_name

    if source.type == "npm":
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        spec = f"{source.name}@{source.ref}" if source.ref else source.name
        _run(["npm", "install", "--no-save", "--ignore-scripts", "--prefix", str(target), spec])
        installed = target / "node_modules" / source.name
        if not installed.is_dir():
            raise PackageError(f"npm install produced no package at {installed}")
        return installed

    # git
    if (target / ".git").is_dir():
        _run(["git", "-C", str(target), "fetch", "--depth", "1", "origin", source.ref or "HEAD"])
        _run(["git", "-C", str(target), "checkout", "--force", "FETCH_HEAD"])
    else:
        if target.exists():
            shutil.rmtree(target)
        cmd = ["git", "clone", "--depth", "1"]
        if source.ref:
            cmd += ["--branch", source.ref]
        cmd += [source.url or f"https://{source.name}", str(target)]
        _run(cmd)
    return target


def install_package(
    spec: str,
    *,
    cwd: str | Path,
    agent_dir: str | Path | None = None,
    scope: Scope = "user",
    fetch: Any = fetch_package,
) -> InstalledPackage:
    """Fetch a package and record it in the scope's settings file."""
    source = parse_source(spec)
    path = fetch(source, agent_dir=agent_dir)
    resources = discover_resources(path)
    if not any(resources.by_kind(kind) for kind in RESOURCE_KINDS):
        raise PackageError(
            f"{spec} provides no extensions, skills or prompts "
            f"(looked for a '{MANIFEST_KEY}' manifest key and the convention "
            f"directories {', '.join(CONVENTION_DIRS.values())})"
        )
    pkg = InstalledPackage(source=source.spec, path=str(path), scope=scope, name=Path(path).name)
    file = settings_path(scope, cwd=cwd, agent_dir=agent_dir)
    settings = _read_settings(file)
    entries = [e for e in settings.get("packages", []) or [] if _entry_source(e) != source.spec]
    entries.append(pkg.to_settings())
    settings["packages"] = entries
    _write_settings(file, settings)
    return pkg


def normalize_spec(spec: str) -> str:
    """The canonical form an install records, so ``remove ./pkg`` matches an
    install that stored the absolute path."""
    try:
        return parse_source(spec).spec
    except PackageError:
        return spec


def remove_package(
    spec: str, *, cwd: str | Path, agent_dir: str | Path | None = None, scope: Scope | None = None
) -> bool:
    """Drop a package from settings. Fetched copies stay on disk so a
    re-install is cheap; ``purge`` deletes them."""
    removed = False
    wanted = normalize_spec(spec)
    scopes: list[Scope] = [scope] if scope else ["user", "project"]
    for sc in scopes:
        file = settings_path(sc, cwd=cwd, agent_dir=agent_dir)
        settings = _read_settings(file)
        entries = settings.get("packages", []) or []
        kept = [e for e in entries if normalize_spec(_entry_source(e)) != wanted]
        if len(kept) != len(entries):
            settings["packages"] = kept
            _write_settings(file, settings)
            removed = True
    return removed


def purge_package(spec: str, *, agent_dir: str | Path | None = None) -> bool:
    """Delete a fetched package's directory. Path sources are never deleted."""
    source = parse_source(spec)
    if source.type == "path":
        return False
    target = install_root(source, agent_dir=agent_dir) / source.install_dir_name
    if target.is_dir():
        shutil.rmtree(target)
        return True
    return False


def update_packages(
    *,
    cwd: str | Path,
    agent_dir: str | Path | None = None,
    fetch: Any = fetch_package,
) -> list[tuple[str, str | None]]:
    """Re-fetch every non-path package. Returns ``(spec, error)`` per package;
    pinned refs are honoured — updating reconciles the clone to its ref."""
    results: list[tuple[str, str | None]] = []
    for pkg in load_packages(cwd=cwd, agent_dir=agent_dir):
        source = parse_source(pkg.source)
        if source.type == "path":
            results.append((pkg.source, None))
            continue
        try:
            fetch(source, agent_dir=agent_dir)
            results.append((pkg.source, None))
        except PackageError as exc:
            results.append((pkg.source, str(exc)))
    return results


def _entry_source(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("source", ""))
    return ""
