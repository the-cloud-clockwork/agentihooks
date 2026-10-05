import json
import sys
from pathlib import Path

import pytest

from scripts.doctor import cli as doctor
from scripts.inbox.store import InboxStore
from scripts.swarm import cli as swarm_cli
from scripts.swarm import prompt
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import RedisStore, SwarmError
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeRuntime

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")
WATCHED, DOCTOR = "watch", "watch-doctor"
PR = "https://github.com/o/r/pull/9"


class FileLedger(LedgerClient):
    """The real ledger ops, applied to the ledger files as the server would, without the HTTP hop."""

    def _call(self, slug, ops=None):
        if ops:
            try:
                core.check_body({"ops": ops})
            except ValueError as exc:
                raise SwarmError(f"ledger {slug}: server refused: 400 {exc}") from exc
        state, rejected = core.sync(slug, ops=ops)
        if rejected:
            raise SwarmError(f"ledger {slug} refused: {rejected}")
        return state


@pytest.fixture
def env(monkeypatch, tmp_path):
    import fakeredis

    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    content = {"title": "Watched work", "overview": "o", "phases": [{"title": "One", "description": "d"}]}
    assert new_ledger.create(WATCHED, content)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    rt = FakeRuntime()
    monkeypatch.setattr(swarm_cli, "connect", lambda: store)
    monkeypatch.setattr(swarm_cli, "LedgerClient", FileLedger)
    monkeypatch.setattr(swarm_cli, "HerdrRuntime", lambda: rt)
    monkeypatch.setattr(swarm_cli.timer, "ensure", lambda binary: True)
    monkeypatch.setattr(swarm_cli.delivery, "HerdrMessenger", lambda: FakeHerdr({}))
    monkeypatch.setattr(doctor, "linked_bundle", lambda: tmp_path / "bundle")
    assert swarm_cli.main([WATCHED, "create", "--repo", "/repo"]) == 0
    return store, rt, tmp_path


def state(slug):
    return core.sync(slug)[0]


def pointers(slug):
    return [c for c in state(slug)["chat"] if "Doctor" in c["text"]]


def test_start_refuses_without_a_linked_bundle_and_creates_nothing(env, monkeypatch, capsys):
    store, _, tmp = env
    monkeypatch.setattr(doctor, "linked_bundle", lambda: None)
    assert doctor.main([WATCHED, "start"]) == 1
    assert "linked bundle" in capsys.readouterr().err
    assert DOCTOR not in store.slugs()
    assert not (tmp / f"{DOCTOR}.json").exists()
    assert state(WATCHED)["sources"] == [] and not pointers(WATCHED)


def test_start_links_both_ledgers_starts_the_doctor_and_registers_peer_masters(env):
    store, rt, tmp = env
    assert doctor.main([WATCHED, "start"]) == 0
    assert state(DOCTOR)["sources"] == [str(tmp / f"{WATCHED}.json")]
    assert state(DOCTOR)["size"] == "swarm"
    assert state(WATCHED)["sources"] == [str(tmp / f"{DOCTOR}.json")]
    assert len(pointers(WATCHED)) == 1
    config = store.config(DOCTOR)
    assert (config.template, config.max_eng, config.max_ci) == ("doctor", 1, 0)
    assert config.state in ("running", "drained") and store.agents(DOCTOR)
    assert config.lanes["eng"]["kind"] == "troubleshoot"
    assert all(kind in config.lanes["eng"]["role"] for kind in ("troubleshoot", "tune", "code"))
    assert (store.peer(WATCHED), store.peer(DOCTOR)) == (DOCTOR, WATCHED)
    opening = InboxStore(store.redis).inbox(f"master@{WATCHED}")
    assert [item.sender for item in opening] == [f"master@{DOCTOR}"]
    name, task = rt.masters[-1]
    assert name.startswith(f"{DOCTOR}-master") and task["peer"] == WATCHED
    assert f"master@{WATCHED}" in prompt.build_master(DOCTOR, "/repo", name, task)


def test_a_second_start_adds_no_second_link(env):
    store, _, _ = env
    assert doctor.main([WATCHED, "start"]) == 0
    assert doctor.main([WATCHED, "start"]) == 0
    assert len(state(WATCHED)["sources"]) == len(state(DOCTOR)["sources"]) == 1
    assert len(pointers(WATCHED)) == 1
    assert len(InboxStore(store.redis).inbox(f"master@{WATCHED}")) == 1


def fix(slug):
    add = {"op": "task_add", "id": "a1", "by": "doctor", "task": "d1", "title": "Inbox wake reaches idle panes"}
    proof = {"command": "agentihooks swarm watch status", "output": "unread items over the window 7 before, 0 after"}
    fields = {"state": "done", "pr_url": PR, "proof": proof}
    done = {"op": "task_update", "id": "a2", "by": "doctor", "item": "tasks/d1", "fields": fields}
    FileLedger()._call(slug, [{**add, "lane": "eng", "kind": "tune"}, done])


@pytest.mark.parametrize("named", [WATCHED, DOCTOR])
def test_stop_closes_the_doctor_ledger_with_every_fix_and_removes_its_agents(env, named, capsys):
    store, rt, _ = env
    doctor.main([WATCHED, "start"])
    fix(DOCTOR)
    assert store.agents(DOCTOR)
    assert doctor.main([named, "stop"]) == 0
    closed = state(DOCTOR)
    assert closed["closed_at"]
    assert "Inbox wake reaches idle panes" in closed["overview"]
    assert "unread items over the window 7 before, 0 after" in closed["overview"]
    assert store.agents(DOCTOR) == [] and store.config(DOCTOR).state == "stopped"
    assert (store.peer(WATCHED), store.peer(DOCTOR)) == ("", "")
    assert store.config(WATCHED).state == "paused"


def test_status_names_the_link_and_the_doctor_swarm(env, capsys):
    doctor.main([WATCHED, "start"])
    capsys.readouterr()
    assert doctor.main([WATCHED, "status"]) == 0
    out = capsys.readouterr().out
    assert f"doctor {DOCTOR} watches {WATCHED}" in out
    assert "peers registered" in out and "ledger open" in out
    assert f"{DOCTOR}-master-1" in out


def test_status_without_a_doctor_says_so(env, capsys):
    assert doctor.main([WATCHED, "status"]) == 1
    assert "no Doctor" in capsys.readouterr().err


def test_swarm_status_json_names_the_peer_for_the_page(env, capsys):
    doctor.main([WATCHED, "start"])
    capsys.readouterr()
    swarm_cli.main([WATCHED, "status", "--json"])
    assert json.loads(capsys.readouterr().out)["peer"] == DOCTOR


def test_the_cli_routes_a_crew_verb_to_the_crew_and_leaves_the_hook_doctor_alone(monkeypatch):
    from scripts import install

    calls = []
    monkeypatch.setattr(doctor, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "doctor", WATCHED, "status"])
    with pytest.raises(SystemExit):
        install.main()
    assert calls == [[WATCHED, "status"]]
    assert install._crew_doctor(["doctor", "--json"]) is False
    assert install._crew_doctor(["doctor"]) is False
