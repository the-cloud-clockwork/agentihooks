import errno
import json
from pathlib import Path

import pytest

from hooks.context import profile_chain
from scripts.codex_session import supervisor
from scripts.swarm import launch_check
from tests.codex_session.fakes import Clock, Server
from tests.codex_session.test_supervisor import SLUG, deps, launch_spec
from tests.codex_session.test_supervisor import root as root
from tests.codex_session.test_supervisor import server as server
from tests.codex_session.test_supervisor import store as store

pytestmark = pytest.mark.unit


def test_launch_storage_does_not_grow_with_canonical_rollouts(store, server, root, tmp_path, monkeypatch):
    profile = Path(server.codex_home)
    sessions = profile / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout-existing.jsonl"
    rollout.write_bytes(b"x" * 1024 * 1024)
    (profile / "history.jsonl").write_bytes(b"y" * 1024 * 1024)
    (profile / "state_5.sqlite").write_bytes(b"z" * 1024 * 1024)
    (profile / "config.toml").write_text('model = "fixture"\n')
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    home = Path(server.spawns[0]["env"]["CODEX_HOME"])

    assert sum(p.stat().st_size for p in home.rglob("*") if p.is_file()) < 4096
    assert (home / "sessions" / rollout.name).samefile(rollout)
    (home / "sessions" / "rollout-new.jsonl").write_text("new transcript")
    original = Path.rglob

    def scan(path, pattern, **kwargs):
        assert not path.is_symlink(), "cleanup must not scan shared rollouts"
        return original(path, pattern, **kwargs)

    monkeypatch.setattr(Path, "rglob", scan)
    supervisor.stop(started.process)
    supervisor.finish(store, SLUG, started, 5000)

    assert not home.exists()
    assert rollout.stat().st_size == 1024 * 1024
    assert (sessions / "rollout-new.jsonl").read_text() == "new transcript"


@pytest.mark.parametrize("linked_auth", [False, True])
def test_launch_stages_only_startup_files_and_auth_references(store, server, root, tmp_path, linked_auth):
    profile = Path(server.codex_home)
    for name in ("config.toml", "AGENTS.md", "AGENTS.override.md"):
        (profile / name).write_text(name)
    for name in ("rules", "skills"):
        (profile / name).mkdir()
        (profile / name / "nested").mkdir()
        (profile / name / "nested" / "instructions.md").write_text(name)
    auth = tmp_path / "login-reference"
    auth.touch()
    if linked_auth:
        (profile / "auth.json").symlink_to(auth)
    else:
        (profile / "auth.json").touch()
    unrelated = profile / "future-cache"
    unrelated.mkdir()
    (unrelated / "large").write_bytes(b"x" * 1024 * 1024)
    (profile / "unknown-state").write_bytes(b"y" * 1024 * 1024)
    (profile / "unknown-link").symlink_to(unrelated, target_is_directory=True)
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    home = Path(server.spawns[0]["env"]["CODEX_HOME"])

    assert not (home / "future-cache").exists()
    assert not (home / "unknown-state").exists()
    assert not (home / "unknown-link").exists()
    for name in ("config.toml", "AGENTS.md", "AGENTS.override.md"):
        assert (home / name).read_text() == name
    for name in ("rules", "skills"):
        assert (home / name / "nested" / "instructions.md").read_text() == name
    assert (home / "auth.json").is_symlink()
    assert (home / "auth.json").samefile(profile / "auth.json")
    assert (home / "sessions").is_symlink()
    assert sum(p.stat().st_size for p in home.rglob("*") if p.is_file()) < 4096
    supervisor.stop(started.process)
    supervisor.finish(store, SLUG, started, 5000)

    assert not home.parent.exists()
    assert (profile / "auth.json").exists()
    assert (unrelated / "large").stat().st_size == 1024 * 1024


def test_launch_stages_the_render_stamp_the_launch_check_reads(store, server, root, tmp_path):
    profile = Path(server.codex_home)
    stamp = {"render": {"chain": ["anton-base", "package:engineer"], "overlays": ["frontend"]}}
    (profile / launch_check.STAMP).write_text(json.dumps(stamp))
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    home = Path(server.spawns[0]["env"]["CODEX_HOME"])

    assert json.loads((home / launch_check.STAMP).read_text()) == stamp
    assert launch_check.chain(home) == ["anton-base", "package:engineer"]
    assert profile_chain.rendered_overlays(home) == ["frontend"]
    supervisor.stop(started.process)
    supervisor.finish(store, SLUG, started, 5000)


def test_launch_ignores_a_full_user_runtime_disk(store, server, root, tmp_path, monkeypatch):
    full = root.parent / "full-runtime"
    home = root.parent / "user"
    original = Path.mkdir

    def mkdir(path, *args, **kwargs):
        if path == full or full in path.parents:
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "home", lambda: home)
    spec = launch_spec(store, server, tmp_path, environ={"HOME": str(home), "XDG_RUNTIME_DIR": str(full)})
    started = supervisor.launch(store, spec, deps(server, Clock(), None))
    assert full not in Path(started.record.endpoint_ref.removeprefix("unix://")).parents
    supervisor.stop(started.process)
    supervisor.finish(store, SLUG, started, 5000)


def test_repeated_launch_and_finish_leave_no_runtime_homes(store, root, tmp_path):
    previous = ""
    for index in range(12):
        server = Server(str(tmp_path / "profile"))
        spec = launch_spec(store, server, tmp_path, previous_execution_id=previous)
        started = supervisor.launch(store, spec, deps(server, Clock(), root))
        previous = started.record.execution_id
        supervisor.stop(started.process)
        supervisor.finish(store, SLUG, started, 5000 + index)
        store.drop_agent(SLUG, started.agent.name, 5000 + index)
        assert list(root.iterdir()) == []


@pytest.mark.parametrize("owner", ["dead", "reused", "live", "foreign", "starting", "dead_starting"])
def test_next_launch_reclaims_only_abandoned_local_homes(store, server, root, tmp_path, owner):
    from dataclasses import replace

    from scripts.codex_session.binding import BindingStore
    from tests.codex_session.test_supervisor import other_seat

    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    directory = Path(first.record.endpoint_ref.removeprefix("unix://")).parent
    record = replace(first.record, active_turn_id="unfinished-turn")
    if owner == "foreign":
        record = replace(record, host_id="another-host")
    elif owner in {"starting", "dead_starting"}:
        record = replace(record, server_pid=0, server_start_identity=0)
        if owner == "dead_starting":
            record = replace(record, bridge_owner="fixture:worker:987654321")
    BindingStore(store).put(SLUG, record)
    successor = Server(server.codex_home)
    if owner == "live":
        successor.starts[first.process.pid] = first.record.server_start_identity
    elif owner == "reused":
        successor.starts[first.process.pid] = first.record.server_start_identity + 1
    spec = other_seat(launch_spec(store, successor, tmp_path))
    started = supervisor.launch(store, spec, deps(successor, Clock(), root))

    preserved = owner in {"live", "foreign", "starting"}
    assert directory.exists() == preserved
    current = BindingStore(store).get(SLUG, record.execution_id)
    assert (current.state, current.active_turn_id) == (
        (record.state, "unfinished-turn") if preserved else ("exited", "")
    )
    if not preserved:
        assert not store.redis.hexists(store.key(SLUG, "codex-endpoints"), record.endpoint_ref)
    assert Path(started.record.endpoint_ref.removeprefix("unix://")).exists()


@pytest.mark.parametrize("action", ["launch", "finish"])
def test_concurrent_cleanup_does_not_fail_launch_or_finish(store, server, root, tmp_path, monkeypatch, action):
    import shutil

    from tests.codex_session.test_supervisor import other_seat

    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    directory = Path(first.record.endpoint_ref.removeprefix("unix://")).parent
    original = shutil.rmtree

    def removed_concurrently(path, *args, **kwargs):
        if path == directory:
            original(path)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "rmtree", removed_concurrently)
        if action == "finish":
            supervisor.stop(first.process)
            supervisor.finish(store, SLUG, first, 5000)
        else:
            successor = Server(server.codex_home)
            spec = other_seat(launch_spec(store, successor, tmp_path))
            supervisor.launch(store, spec, deps(successor, Clock(), root))
    assert not directory.exists()


@pytest.mark.parametrize("action", ["launch", "finish"])
@pytest.mark.parametrize("config_mode", ["explicit", "missing", "private"])
@pytest.mark.parametrize(
    "snapshot",
    [
        ("base\nnew legacy turn\n", "base\n", "base\nnew legacy turn\n"),
        ("base\n", "base\nnew canonical turn\n", "base\nnew canonical turn\n"),
        ("branch a\n", "branch b\n", "branch b\n"),
    ],
)
def test_cleanup_preserves_unique_legacy_rollouts(store, server, root, tmp_path, action, snapshot, config_mode):
    from unittest.mock import patch

    from tests.codex_session.test_supervisor import other_seat

    legacy_text, canonical_text, expected = snapshot
    canonical = tmp_path / ("custom-canonical" if config_mode == "explicit" else ".codex")
    canonical.mkdir()
    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    home = Path(server.spawns[0]["env"]["CODEX_HOME"])
    if config_mode == "explicit":
        (home / "config.toml").write_text(f'sqlite_home = "{canonical}"\n')
    elif config_mode == "private":
        (home / "config.toml").write_text(f'sqlite_home = "{home}"\n')
    else:
        (home / "config.toml").unlink(missing_ok=True)
    for name in ("sessions", "archived_sessions"):
        (home / name).unlink()
        (home / name / "new" / "date").mkdir(parents=True)
        (home / name / "new" / "date" / "unique.jsonl").write_text("unique legacy transcript")
        (home / name / "existing" / "date").mkdir(parents=True)
        (canonical / name / "existing" / "date").mkdir(parents=True)
        for filename in ("shared.jsonl", "other.jsonl"):
            (home / name / "existing" / "date" / filename).write_text(legacy_text)
            (canonical / name / "existing" / "date" / filename).write_text(canonical_text)
    with patch.object(Path, "home", return_value=tmp_path):
        if action == "finish":
            supervisor.stop(first.process)
            supervisor.finish(store, SLUG, first, 5000)
        else:
            successor = Server(server.codex_home)
            spec = other_seat(launch_spec(store, successor, tmp_path))
            supervisor.launch(store, spec, deps(successor, Clock(), root))
    assert not home.exists()
    for name in ("sessions", "archived_sessions"):
        assert (canonical / name / "new" / "date" / "unique.jsonl").read_text() == "unique legacy transcript"
        assert (canonical / name / "existing" / "date" / "shared.jsonl").read_text() == expected
        assert (canonical / name / "existing" / "date" / "other.jsonl").read_text() == expected
        if legacy_text == "branch a\n":
            assert sorted(p.read_text() for p in (canonical / name).rglob("shared.jsonl")) == [
                "branch a\n",
                "branch b\n",
            ]


def test_launch_creates_nested_canonical_rollouts_from_dangling_profile_links(store, server, root, tmp_path):
    profile = Path(server.codex_home)
    canonical = tmp_path / "shared" / "nested"
    for name in ("sessions", "archived_sessions"):
        (profile / name).symlink_to(canonical / name)
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    home = Path(server.spawns[0]["env"]["CODEX_HOME"])
    for name in ("sessions", "archived_sessions"):
        assert (home / name).resolve() == canonical / name
        (home / name / "new.jsonl").write_text("durable transcript")
    supervisor.stop(started.process)
    supervisor.finish(store, SLUG, started, 5000)
    for name in ("sessions", "archived_sessions"):
        assert (canonical / name / "new.jsonl").read_text() == "durable transcript"


def test_a_launch_skips_transport_paths_outside_the_execution_layout(store, server, root, tmp_path):
    import socket

    from scripts.codex_session.binding import BindingStore, TransportRecord

    protected = tmp_path / "unrelated"
    protected.mkdir()
    marker = protected / "keep"
    marker.write_text("unrelated data")
    record = TransportRecord(
        "exe-old", 1, "eng-old@fixture", "old", "old", f"unix://{protected}/socket", host_id=socket.gethostname()
    )
    BindingStore(store).reserve(SLUG, record)
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    assert marker.read_text() == "unrelated data"
    assert Path(started.record.endpoint_ref.removeprefix("unix://")).exists()


def test_a_launch_reclaims_dead_homes_after_live_foreign_and_missing_entries(store, server, root, tmp_path):
    import shutil
    from dataclasses import replace

    from scripts.codex_session.binding import BindingStore

    records = []
    for index in range(1, 5):
        spec = launch_spec(store, server, tmp_path)
        spec = replace(spec, agent=replace(spec.agent, seat=f"eng-{index}@{SLUG}"))
        records.append(supervisor.launch(store, spec, deps(server, Clock(), root)).record)
    live, foreign, missing, dead = records
    BindingStore(store).put(SLUG, replace(foreign, host_id="another-host"))
    directories = [Path(record.endpoint_ref.removeprefix("unix://")).parent for record in records]
    shutil.rmtree(directories[2])
    successor = Server(server.codex_home)
    successor.starts[live.server_pid] = live.server_start_identity
    spec = launch_spec(store, successor, tmp_path)
    spec = replace(spec, agent=replace(spec.agent, seat=f"eng-5@{SLUG}"))
    supervisor.launch(store, spec, deps(successor, Clock(), root))
    assert [directory.exists() for directory in directories] == [True, True, False, False]
    assert BindingStore(store).get(SLUG, dead.execution_id).state == "exited"
