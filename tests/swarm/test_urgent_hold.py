import pytest

from tests.swarm.test_cli import env as env
from tests.swarm.test_cli import run

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "sw"
MINUTE = 60_000


def task(task_id, phase="p1", **fields):
    return {"id": task_id, "title": f"title {task_id}", "phase": phase, "state": "open", "lane": "eng", **fields}


def doc(tasks, **extra):
    phases = [
        {"id": "p1", "title": "build", "depends_on": []},
        {"id": "p2", "title": "later", "depends_on": ["p1"]},
    ]
    return {"overview": "o", "tasks": list(tasks), "phases": phases, **extra}


def hold(found, task_id, fix=""):
    from scripts.swarm import claim_hold
    from scripts.swarm_ledger.repository import hierarchy

    rows = {t["id"]: t for t in found["tasks"]}
    return claim_hold.hold(rows[task_id], rows, found, hierarchy.project(found)[0], fix)


@pytest.fixture
def store():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    return saved


def test_a_claimable_task_has_no_hold():
    assert hold(doc([task("a")]), "a") == ""


def test_a_task_in_a_waiting_phase_is_held_by_its_phase():
    assert hold(doc([task("a", phase="p2")]), "a") == "its phase later is waiting on an earlier phase"


def test_a_frozen_task_is_held_by_the_freeze_that_covers_it():
    found = doc([task("a"), task("b")], freezes=[{"verb": "freeze", "target": "tasks/a"}])
    assert hold(found, "a") == "the freeze on task title a holds it"
    assert hold(found, "b") == ""


def test_a_focus_holds_a_normal_task_but_never_an_urgent_one():
    focus = [{"verb": "focus", "target": "lane:ci"}]
    assert hold(doc([task("a")], freezes=focus), "a") == "a focus elsewhere holds it"
    assert hold(doc([task("a", rank="urgent")], freezes=focus), "a") == ""


def test_an_urgent_task_is_still_held_by_a_freeze():
    found = doc([task("a", rank="urgent")], freezes=[{"verb": "freeze", "target": "phases/p1"}])
    assert hold(found, "a") == "the freeze on phase build holds it"


def test_a_task_with_an_open_dependency_names_it():
    found = doc([task("a"), task("b", depends_on=["a"]), task("c", state="done"), task("d", depends_on=["c"])])
    assert hold(found, "b") == "it waits on task a"
    assert hold(found, "d") == ""


def test_the_tick_ready_check_agrees_with_the_hold():
    from scripts.swarm import tick
    from scripts.swarm_ledger.repository import hierarchy

    found = doc([task("a"), task("b", phase="p2"), task("c", state="claimed"), task("d", out_of_scope=True)])
    rows = {t["id"]: t for t in found["tasks"]}
    graph = hierarchy.project(found)[0]
    assert [t for t in rows if tick.ready(rows[t], rows, found, graph, "")] == ["a"]


def test_an_urgent_task_held_ten_minutes_raises_a_finding_naming_the_hold(store):
    from scripts.swarm.health import urgent_hold

    found = doc(
        [
            task("b", phase="p2"),
            task("c", rank="urgent"),
            task("a", phase="p2", rank="urgent"),
            task("d", rank="urgent", depends_on=["b"]),
        ]
    )
    urgent_hold.observe(store, SLUG, found, 0)
    urgent_hold.observe(store, SLUG, found, 10 * MINUTE)
    assert urgent_hold.findings(store, SLUG) == []
    urgent_hold.observe(store, SLUG, found, 10 * MINUTE + 1)
    finding, waiting = urgent_hold.findings(store, SLUG)
    assert (waiting.id, waiting.evidence) == ("urgent-unclaimable/d", ("it waits on task b",))
    assert finding.id == "urgent-unclaimable/a"
    assert finding.summary == "urgent task title a has been unclaimable for 10 minutes"
    assert finding.evidence == ("its phase later is waiting on an earlier phase",)
    assert (finding.threshold, finding.measure) == ("ten minutes", 10)


def test_a_recent_hold_listed_first_does_not_hide_an_older_one(store):
    from scripts.swarm.health import urgent_hold

    urgent_hold.observe(store, SLUG, doc([task("a", phase="p2"), task("b", phase="p2", rank="urgent")]), 0)
    both = doc([task("a", phase="p2", rank="urgent"), task("b", phase="p2", rank="urgent")])
    urgent_hold.observe(store, SLUG, both, 5 * MINUTE)
    urgent_hold.observe(store, SLUG, both, 11 * MINUTE)
    assert [f.id for f in urgent_hold.findings(store, SLUG)] == ["urgent-unclaimable/b"]


def test_the_doctor_fix_phase_exempts_its_tasks_from_a_focus():
    found = doc([task("a", phase="fix")], freezes=[{"verb": "focus", "target": "lane:ci"}])
    found["phases"].append({"id": "fix", "title": "fix", "depends_on": []})
    assert hold(found, "a", fix="fix") == ""
    assert hold(found, "a") == "a focus elsewhere holds it"


def test_a_lane_freeze_is_named_by_its_lane():
    found = doc([task("a")], freezes=[{"verb": "freeze", "target": "lane:eng"}])
    assert hold(found, "a") == "the freeze on the eng lane holds it"


def test_only_the_open_dependencies_are_named():
    found = doc([task("a", state="done"), task("b"), task("c", depends_on=["a", "b"])])
    assert hold(found, "c") == "it waits on task b"


def test_a_dependency_missing_from_the_ledger_is_named_as_open():
    assert hold(doc([task("c", depends_on=["gone"])]), "c") == "it waits on task gone"


def test_an_urgent_hold_clears_once_the_task_is_claimable_and_restarts_its_clock(store):
    from scripts.swarm.health import urgent_hold

    held = doc([task("a", phase="p2", rank="urgent")])
    urgent_hold.observe(store, SLUG, held, 0)
    urgent_hold.observe(store, SLUG, doc([task("a", rank="urgent")]), 5 * MINUTE)
    urgent_hold.observe(store, SLUG, held, 6 * MINUTE)
    urgent_hold.observe(store, SLUG, held, 16 * MINUTE)
    assert urgent_hold.findings(store, SLUG) == []
    urgent_hold.observe(store, SLUG, held, 16 * MINUTE + 1)
    assert [f.id for f in urgent_hold.findings(store, SLUG)] == ["urgent-unclaimable/a"]


def test_a_claimed_or_out_of_scope_urgent_task_is_never_reported(store):
    from scripts.swarm.health import urgent_hold

    found = doc(
        [
            task("a", phase="p2", rank="urgent", state="claimed"),
            task("b", phase="p2", rank="urgent", out_of_scope=True),
            task("c", phase="p2", rank="urgent", merged_into="d"),
        ]
    )
    urgent_hold.observe(store, SLUG, found, 0)
    urgent_hold.observe(store, SLUG, found, 20 * MINUTE)
    assert urgent_hold.findings(store, SLUG) == []


def test_no_observation_means_no_finding(store):
    from scripts.swarm.health import urgent_hold

    assert urgent_hold.findings(store, SLUG) == []


def test_the_first_covering_freeze_names_the_hold():
    freezes = [{"verb": "freeze", "target": "phases/p1"}, {"verb": "freeze", "target": "tasks/a"}]
    assert hold(doc([task("a")], freezes=freezes), "a") == "the freeze on phase build holds it"


def test_a_watched_swarm_focus_holds_a_task_its_own_focus_covers():
    found = doc([task("a")], freezes=[{"verb": "focus", "target": "lane:eng"}], watched_focus=["the focus elsewhere"])
    assert hold(found, "a") == "a focus elsewhere holds it"


def test_swarm_status_reports_the_urgent_hold(store):
    from scripts.swarm import status
    from scripts.swarm.health import urgent_hold

    found = doc([task("a", phase="p2", rank="urgent")])
    urgent_hold.observe(store, SLUG, found, 0)
    urgent_hold.observe(store, SLUG, found, 11 * MINUTE)
    rows = status.findings(store, SLUG, store.config(SLUG), found["tasks"], [])
    assert [row["id"] for row in rows if row["kind"] == "urgent unclaimable"] == ["urgent-unclaimable/a"]


def test_an_untitled_phase_is_named_by_its_id():
    found = doc([task("a", phase="p2")])
    del found["phases"][1]["title"]
    assert hold(found, "a") == "its phase p2 is waiting on an earlier phase"


def test_a_task_waiting_on_several_tasks_names_each_once_parked_first():
    found = doc([task("a"), task("b"), task("c", depends_on=["a", "b"], parked_on=["b"])])
    assert hold(found, "c") == "it waits on tasks b, a"


def test_a_planned_phase_holds_its_build_tasks_until_review():
    found = doc([task("a"), task("plan", kind="plan", lane="plan", state="done")])
    found["phases"][0]["planning"] = "auto"
    assert hold(found, "a") == "its phase build is waiting for its plan review"


def test_an_urgent_refusal_reads_a_task_without_a_state_as_open():
    from scripts.swarm_ledger import ledger_rank

    found = doc([])
    assert ledger_rank.urgent_refusal({"id": "a", "phase": "p2", "rank": "urgent"}, found) == (
        "task a cannot be urgent: its phase later is waiting on an earlier phase" + ledger_rank.HELD
    )
    assert ledger_rank.urgent_refusal({"id": "a", "phase": "p2", "rank": "high"}, found) == ""
    assert ledger_rank.urgent_refusal({"id": "a", "phase": "p1", "rank": "urgent"}, found) == ""


def test_an_untitled_urgent_task_is_named_by_its_id(store):
    from scripts.swarm.health import urgent_hold

    found = doc([{"id": "a", "phase": "p2", "state": "open", "rank": "urgent"}])
    urgent_hold.observe(store, SLUG, found, 0)
    urgent_hold.observe(store, SLUG, found, 12 * MINUTE)
    (finding,) = urgent_hold.findings(store, SLUG)
    assert finding.summary == "urgent task a has been unclaimable for 12 minutes"


def test_an_urgent_task_waiting_on_a_release_is_reported(store, monkeypatch):
    from scripts.swarm import release_wait
    from scripts.swarm.health import urgent_hold

    tags = {"/repo": ["v1.2.3"]}
    monkeypatch.setattr(release_wait, "tags", lambda repo: tags[repo])
    found = doc([task("a", rank="urgent", after_release="v1.2.3"), task("b", rank="urgent", after_release="v1.2.2")])
    urgent_hold.observe(store, SLUG, found, 0)
    urgent_hold.observe(store, SLUG, found, 11 * MINUTE)
    assert [(f.id, f.evidence) for f in urgent_hold.findings(store, SLUG)] == [
        ("urgent-unclaimable/a", ("it waits on a release newer than v1.2.3",))
    ]


def held_tick_env(env):
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    ledger.phases = [{"id": "p1", "depends_on": []}, {"id": "p2", "title": "later", "depends_on": ["p1"]}]
    ledger.rows["t1"].update(phase="p2", rank="urgent", title="fix quota")
    return store, ledger, rt


def test_the_tick_records_an_urgent_task_its_phase_holds(env):
    from scripts.swarm import cli
    from scripts.swarm.health import urgent_hold
    from tests.swarm.test_delivery import FakeHerdr

    store, ledger, rt = held_tick_env(env)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    held = urgent_hold.read(store, "sw")["tasks"]
    assert {task_id: row["why"] for task_id, row in held.items()} == {
        "t1": "its phase later is waiting on an earlier phase"
    }


def test_the_tick_hands_an_urgent_hold_past_ten_minutes_to_the_findings_pass(env, monkeypatch):
    from scripts.swarm import cli
    from tests.swarm.test_delivery import FakeHerdr

    store, ledger, rt = held_tick_env(env)
    clock = [1_000_000]
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])
    shown, real = [], cli.ledger_events.findings_pass

    def spy(inbox, store, slug, found, doc=None):
        shown.append([row["id"] for row in found])
        return real(inbox, store, slug, found, doc)

    monkeypatch.setattr(cli.ledger_events, "findings_pass", spy)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    clock[0] += 600_001
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert "urgent-unclaimable/t1" not in shown[0]
    assert "urgent-unclaimable/t1" in shown[-1]
