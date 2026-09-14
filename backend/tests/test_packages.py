from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.core.packages import (
    InstalledPackage,
    PackageError,
    PackageSource,
    discover_resources,
    extension_files,
    install_package,
    load_packages,
    parse_source,
    purge_package,
    read_manifest,
    remove_package,
    resource_paths,
    settings_path,
    update_packages,
)


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def make_package(
    root: Path, *, manifest: dict | None = None, manifest_file: str = "package.json"
) -> Path:
    write(root / "extensions" / "one.py", "def setup(api): pass\n")
    write(root / "extensions" / "two.py", "def setup(api): pass\n")
    write(root / "extensions" / "legacy.py", "def setup(api): pass\n")
    write(root / "extensions" / "nested" / "index.py", "def setup(api): pass\n")
    write(root / "skills" / "s" / "SKILL.md", "---\ndescription: a skill\n---\nbody\n")
    write(root / "prompts" / "p.md", "template\n")
    if manifest is not None:
        write(root / manifest_file, json.dumps(manifest))
    return root


# ── spec parsing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("npm:pkg", ("npm", "pkg", None)),
        ("npm:pkg@1.2.3", ("npm", "pkg", "1.2.3")),
        ("npm:@scope/pkg", ("npm", "@scope/pkg", None)),
        ("npm:@scope/pkg@2.0.0-beta.1", ("npm", "@scope/pkg", "2.0.0-beta.1")),
        ("git:github.com/user/repo", ("git", "github.com/user/repo", None)),
        ("git:github.com/user/repo@v1", ("git", "github.com/user/repo", "v1")),
        ("https://github.com/user/repo", ("git", "github.com/user/repo", None)),
    ],
)
def test_parse_source(spec: str, expected: tuple[str, str, str | None]) -> None:
    source = parse_source(spec)
    assert (source.type, source.name, source.ref) == expected
    assert (
        source.spec == spec.replace("https://", "git:") if spec.startswith("https") else source.spec
    )


def test_parse_source_paths_and_errors(project: Path) -> None:
    absolute = parse_source(str(project))
    assert absolute.type == "path" and absolute.name == str(project)
    relative = parse_source("./rel")
    assert relative.type == "path" and Path(relative.name).is_absolute()
    assert parse_source("git:github.com/u/r").url == "https://github.com/u/r"
    with pytest.raises(PackageError):
        parse_source("  ")


def test_install_dir_name_is_filesystem_safe() -> None:
    # leading/trailing dashes are stripped so a directory never looks like a CLI flag
    assert PackageSource("npm", "@scope/pkg").install_dir_name == "scope-pkg"
    assert PackageSource("git", "github.com/user/repo").install_dir_name == "github.com-user-repo"


# ── manifest + discovery ───────────────────────────────────────────────────


def test_conventions_used_without_a_manifest(project: Path) -> None:
    pkg = make_package(project / "conv")
    assert read_manifest(pkg) is None
    res = discover_resources(pkg)
    assert [p.name for p in res.extensions] == ["extensions"]
    assert [p.name for p in res.skills] == ["skills"]
    assert [p.name for p in res.prompts] == ["prompts"]
    assert extension_files([InstalledPackage("x", str(pkg), "user")]) == [
        str(pkg / "extensions" / "legacy.py"),
        str(pkg / "extensions" / "nested" / "index.py"),
        str(pkg / "extensions" / "one.py"),
        str(pkg / "extensions" / "two.py"),
    ]


def test_manifest_globs_and_exclusions(project: Path) -> None:
    pkg = make_package(
        project / "glob",
        manifest={
            "name": "globby",
            "localcode": {"extensions": ["extensions/*.py", "!extensions/legacy.py"], "skills": []},
        },
    )
    manifest = read_manifest(pkg)
    assert manifest == {"extensions": ["extensions/*.py", "!extensions/legacy.py"], "skills": []}
    res = discover_resources(pkg)
    assert sorted(p.name for p in res.extensions) == ["one.py", "two.py"]
    assert res.skills == []  # explicitly disabled
    assert [p.name for p in res.prompts] == ["prompts"]  # falls back to convention


def test_localcode_json_manifest_and_bare_form(project: Path) -> None:
    pkg = make_package(
        project / "lc",
        manifest={"extensions": ["extensions/one.py"]},
        manifest_file="localcode.json",
    )
    assert read_manifest(pkg) == {"extensions": ["extensions/one.py"]}
    assert [p.name for p in discover_resources(pkg).extensions] == ["one.py"]
    broken = write(project / "broken" / "package.json", "{not json")
    assert read_manifest(broken.parent) is None


def test_settings_filters_override_the_manifest(project: Path) -> None:
    pkg = make_package(project / "filt", manifest={"localcode": {"extensions": ["extensions"]}})
    installed = InstalledPackage(
        "x", str(pkg), "user", filters={"extensions": ["extensions/two.py"]}
    )
    assert resource_paths([installed], "extensions") == [str(pkg / "extensions" / "two.py")]
    disabled = InstalledPackage("x", str(pkg), "user", filters={"extensions": []})
    assert resource_paths([disabled], "extensions") == []


def test_discover_resources_on_a_missing_directory(project: Path) -> None:
    res = discover_resources(project / "nope")
    assert res.extensions == [] and res.skills == [] and res.prompts == []


# ── install / remove / update ──────────────────────────────────────────────


def test_install_path_package_records_settings(agent_dir: Path, project: Path) -> None:
    pkg_dir = make_package(project / "mine")
    installed = install_package(str(pkg_dir), cwd=project, agent_dir=agent_dir)
    assert installed.scope == "user" and installed.path == str(pkg_dir)
    settings = json.loads((agent_dir / "settings.json").read_text())
    assert settings["packages"] == [{"source": str(pkg_dir)}]
    loaded = load_packages(cwd=project, agent_dir=agent_dir)
    assert [p.source for p in loaded] == [str(pkg_dir)]
    assert resource_paths(loaded, "skills") == [str(pkg_dir / "skills")]
    # re-installing the same spec replaces rather than duplicating
    install_package(str(pkg_dir), cwd=project, agent_dir=agent_dir)
    assert len(json.loads((agent_dir / "settings.json").read_text())["packages"]) == 1


def test_install_project_scope_and_scope_order(agent_dir: Path, project: Path) -> None:
    user_pkg = make_package(project / "u")
    proj_pkg = make_package(project / "p")
    install_package(str(user_pkg), cwd=project, agent_dir=agent_dir, scope="user")
    install_package(str(proj_pkg), cwd=project, agent_dir=agent_dir, scope="project")
    assert settings_path("project", cwd=project).is_file()
    loaded = load_packages(cwd=project, agent_dir=agent_dir)
    assert [p.scope for p in loaded] == ["user", "project"]
    only_user = load_packages(cwd=project, agent_dir=agent_dir, include_project=False)
    assert [p.scope for p in only_user] == ["user"]


def test_install_rejects_a_package_with_nothing_to_offer(agent_dir: Path, project: Path) -> None:
    empty = project / "empty"
    empty.mkdir()
    with pytest.raises(PackageError, match="provides no extensions"):
        install_package(str(empty), cwd=project, agent_dir=agent_dir)
    with pytest.raises(PackageError, match="not a directory"):
        install_package(str(project / "ghost"), cwd=project, agent_dir=agent_dir)


def test_remote_install_uses_the_injected_fetcher(agent_dir: Path, project: Path) -> None:
    fetched = make_package(project / "remote")
    calls: list[str] = []

    def fake_fetch(source: PackageSource, *, agent_dir: Path | None = None) -> Path:
        calls.append(source.spec)
        return fetched

    installed = install_package(
        "npm:@acme/tools@1.0.0", cwd=project, agent_dir=agent_dir, fetch=fake_fetch
    )
    assert calls == ["npm:@acme/tools@1.0.0"]
    assert installed.source == "npm:@acme/tools@1.0.0"
    entry = json.loads((agent_dir / "settings.json").read_text())["packages"][0]
    assert entry == {"source": "npm:@acme/tools@1.0.0", "path": str(fetched)}
    results = update_packages(cwd=project, agent_dir=agent_dir, fetch=fake_fetch)
    assert results == [("npm:@acme/tools@1.0.0", None)]
    assert calls == ["npm:@acme/tools@1.0.0", "npm:@acme/tools@1.0.0"]


def test_update_reports_per_package_failure(agent_dir: Path, project: Path) -> None:
    fetched = make_package(project / "r2")
    install_package(
        "git:github.com/u/r@v1",
        cwd=project,
        agent_dir=agent_dir,
        fetch=lambda source, agent_dir=None: fetched,
    )
    install_package(str(make_package(project / "local")), cwd=project, agent_dir=agent_dir)

    def failing(source: PackageSource, *, agent_dir: Path | None = None) -> Path:
        raise PackageError("network down")

    results = dict(update_packages(cwd=project, agent_dir=agent_dir, fetch=failing))
    assert results["git:github.com/u/r@v1"] == "network down"
    assert results[str(project / "local")] is None  # path packages are never re-fetched


def test_remove_and_purge(agent_dir: Path, project: Path) -> None:
    pkg_dir = make_package(project / "gone")
    install_package(str(pkg_dir), cwd=project, agent_dir=agent_dir)
    assert remove_package(str(pkg_dir), cwd=project, agent_dir=agent_dir) is True
    assert load_packages(cwd=project, agent_dir=agent_dir) == []
    assert remove_package("npm:never-installed", cwd=project, agent_dir=agent_dir) is False
    fetched_dir = agent_dir / "git" / "github.com-u-r"
    make_package(fetched_dir)
    assert purge_package("git:github.com/u/r", agent_dir=agent_dir) is True
    assert not fetched_dir.exists()
    assert purge_package(str(pkg_dir), agent_dir=agent_dir) is False  # path sources are left alone
    assert pkg_dir.is_dir()


def test_malformed_settings_entries_are_skipped(agent_dir: Path, project: Path) -> None:
    pkg_dir = make_package(project / "ok")
    (agent_dir / "settings.json").write_text(
        json.dumps({"packages": [str(pkg_dir), {"no": "source"}, 42, {"source": "  "}]})
    )
    loaded = load_packages(cwd=project, agent_dir=agent_dir)
    assert [p.source for p in loaded] == [str(pkg_dir)]  # bare string form still works
