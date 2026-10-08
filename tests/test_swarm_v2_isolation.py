import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from targets.codex_target import codex_home

from hooks import _redis
from hooks.config import _agentibrain_home
from hooks.memory import store as memory_store
from hooks.observability import event_relay
from scripts.claude_config import claude_home
from scripts.herdr_setup import config_path as herdr_config_path
from tests import installer_isolation, swarm_v2_isolation
from tests.swarm_v2_isolation import (
    LIVE_PROGRAMS,
    REJECTIONS,
    acquire,
    build,
    confine,
    confine_environ,
    live_roots,
    owned,
    remove,
    sweep,
)

pytestmark = pytest.mark.xdist_group("fakeredis")
Failed = pytest.fail.Exception


@pytest.fixture
def rejections():
    before = REJECTIONS.copy()
    yield lambda: {k: v - before.get(k, 0) for k, v in REJECTIONS.items() if v != before.get(k, 0)}


def _fake_program(directory: Path, name: str, output: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    program = directory / name
    program.write_text(f'#!/bin/sh\necho {output} "$@"\n')
    program.chmod(0o755)
    return program


# T-SV2-FND-05-A


def test_a_every_live_resolver_points_inside_the_test_directory(tmp_path):
    for label, path in (
        ("claude home", claude_home()),
        ("codex home", codex_home()),
        ("brain home", _agentibrain_home()),
        ("herdr config", herdr_config_path()),
        ("kubeconfig", Path(os.environ["KUBECONFIG"])),
    ):
        assert path.resolve().is_relative_to(tmp_path.resolve()), label
    assert "VAULT_ROOT" not in os.environ


def test_a_live_roots_cover_the_brain_vault_kube_herdr_and_every_configured_home(tmp_path):
    home = tmp_path / "home"
    environ = {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "profiles/engineer/claude"),
        "CODEX_HOME": f"{tmp_path / 'codex-a'},{tmp_path / 'codex-b'}",
        "VAULT_ROOT": str(tmp_path / "vault"),
        "KUBECONFIG": f"{tmp_path / 'kube/one'}{os.pathsep}{tmp_path / 'kube/two'}",
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "HERDR_CONFIG_PATH": "relative/is/ignored",
        "COPILOT_HOME": "~/copilot-live,~nosuchuser-sv2/x",
    }
    roots = live_roots(environ, home)
    for expected in (
        home / ".agentibrain",
        home / ".kube",
        home / ".config/herdr",
        home / ".claude",
        tmp_path / "profiles/engineer/claude",
        tmp_path / "codex-a",
        tmp_path / "codex-b",
        tmp_path / "vault",
        tmp_path / "kube/one",
        tmp_path / "kube/two",
        tmp_path / "xdg/herdr",
        Path("~/copilot-live").expanduser(),
    ):
        assert expected.resolve() in roots
    assert not any("relative" in str(root) for root in roots)
    assert len(roots) == len(set(roots))


def test_a_the_suite_write_guard_protects_the_run_live_roots():
    assert installer_isolation.PROTECTED_PATHS == swarm_v2_isolation.LIVE_ROOTS
    real = swarm_v2_isolation.REAL_HOME
    for name in (".agentibrain", ".kube", ".config/herdr", ".claude", ".codex", ".agentihooks"):
        assert (real / name).resolve() in swarm_v2_isolation.LIVE_ROOTS


def test_a_hook_redis_keys_carry_the_run_test_prefix():
    prefix = swarm_v2_isolation.RUN_PREFIX
    assert prefix == f"agentihooks-test-{swarm_v2_isolation.RUN_ID}"
    assert _redis._KEY_PREFIX == memory_store._KEY_PREFIX == event_relay.STREAM_KEY_PREFIX == prefix
    assert os.environ["REDIS_KEY_PREFIX"] == prefix
    assert event_relay._stream_key("c1") == f"{prefix}:events:c1"


def test_a_identities_build_every_root_inside_the_fixture(tmp_path):
    ids = build(tmp_path / "fx", "run1")
    for path in (ids.claude_home, ids.codex_home, ids.brain_home, ids.vault, ids.archive, ids.kubeconfig.parent):
        assert path.is_dir() and path.resolve().is_relative_to(ids.root.resolve())
    assert ids.vault.parent == ids.brain_home
    assert (ids.root / ".fixture-run").read_text() == "run1"
    assert ids.redis_prefix == ids.kube_namespace == "agentihooks-test-run1"
    assert ids.redis_url == f"unix://{ids.root / 'redis.sock'}?db=15"
    assert ids.key("lock", "t1") == "agentihooks-test-run1:lock:t1"
    config = ids.kubeconfig.read_text()
    assert "server: https://127.0.0.1:9" in config
    assert "current-context: agentihooks-test-run1" in config
    assert "namespace: agentihooks-test-run1" in config


def test_a_identities_steer_claude_codex_brain_and_kube_into_the_fixture(tmp_path, monkeypatch):
    ids = build(tmp_path / "fx", "run1")
    for name, value in ids.environ().items():
        monkeypatch.setenv(name, value)
    confine_environ(ids, os.environ)
    assert claude_home().resolve() == ids.claude_home.resolve()
    assert codex_home().resolve() == ids.codex_home.resolve()
    assert _agentibrain_home().resolve() == ids.brain_home.resolve()
    assert sorted(ids.environ()) == [
        "AGENTIBRAIN_HOME",
        "AGENTIHOOKS_SWARM_REDIS_URL",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "KUBECONFIG",
        "REDIS_KEY_PREFIX",
        "VAULT_ROOT",
    ]


def test_a_a_second_independent_fixture_shares_no_root_or_prefix(tmp_path):
    first, second = build(tmp_path / "one"), build(tmp_path / "two")
    assert first.run_id != second.run_id and len(first.run_id) == 12
    assert first.redis_prefix != second.redis_prefix
    assert not set(first.environ().values()) & set(second.environ().values())


def test_a_a_fake_kubectl_and_herdr_under_the_test_directory_run(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    for name in ("kubectl", "herdr"):
        _fake_program(bin_dir, name, f"fake-{name}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    assert (
        subprocess.run(["kubectl", "get", "pods"], capture_output=True, text=True).stdout == "fake-kubectl get pods\n"
    )
    out = subprocess.run("FOO=1 herdr pane list", shell=True, capture_output=True, text=True).stdout
    assert out == "fake-herdr pane list\n"


def test_a_an_unrelated_program_and_a_missing_live_program_are_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert subprocess.run([sys.executable, "-c", "print('herdr')"], capture_output=True, text=True).stdout == "herdr\n"
    with pytest.raises(FileNotFoundError):
        subprocess.run(["kubectl", "version"], capture_output=True)


def test_a_writes_inside_the_fixture_are_allowed(tmp_path):
    ids = build(tmp_path / "fx")
    (ids.claude_home / "settings.json").write_text("{}")
    (ids.vault / "note.md").write_text("x")
    assert (ids.vault / "note.md").read_text() == "x"


def test_a_herdr_is_absent_by_default_and_the_swarm_session_does_not_leak_in():
    from scripts import herdr_host, herdr_setup

    assert herdr_host.binary() is None and herdr_setup.binary() is None
    with pytest.raises(herdr_host.HerdrError, match="^herdr is not installed$"):
        herdr_host._cli(["workspace", "list"], dict(os.environ))
    assert herdr_host.server_running(dict(os.environ)) is False
    inherited = sorted(n for n in os.environ if n.startswith("AGENTIHOOKS_"))
    assert inherited == ["AGENTIHOOKS_SWARM_KEY_PREFIX", "AGENTIHOOKS_SWARM_REDIS_URL"]


# T-SV2-FND-05-B


def test_b_a_symlink_trap_to_a_live_home_aborts_and_counts(tmp_path, rejections):
    ids = build(tmp_path / "fx", "run1")
    assert ids.traps["symlink"].is_symlink()
    with pytest.raises(Failed, match="symlink resolves outside the fixture directory"):
        confine(ids.root, "symlink", ids.traps["symlink"])
    assert rejections() == {"escape": 1}


def test_b_rejections_reach_the_test_report_as_the_package_measurement(tmp_path):
    report = tmp_path / "report.xml"
    selected = [
        "tests/test_swarm_v2_isolation.py::test_b_a_symlink_trap_to_a_live_home_aborts_and_counts",
        "tests/test_swarm_v2_isolation.py::test_a_writes_inside_the_fixture_are_allowed",
    ]
    flags = ["-p", "no:xdist", "-p", "no:randomly", "-p", "no:cacheprovider", f"--junitxml={report}"]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *flags, *selected],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    xml = report.read_text()
    assert xml.count('name="test_live_path_rejections_total"') == 1
    assert '<property name="test_live_path_rejections_total" value="{&quot;escape&quot;: 1}"' in xml


@pytest.mark.parametrize("trap", ["parent", "environ"])
def test_b_a_parent_escape_and_a_live_environment_value_abort(tmp_path, rejections, trap):
    ids = build(tmp_path / "fx", "run1")
    with pytest.raises(Failed, match=f"^{trap} resolves outside the fixture directory {ids.root}: "):
        confine(ids.root, trap, ids.traps[trap])
    assert rejections() == {"escape": 1}


def test_b_an_environment_naming_a_live_home_is_refused(tmp_path, monkeypatch, rejections):
    ids = build(tmp_path / "fx", "run1")
    environ = {**ids.environ(), "CODEX_HOME": str(ids.traps["environ"])}
    with pytest.raises(Failed, match="^CODEX_HOME resolves outside"):
        confine_environ(ids, environ)
    redis_escape = {**ids.environ(), "AGENTIHOOKS_SWARM_REDIS_URL": f"unix://{tmp_path / 'live.sock'}"}
    with pytest.raises(Failed, match="^AGENTIHOOKS_SWARM_REDIS_URL resolves outside"):
        confine_environ(ids, redis_escape)
    with pytest.raises(Failed, match="^REDIS_KEY_PREFIX is not the fixture run prefix agentihooks-test-run1$"):
        confine_environ(ids, {**ids.environ(), "REDIS_KEY_PREFIX": "agentihooks:swarm"})
    assert rejections() == {"escape": 2, "redis": 1}


def test_b_a_write_into_a_live_root_aborts_without_mutating_it(tmp_path_factory, monkeypatch, rejections):
    live = tmp_path_factory.mktemp("live-home")
    (live / "settings.json").write_text("live")
    monkeypatch.setattr(installer_isolation, "PROTECTED_PATHS", (live.resolve(),))
    with pytest.raises(
        Failed, match=f"^refusing installer write outside the test directory: {live / 'settings.json'}$"
    ):
        (live / "settings.json").write_text("clobbered")
    with pytest.raises(Failed):
        (live / "new.json").write_text("{}")
    assert sorted(p.name for p in live.iterdir()) == ["settings.json"]
    assert (live / "settings.json").read_text() == "live"
    assert rejections() == {"write": 2}


@pytest.mark.parametrize("program", sorted(LIVE_PROGRAMS))
def test_b_a_live_program_outside_the_test_directory_aborts(tmp_path_factory, monkeypatch, rejections, program):
    outside = tmp_path_factory.mktemp("live-bin")
    real = _fake_program(outside, program, "LIVE")
    monkeypatch.setenv("PATH", f"{outside}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(Failed, match=f"^refusing a live {program} outside the test directory: {real}$"):
        subprocess.run([program, "apply"], capture_output=True)
    with pytest.raises(Failed, match=f"refusing a live {program}"):
        subprocess.run(f"cd / && {program} delete", shell=True, capture_output=True)
    with pytest.raises(Failed, match=f"refusing a live {program}"):
        subprocess.run([str(real)], capture_output=True)
    assert rejections() == {"program": 3}


@pytest.mark.parametrize(
    "spawn",
    [
        lambda: subprocess.run(["env", "FOO=1", "kubectl", "get"], capture_output=True),
        lambda: subprocess.run(["timeout", "5s", "nohup", "kubectl"], capture_output=True),
        lambda: subprocess.run(["sh", "-e", "-c", "true; exec kubectl get"], capture_output=True),
        lambda: subprocess.run(["bash", "-lc", "echo $(kubectl get)"], capture_output=True),
        lambda: subprocess.run("echo `kubectl get`", shell=True, capture_output=True),
        lambda: os.system("kubectl delete pod x"),
        lambda: subprocess.run(["env", "-u", "HOME", "kubectl"], capture_output=True),
        lambda: subprocess.run(["timeout", "-s", "KILL", "5", "kubectl"], capture_output=True),
        lambda: subprocess.run(["xargs", "-I", "{}", "kubectl", "{}"], input=b"x", capture_output=True),
        lambda: subprocess.run(["sh", "-c", "if true; then kubectl get; fi"], capture_output=True),
        lambda: subprocess.run(["sh", "-c", "! kubectl get"], capture_output=True),
        lambda: subprocess.run(["sh", "-c", "{ kubectl; }"], capture_output=True),
        lambda: subprocess.run(["bash", "-o", "pipefail", "-c", "kubectl get"], capture_output=True),
        lambda: subprocess.run(["env", "FOO=1", "sh", "-c", "echo x && 'kubectl' get"], capture_output=True),
        lambda: subprocess.run(["sh", "-c", "echo kubectl"], capture_output=True),
        lambda: subprocess.run(["sh", "-c", "\\kubectl get"], capture_output=True),
        lambda: subprocess.run(["sh", "-c", 'ku""bectl get'], capture_output=True),
        lambda: subprocess.run(["sh", "-c", "ku''bectl get"], capture_output=True),
        lambda: subprocess.run(["sh", "-c", "${K:-kubectl} get"], capture_output=True),
        lambda: subprocess.run(["sh", "-c", "'kubectl"], capture_output=True),
    ],
    ids=[
        "env",
        "timeout-nohup",
        "shell-flags",
        "substitution",
        "backtick",
        "os-system",
        "env-option",
        "timeout-option",
        "xargs-option",
        "shell-keywords",
        "shell-bang",
        "shell-group",
        "shell-option-value",
        "wrapped-shell-quoted",
        "shell-argument",
        "backslash",
        "double-quote-splice",
        "single-quote-splice",
        "parameter-default",
        "unbalanced-quote",
    ],
)
def test_b_a_wrapped_or_shell_spawned_live_program_aborts(tmp_path_factory, monkeypatch, rejections, spawn):
    outside = tmp_path_factory.mktemp("live-bin")
    _fake_program(outside, "kubectl", "LIVE")
    monkeypatch.setenv("PATH", f"{outside}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(os, "defpath", str(outside))
    with pytest.raises(Failed, match="^refusing a live kubectl outside the test directory: "):
        spawn()
    assert rejections() == {"program": 1}


def test_b_an_environment_without_path_is_checked_on_the_default_exec_path(
    tmp_path, tmp_path_factory, monkeypatch, rejections
):
    fake = _fake_program(tmp_path / "bin", "kubectl", "fake")
    real = _fake_program(tmp_path_factory.mktemp("live-bin"), "kubectl", "LIVE")
    monkeypatch.setenv("PATH", str(fake.parent))
    monkeypatch.setattr(os, "defpath", str(real.parent))
    assert subprocess.run(["kubectl"], capture_output=True, text=True).stdout == "fake\n"
    with pytest.raises(Failed, match=f"^refusing a live kubectl outside the test directory: {real}$"):
        subprocess.run(["kubectl"], env={"HOME": str(tmp_path)}, capture_output=True)
    assert rejections() == {"program": 1}


def test_b_a_live_program_name_as_an_argument_is_left_alone(tmp_path_factory, monkeypatch):
    outside = tmp_path_factory.mktemp("live-bin")
    _fake_program(outside, "kubectl", "LIVE")
    monkeypatch.setenv("PATH", f"{outside}{os.pathsep}{os.environ['PATH']}")
    done = subprocess.run(["echo", "kubectl", "helm"], capture_output=True, text=True)
    assert done.stdout == "kubectl helm\n"


def test_b_a_fake_symlinked_to_a_live_binary_aborts(tmp_path, tmp_path_factory, monkeypatch, rejections):
    real = _fake_program(tmp_path_factory.mktemp("live-bin"), "kubectl", "LIVE")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "kubectl").symlink_to(real)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(Failed, match="refusing a live kubectl"):
        subprocess.run(["kubectl", "get", "pods"], capture_output=True)
    with pytest.raises(Failed, match="refusing a live kubectl"):
        subprocess.run(["true"], executable=str(real), capture_output=True)
    assert rejections() == {"program": 2}


def test_b_a_key_outside_the_run_prefix_is_refused(tmp_path, rejections):
    ids = build(tmp_path / "fx", "run1")
    assert owned(ids, "agentihooks-test-run1:lock:t") == "agentihooks-test-run1:lock:t"
    for key in ("agentihooks:swarm:demo:claim:t1", "agenticore:memory:x", "agentihooks-test-run1"):
        with pytest.raises(Failed, match=f"^key {key} is outside fixture run run1$"):
            owned(ids, key)
    assert rejections() == {"redis": 3}


def test_b_the_guards_are_inert_outside_a_test(monkeypatch, tmp_path_factory):
    real = _fake_program(tmp_path_factory.mktemp("live-bin"), "herdr", "LIVE")
    monkeypatch.setattr(swarm_v2_isolation, "FIXTURE_ROOT", None)
    assert subprocess.run([str(real)], capture_output=True, text=True).stdout == "LIVE\n"


# T-SV2-FND-05-C


def test_c_an_interrupted_run_leaves_its_lock_and_the_next_run_takes_its_own(tmp_path, rejections):
    import fakeredis

    server = fakeredis.FakeServer()
    redis = fakeredis.FakeRedis(server=server, decode_responses=True)
    redis.set("live:swarm:demo:claim:t1", "live-agent")
    first = build(tmp_path / "one", "run1")
    assert acquire(redis, first, "claim", "worker-1", 60_000)
    assert not acquire(redis, first, "claim", "worker-1-retry", 60_000)
    second = build(tmp_path / "two", "run2")
    assert acquire(redis, second, "claim", "worker-2", 60_000)
    assert redis.get("agentihooks-test-run1:lock:claim") == "worker-1"
    assert sweep(redis, second) == ["agentihooks-test-run2:lock:claim", "agentihooks-test-run2:owner"]
    assert sorted(redis.keys()) == [
        "agentihooks-test-run1:lock:claim",
        "agentihooks-test-run1:owner",
        "live:swarm:demo:claim:t1",
    ]
    assert redis.get("live:swarm:demo:claim:t1") == "live-agent"
    assert rejections() == {}


def test_c_a_sweep_needs_the_owning_run_and_replays_without_a_second_effect(tmp_path, rejections):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    first = build(tmp_path / "one", "run1")
    acquire(redis, first, "claim", "worker-1", 60_000)
    forged = replace(first, run_id="run9")
    with pytest.raises(Failed, match="^run run9 does not own the keys under agentihooks-test-run1$"):
        sweep(redis, forged)
    assert redis.get("agentihooks-test-run1:lock:claim") == "worker-1"
    assert sweep(redis, first) == ["agentihooks-test-run1:lock:claim", "agentihooks-test-run1:owner"]
    with pytest.raises(Failed, match="does not own"):
        sweep(redis, first)
    assert redis.keys() == []
    assert rejections() == {"redis": 2}


def test_c_a_stale_fixture_root_is_removed_only_by_its_own_run(tmp_path, tmp_path_factory, rejections):
    live = tmp_path_factory.mktemp("live-home")
    (live / "settings.json").write_text("live")
    stale = build(tmp_path / "stale", "run1")
    (stale.claude_home / "sync.lock").write_text("held")
    (stale.root / "traps" / "live-home").symlink_to(live)
    fresh = build(tmp_path / "fresh", "run2")
    assert not (fresh.claude_home / "sync.lock").exists()
    with pytest.raises(Failed, match=f"^{stale.root.resolve()} does not belong to fixture run run2$"):
        remove(replace(fresh, root=stale.root))
    assert (stale.claude_home / "sync.lock").read_text() == "held"
    remove(stale)
    assert not stale.root.exists()
    assert (live / "settings.json").read_text() == "live"
    with pytest.raises(Failed, match="does not belong"):
        remove(stale)
    assert rejections() == {"cleanup": 2}


def test_c_a_live_root_is_never_removed(tmp_path, monkeypatch, rejections):
    ids = build(tmp_path / "fx", "run1")
    monkeypatch.setattr(swarm_v2_isolation, "LIVE_ROOTS", (tmp_path.resolve(),))
    with pytest.raises(Failed, match=f"^{ids.root.resolve()} is a live root$"):
        remove(ids)
    assert ids.root.is_dir()
    assert rejections() == {"cleanup": 1}
