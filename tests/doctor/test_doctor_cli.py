import json
import sys
from pathlib import Path

import pytest

from scripts.doctor import cli as doctor
from scripts.inbox.store import InboxStore
from scripts.swarm import cli as swarm_cli
from scripts.swarm import prompt
from scripts.swarm.health.findings import Finding
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.status import status_report
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

    def _resource(self, slug, path, collection=False):
        from scripts.swarm_ledger.api.resources import value

        return value(self._call(slug), path)


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
    monkeypatch.setattr(doctor.detect, "readers", lambda *args, **kwargs: {})
    assert swarm_cli.main([WATCHED, "create", "--repo", "/repo"]) == 0
    return store, rt, tmp_path


def state(slug):
    return core.sync(slug)[0]


def pointers(slug):
    return [c for c in state(slug)["chat"] if c["text"] == doctor.POINTER]


def test_start_refuses_without_a_linked_bundle_and_creates_nothing(env, monkeypatch, capsys):
    store, _, tmp = env
    monkeypatch.setattr(doctor, "linked_bundle", lambda: None)
    assert doctor.main([WATCHED, "start"]) == 1
    assert "linked bundle" in capsys.readouterr().err
    assert DOCTOR not in store.slugs()
    assert not (tmp / f"{DOCTOR}.json").exists()
    assert state(WATCHED)["sources"] == [] and not pointers(WATCHED)


def test_an_engineer_cannot_start_a_doctor_on_the_shared_ledger_folder(env, monkeypatch, capsys):
    from scripts.swarm_ledger import ledger_creator

    store, _, _ = env
    shared = Path.home() / "development-ledger"
    monkeypatch.setattr(core, "LEDGER_DIR", shared)
    monkeypatch.setenv("LEDGER_DIR", str(shared))
    assert new_ledger.create(WATCHED, {"title": "Watched work", "overview": "o", "phases": [{"title": "One"}]})
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "other")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", "eng")
    assert doctor.main([WATCHED, "start"]) == 1
    assert capsys.readouterr().err.strip() == f"doctor: {ledger_creator.CALLER}"
    assert DOCTOR not in store.slugs()
    assert not (shared / f"{DOCTOR}.json").exists()


def test_a_proof_doctor_on_a_scratch_folder_needs_its_own_redis(env, monkeypatch, capsys):
    from scripts.swarm_ledger import ledger_creator

    store, _, tmp = env
    monkeypatch.delenv("AGENTIHOOKS_SWARM_REDIS_URL")
    assert doctor.main([WATCHED, "start"]) == 1
    assert capsys.readouterr().err.strip() == f"doctor: {ledger_creator.REDIS}"
    assert DOCTOR not in store.slugs()
    assert not (tmp / f"{DOCTOR}.json").exists()


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
    opening = [i for i in InboxStore(store.redis).inbox(f"master@{WATCHED}") if not i.fyi]
    assert [item.sender for item in opening] == [f"master@{DOCTOR}"]
    name, task = rt.masters[-1]
    assert store.names.slug_of(name) == DOCTOR and task["peer"] == WATCHED
    assert f"master@{WATCHED}" in prompt.build_master(DOCTOR, "/repo", name, task)


def test_a_second_start_adds_no_second_link(env):
    store, _, _ = env
    assert doctor.main([WATCHED, "start"]) == 0
    assert doctor.main([WATCHED, "start"]) == 0
    assert len(state(WATCHED)["sources"]) == len(state(DOCTOR)["sources"]) == 1
    assert len(pointers(WATCHED)) == 1
    assert len([i for i in InboxStore(store.redis).inbox(f"master@{WATCHED}") if not i.fyi]) == 1


LONG = "a" * 40 + "-2026-10"


def test_start_on_a_ledger_name_at_the_swarm_name_limit_builds_a_valid_unique_doctor_name(env, capsys):
    store, rt, tmp = env
    twin = LONG[:-1] + "1"
    for slug in (LONG, twin):
        assert new_ledger.create(
            slug, {"title": "Long", "overview": "o", "phases": [{"title": "One", "description": "d"}]}
        )
        assert swarm_cli.main([slug, "create", "--repo", "/repo"]) == 0
    assert len(LONG) == 48
    assert doctor.main([LONG, "start"]) == 0
    assert doctor.main([twin, "start"]) == 0
    doctors = {store.peer(LONG), store.peer(twin)}
    assert len(doctors) == 2 and doctors == {s for s in store.slugs() if store.config(s).template == "doctor"}
    assert all(swarm_cli.SLUG_RE.match(slug) and slug.endswith("-doctor") for slug in doctors)
    long_doctor = store.peer(LONG)
    assert store.peer(long_doctor) == LONG
    assert state(long_doctor)["sources"] == [str(tmp / f"{LONG}.json")]
    assert state(LONG)["sources"] == [str(tmp / f"{long_doctor}.json")]
    [(name, task)] = [(n, t) for n, t in rt.masters if store.names.slug_of(n) == long_doctor]
    assert f"agentihooks doctor {LONG} verdict" in prompt.build_master(long_doctor, "/repo", name, task)
    capsys.readouterr()
    assert doctor.main([long_doctor, "status"]) == 0
    assert f"doctor {long_doctor} watches {LONG}" in capsys.readouterr().out
    assert doctor.main([long_doctor, "stop"]) == 0
    assert state(long_doctor)["closed_at"]
    assert doctor.main([long_doctor, "status"]) == 0
    assert f"doctor {long_doctor} watches {LONG}" in capsys.readouterr().out


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


def test_stop_summary_includes_before_and_after_from_fix_proof_files(env):
    _, _, root = env
    doctor.main([WATCHED, "start"])
    records = [
        (
            "pending",
            "Close items for exited agents",
            "2026-10-05T17:44:10Z before: inbox lists 2 pending\n2026-10-05T17:54:57Z after: inbox lists 0 pending (before 2)",
        ),
        ("delivered", "Settle delivered items", "before 4 [('item', 'delivered')]\nafter 0"),
        (
            "fyi",
            "Skip informational items",
            "measure on recorded inputs: before 11, after 0; control work item still flagged: True",
        ),
    ]
    for task, title, numbers in records:
        workspace = root / task
        workspace.mkdir()
        (workspace / "proof.md").write_text(f"Tests passed\n{numbers}\nReview closed\n")
        FileLedger()._call(
            DOCTOR,
            [
                {
                    "op": "task_add",
                    "id": f"add-{task}",
                    "by": "doctor",
                    "task": task,
                    "title": title,
                    "lane": "eng",
                    "kind": "code",
                },
                {
                    "op": "task_update",
                    "id": f"done-{task}",
                    "by": "doctor",
                    "item": f"tasks/{task}",
                    "fields": {
                        "state": "done",
                        "pr_url": PR,
                        "workspace": str(workspace),
                        "proof": {"output": "Regression tests passed"},
                    },
                },
            ],
        )
    assert doctor.main([WATCHED, "stop"]) == 0
    summary = state(DOCTOR)["overview"]
    for _, title, numbers in records:
        assert title in summary
        for line in numbers.splitlines():
            assert line in summary
    assert "no number recorded" not in summary


@pytest.mark.parametrize("content", [None, "", "Tests passed; review closed"])
def test_summary_reports_missing_numbers_only_without_measurement_evidence(tmp_path, content):
    if content is not None:
        (tmp_path / "proof.md").write_text(content)
    tasks = [
        {"title": "Missing", "state": "done", "workspace": str(tmp_path)},
        {"title": "Ledger", "state": "done", "workspace": str(tmp_path), "proof": {"output": "before 3, after 0"}},
        {"title": "Legacy", "state": "done", "proof": {"output": "before 5, after 0"}},
    ]
    summary = doctor.fixes_note(tasks)
    assert "Missing: moved no number recorded" in summary
    assert "Ledger: moved before 3, after 0" in summary
    assert "Legacy: moved before 5, after 0" in summary
    assert summary.count("no number recorded") == 1


def test_stop_cancels_the_pending_peer_messages_and_tells_the_watched_master(env):
    store, _, _ = env
    doctor.main([WATCHED, "start"])
    inbox = InboxStore(store.redis)
    sent = inbox.send(f"master@{WATCHED}", f"master@{DOCTOR}", "the watched swarm merged a fix")
    assert doctor.main([WATCHED, "stop"]) == 0
    assert inbox.pending_items(f"master@{DOCTOR}") == []
    assert inbox.get(sent.id).state == "cancelled"
    notice = [i for i in inbox.pending_items(f"master@{WATCHED}") if "stopped the Doctor" in i.text]
    assert len(notice) == 1 and notice[0].fyi
    assert notice[0].text.endswith("The Doctor is stopped.")


def test_a_message_to_a_closed_doctors_master_does_not_restart_it(env):
    store, rt, _ = env
    doctor.main([WATCHED, "start"])
    assert doctor.main([WATCHED, "stop"]) == 0
    masters = len(rt.masters)
    sent = InboxStore(store.redis).send(f"master@{WATCHED}", f"master@{DOCTOR}", "still syncing with my peer")
    tick(store, rt)
    assert store.config(DOCTOR).state == "stopped" and store.agents(DOCTOR) == []
    assert len(rt.masters) == masters
    assert InboxStore(store.redis).get(sent.id).state == "cancelled"


def test_status_names_the_link_and_the_doctor_swarm(env, capsys):
    store = env[0]
    doctor.main([WATCHED, "start"])
    capsys.readouterr()
    assert doctor.main([WATCHED, "status"]) == 0
    out = capsys.readouterr().out
    assert f"doctor {DOCTOR} watches {WATCHED}" in out
    assert "peers registered" in out and "ledger open" in out
    assert f"master@{store.config(DOCTOR).code}-0001" in out


def test_status_without_a_doctor_says_so(env, capsys):
    assert doctor.main([WATCHED, "status"]) == 1
    assert "no Doctor" in capsys.readouterr().err


def test_swarm_status_json_names_the_peer_for_the_page(env, capsys):
    doctor.main([WATCHED, "start"])
    capsys.readouterr()
    swarm_cli.main([WATCHED, "status", "--json"])
    assert json.loads(capsys.readouterr().out)["peer"] == DOCTOR


def test_swarm_status_json_prints_the_status_report_from_one_ledger_read(env, monkeypatch, capsys):
    store, _, _ = env
    reads = []
    monkeypatch.setattr(FileLedger, "_call", lambda self, slug, ops=None: reads.append(ops) or state(slug))
    swarm_cli.main([WATCHED, "status", "--json"])
    printed = json.loads(capsys.readouterr().out)
    report = status_report(store, WATCHED, state(WATCHED))
    assert reads == [None]
    assert printed.keys() == report.keys()
    assert printed["config"] == report["config"]
    assert printed["tasks"] == report["tasks"]


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


@pytest.mark.parametrize(
    "rest",
    [
        ["verdict", "health/f1", "established", "--note", "checked"],
        ["task", "health/f1", "--fix", "tune"],
        ["measure", "health/f1"],
        ["rates", "--at", "2026-10-06T10:00Z", "--json"],
        ["intervene", "message", "--to", "master@watch", "--text", "hi"],
    ],
)
def test_the_cli_routes_every_crew_verb_to_the_crew(monkeypatch, rest):
    from scripts import install

    calls = []
    monkeypatch.setattr(doctor, "main", lambda argv: calls.append(doctor.build_parser().parse_args(argv).command) or 0)
    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "doctor", WATCHED, *rest])
    with pytest.raises(SystemExit):
        install.main()
    assert calls == [rest[0]]


@pytest.mark.parametrize("rest", [[], ["--json"], ["--target", "codex"], ["--debug-hook"]])
def test_the_cli_leaves_hook_health_on_the_hook_doctor(monkeypatch, rest):
    import argparse

    from scripts import install

    class HookHealth(Exception):
        pass

    def hook_parser(self, args=None, namespace=None):
        raise HookHealth

    monkeypatch.setattr(doctor, "main", lambda argv: pytest.fail(f"crew doctor got {argv}"))
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", hook_parser)
    monkeypatch.setattr(install.sys, "argv", ["agentihooks", "doctor", *rest])
    with pytest.raises(HookHealth):
        install.main()


STALE = Finding(
    "stale claim", "watch-eng-1", "claimed task with no change for 40 minutes", ("task t1",), "30 minutes", 40
)


def detecting(monkeypatch, *found):
    monkeypatch.setattr(doctor.detect, "readers", lambda *args, **kwargs: {"health": lambda: list(found)})


def tick(store, rt):
    return swarm_cli.run_tick(store, DOCTOR, ledger=FileLedger(), runtime=rt, messenger=FakeHerdr({}))


def test_the_swarm_tick_reports_a_finding_and_task_turns_its_verdict_into_ledger_tasks(env, monkeypatch, capsys):
    store, rt, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    detecting(monkeypatch, STALE)
    doctor.main([WATCHED, "start"])
    [item] = InboxStore(store.redis).inbox(f"master@{DOCTOR}")
    assert f"agentihooks doctor {WATCHED} verdict {STALE.id}" in item.text
    assert not any("new finding" in action for action in tick(store, rt))
    assert doctor.main([WATCHED, "task", STALE.id, "--fix", "tune"]) == 1
    assert "established or early-real" in capsys.readouterr().err
    assert doctor.main([WATCHED, "verdict", STALE.id, "established", "--note", "checked"]) == 0
    assert doctor.main([WATCHED, "task", STALE.id, "--fix", "tune"]) == 0
    tasks = {t["kind"]: t for t in state(DOCTOR)["tasks"]}
    assert set(tasks) == {"troubleshoot", "tune"}
    assert tasks["tune"]["depends_on"] == [tasks["troubleshoot"]["id"]]
    assert f"agentihooks doctor {WATCHED} measure {STALE.id}" in tasks["tune"]["contract"]["check"]
    capsys.readouterr()
    assert doctor.main([WATCHED, "measure", STALE.id]) == 0
    assert capsys.readouterr().out == f"{STALE.id} 40\n"
    detecting(monkeypatch)
    assert doctor.main([WATCHED, "measure", STALE.id]) == 0
    assert capsys.readouterr().out == f"{STALE.id} 0\n"


def test_the_swarm_tick_closes_a_quiet_doctor_with_its_note(env, monkeypatch):
    store, rt, _ = env
    doctor.main([WATCHED, "start"])
    detecting(monkeypatch)
    monkeypatch.setenv("AGENTIHOOKS_DOCTOR_QUIET_MINUTES", "0")
    assert any("no new finding" in action for action in tick(store, rt))
    closed = state(DOCTOR)
    assert closed["closed_at"] and "two hours with no new finding" in closed["overview"]
    assert store.agents(DOCTOR) == [] and store.config(DOCTOR).state == "stopped"
    assert (store.peer(WATCHED), store.peer(DOCTOR)) == ("", "")
    assert not any("no new finding" in action for action in tick(store, rt))


def test_intervene_refuses_a_forbidden_action_and_logs_nothing(env, capsys):
    doctor.main([WATCHED, "start"])
    before = len(state(WATCHED)["chat"])
    assert doctor.main([WATCHED, "intervene", "task-set", "--to", f"{WATCHED}-eng-1"]) == 1
    assert "not an allowed intervention" in capsys.readouterr().err
    assert len(state(WATCHED)["chat"]) == before


def test_intervene_messages_the_watched_master_and_logs_on_both_ledgers(env, monkeypatch):
    store, _, _ = env
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    doctor.main([WATCHED, "start"])
    args = ["intervene", "message", "--to", f"master@{WATCHED}", "--text", "The inbox fix is merged."]
    assert doctor.main([WATCHED, *args]) == 0
    texts = [item.text for item in InboxStore(store.redis).inbox(f"master@{WATCHED}")]
    assert "The inbox fix is merged." in texts
    for slug in (WATCHED, DOCTOR):
        assert any("sent a message to the watched swarm's master" in c["text"] for c in state(slug)["chat"])


@pytest.mark.parametrize("bounded,count", [(False, 2), (True, 2), (True, 0)])
def test_failed_spawn_measure_uses_only_the_spawn_reader(env, monkeypatch, capsys, bounded, count):
    store, _, _ = env
    assert doctor.main([WATCHED, "start"]) == 0
    capsys.readouterr()
    since, until = "2026-10-07T09:58:24Z", "2026-10-07T12:58:24Z"
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: 1791388800000)

    def unrelated(*args, **kwargs):
        pytest.fail("failed spawn measure called unrelated detectors")

    def record(actual_store, slug, now, **kwargs):
        assert actual_store is store
        assert slug == WATCHED
        assert now == 1791388800000
        assert kwargs == ({"since": "@1791367104.000", "until": "@1791377904.000"} if bounded else {})
        return {
            "slug": slug,
            "actions": [f"{slug}: spawn failed for mu1, task mu1 reopened: timeout"] * count,
        }

    monkeypatch.setattr(doctor.detect, "readers", unrelated)
    monkeypatch.setattr(doctor.detect.spawn_read, "records", record)
    flags = ["--since", since, "--until", until] if bounded else []
    assert doctor.main([WATCHED, "measure", "failed-spawn/mu1", *flags]) == 0
    assert capsys.readouterr().out == f"failed-spawn/mu1 {count}\n"


@pytest.mark.parametrize(
    "finding,flags,error",
    [
        ("failed-spawn/mu1", ["--since", "2026-10-07T09:00Z"], "--since and --until must be supplied together"),
        ("failed-spawn/mu1", ["--until", "2026-10-07T12:00Z"], "--since and --until must be supplied together"),
        (
            "failed-spawn/mu1",
            ["--since", "bad", "--until", "2026-10-07T12:00Z"],
            "--since takes an ISO time such as 2026-10-06T12:00Z, not bad",
        ),
        (
            "failed-spawn/mu1",
            ["--since", "2026-10-07T09:00Z", "--until", "bad"],
            "--until takes an ISO time such as 2026-10-06T12:00Z, not bad",
        ),
        (
            "failed-spawn/mu1",
            ["--since", "2026-10-07T12:00Z", "--until", "2026-10-07T12:00Z"],
            "--since must precede --until",
        ),
        (
            "failed-spawn/mu1",
            ["--since", "2026-10-07T13:00Z", "--until", "2026-10-07T12:00Z"],
            "--since must precede --until",
        ),
        (
            "failed-spawn/mu1",
            ["--since", "2026-10-07T12:00Z", "--until", "2030-01-01T00:00Z"],
            "--until must not be in the future",
        ),
        (
            "health/f1",
            ["--since", "2026-10-07T09:00Z", "--until", "2026-10-07T12:00Z"],
            "journal bounds are only supported for failed-spawn findings",
        ),
    ],
)
def test_failed_spawn_measure_refuses_invalid_bounds(env, monkeypatch, capsys, finding, flags, error):
    assert doctor.main([WATCHED, "start"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: 1791388800000)
    assert doctor.main([WATCHED, "measure", finding, *flags]) == 1
    output = capsys.readouterr()
    assert output.err == f"doctor: {error}\n"
    assert output.out == ""


def test_failed_spawn_measure_accepts_a_window_ending_now(env, monkeypatch, capsys):
    store, _, _ = env
    assert doctor.main([WATCHED, "start"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: 1791388800000)
    seen = []
    monkeypatch.setattr(
        doctor.detect.spawn_read, "records", lambda *a, **kw: seen.append(kw) or {"slug": WATCHED, "actions": []}
    )
    flags = ["--since", "2026-10-07T13:00Z", "--until", "2026-10-07T16:00Z"]
    assert doctor.main([WATCHED, "measure", "failed-spawn/mu1", *flags]) == 0
    assert seen == [{"since": "@1791378000.000", "until": "@1791388800.000"}]
    assert capsys.readouterr().out == "failed-spawn/mu1 0\n"


def test_other_measures_read_every_detector_at_one_instant(env, monkeypatch, capsys):
    store, _, _ = env
    assert doctor.main([WATCHED, "start"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: 1791388800000)
    calls = []

    def readers(actual_store, ledger, slug, now):
        calls.append((actual_store, type(ledger), slug, now))
        return {"health": lambda: [STALE], "inbox": lambda: 1 / 0}

    monkeypatch.setattr(doctor.detect, "readers", readers)
    assert doctor.main([WATCHED, "measure", STALE.id]) == 0
    assert calls == [(store, swarm_cli.LedgerClient, WATCHED, 1791388800000)]
    output = capsys.readouterr()
    assert output.out == f"{STALE.id} {STALE.measure}\n"
    assert output.err == "the inbox detector failed: ZeroDivisionError: division by zero\n"
