import errno
from pathlib import Path

import pytest

from scripts.codex_session import supervisor
from tests.codex_session.fakes import Clock, Server
from tests.codex_session.test_supervisor import SLUG, deps, launch_spec
from tests.codex_session.test_supervisor import root as root
from tests.codex_session.test_supervisor import server as server
from tests.codex_session.test_supervisor import store as store

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


def test_launch_storage_does_not_grow_with_canonical_rollouts(store, server, root, tmp_path):
    profile = Path(server.codex_home)
    sessions = profile / "sessions"
    sessions.mkdir()
    rollout = sessions / "rollout-existing.jsonl"
    rollout.write_bytes(b"x" * 1024 * 1024)
    (profile / "history.jsonl").write_bytes(b"y" * 1024 * 1024)
    (profile / "config.toml").write_text('model = "fixture"\n')
    started = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    home = Path(server.spawns[0]["env"]["CODEX_HOME"])

    assert sum(p.stat().st_size for p in home.rglob("*") if p.is_file()) < 4096
    assert (home / "sessions" / rollout.name).samefile(rollout)
    (home / "sessions" / "rollout-new.jsonl").write_text("new transcript")
    supervisor.stop(started.process)
    supervisor.finish(store, SLUG, started, 5000)

    assert not home.exists()
    assert rollout.stat().st_size == 1024 * 1024
    assert (sessions / "rollout-new.jsonl").read_text() == "new transcript"


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
    for index in range(12):
        server = Server(str(tmp_path / "profile"))
        spec = launch_spec(store, server, tmp_path)
        started = supervisor.launch(store, spec, deps(server, Clock(), root))
        supervisor.stop(started.process)
        supervisor.finish(store, SLUG, started, 5000 + index)
        store.drop_agent(SLUG, started.agent.name, 5000 + index)
        assert list(root.iterdir()) == []


@pytest.mark.parametrize("owner", ["dead", "reused", "live", "foreign", "starting"])
def test_next_launch_reclaims_only_abandoned_local_homes(store, server, root, tmp_path, owner):
    from dataclasses import replace

    from scripts.codex_session.binding import BindingStore
    from tests.codex_session.test_supervisor import other_seat

    first = supervisor.launch(store, launch_spec(store, server, tmp_path), deps(server, Clock(), root))
    directory = Path(first.record.endpoint_ref.removeprefix("unix://")).parent
    record = first.record
    if owner == "foreign":
        record = replace(record, host_id="another-host")
    elif owner == "starting":
        record = replace(record, server_pid=0, server_start_identity=0)
    BindingStore(store).put(SLUG, record)
    successor = Server(server.codex_home)
    if owner == "live":
        successor.starts[first.process.pid] = first.record.server_start_identity
    elif owner == "reused":
        successor.starts[first.process.pid] = first.record.server_start_identity + 1
    spec = other_seat(launch_spec(store, successor, tmp_path))
    started = supervisor.launch(store, spec, deps(successor, Clock(), root))

    assert directory.exists() == (owner in {"live", "foreign", "starting"})
    assert Path(started.record.endpoint_ref.removeprefix("unix://")).exists()
