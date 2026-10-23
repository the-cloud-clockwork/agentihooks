import os
import re
import subprocess
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.doctor import interventions
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import server_code

WATCHED, DOCTOR = "watch", "watch-doctor"
ENGINEER = f"{WATCHED}-eng-1"


class FakeRun:
    def __init__(self, outputs=None, failing=()):
        self.calls, self.outputs, self.failing = [], outputs or {}, failing

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        code = 1 if any(word in argv for word in self.failing) else 0
        out = next((text for word, text in self.outputs.items() if word in argv), "")
        return subprocess.CompletedProcess(argv, code, out, "refused" if code else "")


class FakeLedger:
    def __init__(self, rows=()):
        self.said, self.noted, self.rows, self.updates = [], [], {slug: list(rows) for slug in (DOCTOR,)}, []

    def say(self, slug, text, by=None):
        self.said.append((slug, text, by))

    def notify(self, slug, text):
        self.noted.append((slug, text))

    def tasks(self, slug):
        return self.rows.get(slug, [])

    def update_task(self, slug, task_id, fields, by="swarm"):
        self.updates.append((slug, task_id, fields, by))


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(WATCHED, "/repo", 2, 1))
    found.create(SwarmConfig(DOCTOR, "/repo", 1, 0, template="doctor"))
    found.put_agent(WATCHED, AgentRecord(ENGINEER, "eng", "t1", seat=f"eng-1@{WATCHED}"))
    return found


def context(store, run=None, **paths):
    return interventions.Context(store, FakeLedger(), WATCHED, DOCTOR, run or FakeRun({"rev-parse": "dev\n"}), **paths)


def args(to="", text="", file=""):
    return Namespace(to=to, text=text, file=file)


def logged_on_both(ctx):
    told = [i.text for i in InboxStore(ctx.store.redis).inbox(f"master@{WATCHED}")]
    noted = sorted(slug for slug, _ in ctx.ledger.noted)
    return ctx.ledger.said == [] and noted == sorted([WATCHED, DOCTOR]) and told == [ctx.ledger.noted[0][1]]


@pytest.mark.parametrize("action", ["task-set", "task-reopen", "retire", "kill", "terminate-agent", "release", "close"])
def test_the_allow_list_refuses_every_other_action_and_touches_nothing(store, action):
    ctx = context(store)
    with pytest.raises(SwarmError, match="not an allowed intervention") as refused:
        interventions.apply(ctx, action, args(to=ENGINEER, text="x"))
    assert "tasks" in str(refused.value) and "kills" in str(refused.value)
    assert ctx.run.calls == [] and ctx.ledger.noted == []
    assert InboxStore(store.redis).inbox(f"eng-1@{WATCHED}") == []
    assert [a.name for a in store.agents(WATCHED)] == [ENGINEER]


def test_pull_dev_fast_forwards_the_main_checkout_and_logs_on_both_ledgers(store):
    ctx = context(store)
    interventions.apply(ctx, "pull-dev", args())
    assert ctx.run.calls[-2:] == [
        ["git", "-C", "/repo", "fetch", "origin", "dev"],
        ["git", "-C", "/repo", "merge", "--ff-only", "origin/dev"],
    ]
    assert logged_on_both(ctx)


def test_pull_dev_refuses_a_checkout_that_is_not_on_dev(store):
    ctx = context(store, FakeRun({"rev-parse": "feature\n"}))
    with pytest.raises(SwarmError, match="not dev"):
        interventions.apply(ctx, "pull-dev", args())
    assert not any("fetch" in call or "merge" in call for call in ctx.run.calls) and ctx.ledger.noted == []


def test_pull_dev_records_the_applied_time_of_each_merged_fix_an_open_tune_task_measures(store):
    rows = [
        {"id": "m", "kind": "tune", "state": "open", "depends_on": ["a", "b", "c", "d", "e", "g", "h", "ghost"]},
        {"id": "done-tune", "kind": "tune", "state": "done", "depends_on": ["f"]},
        {"id": "a", "state": "done"},
        {"id": "b", "kind": "ci", "state": "done"},
        {"id": "c", "kind": "code", "state": "pr"},
        {"id": "d", "kind": "code", "state": "done", "applied_at": "2026-10-17T01:00:00Z"},
        {"id": "e", "kind": "research", "state": "done"},
        {"id": "f", "kind": "code", "state": "done"},
        {"id": "g", "kind": "code", "state": "done", "branch_repo": "git@github.com:O/bundle.git"},
        {"id": "h", "kind": "code", "state": "done", "branch_repo": "https://github.com/o/r"},
    ]
    run = FakeRun({"rev-parse": "dev\n", "get-url": "git@github.com:O/r.git\n"})
    ledger = FakeLedger(rows)
    read = ledger.tasks
    ledger.tasks = lambda slug: run.calls.append(["ledger-read", slug]) or read(slug)
    ctx = interventions.Context(store, ledger, WATCHED, DOCTOR, run)
    line = interventions.apply(ctx, "pull-dev", args())
    stamped = [(slug, task, by) for slug, task, _, by in ctx.ledger.updates]
    assert stamped == [(DOCTOR, t, f"master@{DOCTOR}") for t in ("a", "b", "h")]
    times = {fields["applied_at"] for *_, fields, _ in ctx.ledger.updates}
    assert len(times) == 1 and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", times.pop())
    pull = ["git", "-C", "/repo", "fetch", "origin", "dev"]
    assert run.calls.index(["ledger-read", DOCTOR]) < run.calls.index(pull)
    assert ["git", "-C", "/repo", "remote", "get-url", "origin"] in run.calls
    assert (
        line == "The Doctor pulled dev into the main checkout of the watched swarm and recorded 3 merged fixes applied."
    )
    assert logged_on_both(ctx)


def test_pull_dev_with_no_unapplied_fix_records_nothing(store):
    ctx = context(store)
    line = interventions.apply(ctx, "pull-dev", args())
    assert ctx.ledger.updates == []
    assert line == "The Doctor pulled dev into the main checkout of the watched swarm."


class SourceRun(FakeRun):
    def __init__(self, before, after):
        super().__init__({"rev-parse": "dev\n"})
        self.text, self.after = before, after

    def __call__(self, argv, **kwargs):
        done = super().__call__(argv, **kwargs)
        if "merge" in argv:
            self.text = self.after
        return subprocess.CompletedProcess(argv, 0, self.text, "") if "show" in argv else done


def test_pull_dev_of_the_tool_source_installs_a_newly_declared_dependency(store):
    before = '[project]\ndependencies = ["redis>=5"]\n'
    run = SourceRun(before, before.replace('"redis>=5"', '"redis>=5", "croniter>=6.2.4,<7", "rich"'))
    ctx = context(store, run, source=Path("/repo"), python="/tool/python")
    line = interventions.apply(ctx, "pull-dev", args())
    assert run.calls[-1] == ["uv", "pip", "install", "--python", "/tool/python", "croniter>=6.2.4,<7", "rich"]
    assert line == (
        "The Doctor pulled dev into the main checkout of the watched swarm. "
        "It installed croniter>=6.2.4,<7, rich into the agentihooks tool environment."
    )
    assert logged_on_both(ctx)


def test_pull_dev_of_another_repository_installs_nothing(store):
    run = SourceRun("", '[project]\ndependencies = ["croniter"]\n')
    ctx = context(store, run, source=Path("/elsewhere"), python="/tool/python")
    interventions.apply(ctx, "pull-dev", args())
    assert not any("show" in call or "install" in call for call in run.calls)


def test_record_applied_stamps_utc_whatever_the_local_zone(store, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 18, 12, tzinfo=timezone.utc).astimezone(tz) if tz else datetime(2026, 10, 18, 21)

    monkeypatch.setattr(interventions, "datetime", Clock)
    ctx = context(store)
    assert interventions.record_applied(ctx, ["a"]) == ["a"]
    assert ctx.ledger.updates == [(DOCTOR, "a", {"applied_at": "2026-10-18T12:00:00Z"}, f"master@{DOCTOR}")]


def test_a_failed_command_is_reported_and_not_logged(store):
    ctx = context(store, FakeRun({"rev-parse": "dev\n"}, failing=("pull",)))
    with pytest.raises(SwarmError, match="refused"):
        interventions.apply(ctx, "pull-dev", args())
    assert ctx.ledger.noted == []


REFUSED = "the ledger server already runs its current code; the Doctor restarts it only after a change"


def server_tree(tmp_path):
    files = {
        "scripts/__init__.py": "",
        "scripts/swarm/__init__.py": "",
        "scripts/swarm/naming.py": "",
        "scripts/swarm/operator_mail.py": "import json\nfrom . import relay\n",
        "scripts/swarm/relay.py": "",
        "scripts/swarm/unused.py": "",
        "scripts/swarm_ledger/ledger_core.py": "from . import ledger_gate\nfrom ..swarm import naming\nfrom .. import swarm\n",
        "scripts/swarm_ledger/ledger_gate.py": "",
        "scripts/swarm_ledger/ledger_server.py": (
            "import os\nimport ledger_core\n\n\ndef relay():\n    from scripts.swarm import operator_mail\n"
        ),
        "scripts/swarm_ledger/shell.html": "<html></html>",
        "scripts/swarm_ledger/__pycache__/ledger_core.cpython-312.pyc": "",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
        os.utime(tmp_path / name, (100, 100))
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("1")
    os.utime(pidfile, (200, 200))
    server_code.record(tmp_path, 1, 200_000_000_000)
    return tmp_path / "scripts" / "swarm_ledger", pidfile


def process_tree(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "1").mkdir(parents=True)
    (proc / "stat").write_text("btime 100\n")
    tail = ["0"] * 20
    tail[19] = str(100 * os.sysconf("SC_CLK_TCK"))
    (proc / "1" / "stat").write_text("1 (ledger server) " + " ".join(tail))
    monkeypatch.setattr(interventions, "PROC", proc, raising=False)


def restarted(changed):
    return f"The Doctor restarted the ledger server on its new code: {changed}."


def test_the_ledger_server_restarts_only_when_its_code_changed(store, tmp_path):
    code, pidfile = server_tree(tmp_path)
    ctx = context(store, code=code, pidfile=pidfile)
    with pytest.raises(SwarmError) as refused:
        interventions.apply(ctx, "restart-ledger-server", args())
    assert str(refused.value) == REFUSED and ctx.run.calls == []
    os.utime(code / "ledger_server.py", (300, 300))
    os.utime(code / "ledger_gate.py", (300, 300))
    line = interventions.apply(ctx, "restart-ledger-server", args())
    assert [call[-1] for call in ctx.run.calls] == ["--stop", "--ensure"]
    assert line == restarted(
        "scripts/swarm_ledger/ledger_gate.py, scripts/swarm_ledger/ledger_server.py changed since it started"
    )
    assert logged_on_both(ctx)


@pytest.mark.parametrize("recorded", [True, False])
def test_rewritten_pidfile_does_not_hide_code_newer_than_the_process(store, tmp_path, monkeypatch, recorded):
    code, pidfile = server_tree(tmp_path)
    process_tree(tmp_path, monkeypatch)
    if not recorded:
        (tmp_path / server_code.RECORD).unlink()
    os.utime(code / "ledger_server.py", (300, 300))
    pidfile.write_text("1")
    os.utime(pidfile, (400, 400))
    ctx = context(store, code=code, pidfile=pidfile)

    line = interventions.apply(ctx, "restart-ledger-server", args())

    assert [call[-1] for call in ctx.run.calls] == ["--stop", "--ensure"]
    assert line == restarted("scripts/swarm_ledger/ledger_server.py changed since it started")
    assert logged_on_both(ctx)


def test_current_loaded_code_refuses_restart_after_same_process_reload(store, tmp_path, monkeypatch):
    code, pidfile = server_tree(tmp_path)
    process_tree(tmp_path, monkeypatch)
    os.utime(code / "ledger_server.py", (300, 300))
    server_code.record(tmp_path, 1, 300_000_000_000)
    pidfile.write_text("1")
    os.utime(pidfile, (400, 400))
    ctx = context(store, code=code, pidfile=pidfile)

    with pytest.raises(SwarmError, match=REFUSED):
        interventions.apply(ctx, "restart-ledger-server", args())

    assert ctx.run.calls == [] and ctx.ledger.noted == []


@pytest.mark.parametrize("recorded_pid", [None, 2])
def test_legacy_process_refuses_restart_when_imported_code_is_unchanged(store, tmp_path, monkeypatch, recorded_pid):
    code, pidfile = server_tree(tmp_path)
    process_tree(tmp_path, monkeypatch)
    if recorded_pid is None:
        (tmp_path / server_code.RECORD).unlink()
    else:
        server_code.record(tmp_path, recorded_pid, 0)
    os.utime(pidfile, (400, 400))
    os.utime(code / "ledger_server.py", (200, 200))
    ctx = context(store, code=code, pidfile=pidfile)

    with pytest.raises(SwarmError, match=REFUSED):
        interventions.apply(ctx, "restart-ledger-server", args())

    assert ctx.run.calls == [] and ctx.ledger.noted == []


def test_missing_process_start_is_reported_without_restarting(store, tmp_path, monkeypatch):
    code, pidfile = server_tree(tmp_path)
    process_tree(tmp_path, monkeypatch)
    (tmp_path / server_code.RECORD).unlink()
    (tmp_path / "proc" / "1" / "stat").unlink()
    ctx = context(store, code=code, pidfile=pidfile)

    with pytest.raises(SwarmError, match="^cannot read the ledger server process start$"):
        interventions.apply(ctx, "restart-ledger-server", args())

    assert ctx.run.calls == [] and ctx.ledger.noted == []


@pytest.mark.parametrize("newer", [False, True])
def test_legacy_restart_preserves_nanosecond_process_start_boundary(store, tmp_path, monkeypatch, newer):
    monkeypatch.setattr(interventions.os, "sysconf", lambda _: 100)
    code, pidfile = server_tree(tmp_path)
    process_tree(tmp_path, monkeypatch)
    (tmp_path / server_code.RECORD).unlink()
    (tmp_path / "proc" / "stat").write_text("btime 1791800000\n")
    tail = ["0"] * 20
    tail[19] = "10001"
    (tmp_path / "proc" / "1" / "stat").write_text("1 (ledger server) " + " ".join(tail))
    stamp = 1_791_800_100_010_000_001 if newer else 1_791_800_100_010_000_000
    os.utime(code / "ledger_server.py", ns=(stamp, stamp))
    ctx = context(store, code=code, pidfile=pidfile)

    if newer:
        line = interventions.apply(ctx, "restart-ledger-server", args())
        assert [call[-1] for call in ctx.run.calls] == ["--stop", "--ensure"]
        assert line == restarted("scripts/swarm_ledger/ledger_server.py changed since it started")
        assert logged_on_both(ctx)
    else:
        with pytest.raises(SwarmError, match=REFUSED):
            interventions.apply(ctx, "restart-ledger-server", args())
        assert ctx.run.calls == [] and ctx.ledger.noted == []


@pytest.mark.parametrize(
    "changed",
    [
        "scripts/swarm/operator_mail.py",
        "scripts/swarm/relay.py",
        "scripts/swarm/naming.py",
        "scripts/swarm/__init__.py",
        "scripts/swarm_ledger/shell.html",
    ],
)
def test_a_change_to_any_module_the_server_imports_allows_the_restart_and_names_it(store, tmp_path, changed):
    code, pidfile = server_tree(tmp_path)
    os.utime(tmp_path / changed, (300, 300))
    ctx = context(store, code=code, pidfile=pidfile)
    line = interventions.apply(ctx, "restart-ledger-server", args())
    assert [call[-1] for call in ctx.run.calls] == ["--stop", "--ensure"]
    assert line == restarted(f"{changed} changed since it started") and logged_on_both(ctx)


@pytest.mark.parametrize(
    ("touched", "when"),
    [
        ("scripts/swarm/unused.py", 300),
        ("scripts/swarm_ledger/__pycache__/ledger_core.cpython-312.pyc", 300),
        ("scripts/swarm/operator_mail.py", 200),
    ],
)
def test_no_change_since_the_server_started_still_refuses(store, tmp_path, touched, when):
    code, pidfile = server_tree(tmp_path)
    os.utime(tmp_path / touched, (when, when))
    ctx = context(store, code=code, pidfile=pidfile)
    with pytest.raises(SwarmError) as refused:
        interventions.apply(ctx, "restart-ledger-server", args())
    assert str(refused.value) == REFUSED and ctx.run.calls == [] and ctx.ledger.noted == []


def test_a_server_without_a_pidfile_is_restarted(store, tmp_path):
    code, pidfile = server_tree(tmp_path)
    pidfile.unlink()
    ctx = context(store, code=code, pidfile=pidfile)
    line = interventions.apply(ctx, "restart-ledger-server", args())
    assert [call[-1] for call in ctx.run.calls] == ["--stop", "--ensure"]
    assert line == restarted("no running server recorded its start") and logged_on_both(ctx)


def test_refresh_rules_runs_the_rules_refresh(store):
    ctx = context(store)
    interventions.apply(ctx, "refresh-rules", args())
    assert ctx.run.calls == [["agentihooks", "refresh-rules"]] and logged_on_both(ctx)


def test_culture_replaces_the_watched_swarm_culture(store, tmp_path):
    (tmp_path / "culture.md").write_text("Prove every fix.\n")
    ctx = context(store)
    interventions.apply(ctx, "culture", args(file=str(tmp_path / "culture.md")))
    assert store.culture.get(WATCHED) == "Prove every fix.\n" and logged_on_both(ctx)


def test_handoff_at_stop_asks_a_live_agent_of_the_watched_swarm(store):
    ctx = context(store)
    interventions.apply(ctx, "handoff-at-stop", args(to=ENGINEER))
    [item] = InboxStore(store.redis).inbox(f"eng-1@{WATCHED}")
    assert item.sender == f"master@{DOCTOR}" and "next stop" in item.text and f"swarm {WATCHED} handoff" in item.text
    assert logged_on_both(ctx)
    with pytest.raises(SwarmError, match="no agent"):
        interventions.apply(ctx, "handoff-at-stop", args(to=f"{WATCHED}-eng-9"))


def test_message_reaches_the_watched_master_or_an_agent_and_nobody_else(store):
    ctx = context(store)
    interventions.apply(ctx, "message", args(to=f"master@{WATCHED}", text="The fix is merged."))
    item, told = InboxStore(store.redis).inbox(f"master@{WATCHED}")
    assert item.sender == f"master@{DOCTOR}" and item.text == "The fix is merged."
    assert told.text == "The Doctor sent a message to the watched swarm's master."
    interventions.apply(ctx, "message", args(to=ENGINEER, text="Pull dev before your next commit."))
    assert len(ctx.ledger.noted) == 4
    for outside in ("master@other", "operator", "other-eng-1"):
        with pytest.raises(SwarmError, match="watched swarm"):
            interventions.apply(ctx, "message", args(to=outside, text="x"))
    with pytest.raises(SwarmError, match="text"):
        interventions.apply(ctx, "message", args(to=f"master@{WATCHED}"))


@pytest.mark.parametrize("seat", [f"master@{WATCHED}", ""])
def test_the_intervention_note_reaches_the_live_master_by_seat_or_else_by_name(store, seat):
    store.put_agent(WATCHED, AgentRecord(f"{WATCHED}-master-1", "master", "master", seat=seat))
    ctx = context(store)
    line = interventions.apply(ctx, "message", args(to=ENGINEER, text="Pull dev before your next commit."))
    inbox = InboxStore(store.redis)
    assert [i.text for i in inbox.inbox(seat or f"{WATCHED}-master-1")] == [line]
    assert inbox.inbox(f"{WATCHED}-master-1" if seat else f"master@{WATCHED}") == []
