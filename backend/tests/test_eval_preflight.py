from __future__ import annotations

from backend.app.eval.preflight import Probes, preflight


def _probes(**over):
    base = dict(
        docker_up=lambda: True,
        docker_mem_gib=lambda: 16.0,
        free_disk_gib=lambda: 200.0,
        swebench_version=lambda: "5.0.2",
        binaries_ok=lambda names: [],
    )
    base.update(over)
    return Probes(**base)


def test_a_healthy_machine_passes_with_no_warnings():
    rep = preflight(providers=("claude",), probes=_probes())
    assert rep.ok and rep.refusals == [] and rep.warnings == []


def test_low_docker_memory_refuses_and_names_the_fix():
    rep = preflight(providers=("claude",), probes=_probes(docker_mem_gib=lambda: 7.7))
    assert not rep.ok
    assert any("Docker Desktop" in r and "8 GiB" in r for r in rep.refusals)


def test_middling_docker_memory_only_warns():
    rep = preflight(providers=("claude",), probes=_probes(docker_mem_gib=lambda: 10.0))
    assert rep.ok and any("12 GiB" in w for w in rep.warnings)


def test_missing_binary_refuses_by_name():
    rep = preflight(
        providers=("codex",), probes=_probes(binaries_ok=lambda names: ["codex"])
    )
    assert not rep.ok and any("codex" in r for r in rep.refusals)


def test_wrong_swebench_version_refuses():
    rep = preflight(providers=("claude",), probes=_probes(swebench_version=lambda: "4.0.0"))
    assert not rep.ok and any("5.0.2" in r for r in rep.refusals)


def test_low_disk_refuses():
    rep = preflight(providers=("claude",), probes=_probes(free_disk_gib=lambda: 30.0))
    assert not rep.ok and any("60 GiB" in r for r in rep.refusals)
