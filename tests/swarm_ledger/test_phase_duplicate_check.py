import json
import uuid
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_duplicates, ledger_phase_cli, ledger_phases, ledger_task_duplicates
from tests.swarm_ledger.test_ledger_authority import MASTER, SLUG, admin_put, cli_ledger, core, send
from tests.swarm_ledger.test_task_duplicate_check import Judge, authority_live, judged
from tests.swarm_ledger.test_task_duplicate_check import live as _task_live

live = _task_live
authority_live = authority_live
AGAIN = "Automate the releases"
DESCRIPTION = "Ship releases from CI on every tag"
OTHER = "Fold the inbox drawer"
HINT = 'If it is a different change, add it with --not-duplicate "<why it differs>"'


def refusal(new, existing):
    return (
        f'phase {new} repeats phase {existing} "Release automation" (open, rank none, phase none): '
        f"it is on the ledger and will be built. {HINT}"
    )


def phase(phase_id, title=AGAIN, description=DESCRIPTION, **fields):
    return {
        "op": "phase_add",
        "id": uuid.uuid4().hex,
        "by": MASTER,
        "phase": phase_id,
        "title": title,
        "description": description,
        **fields,
    }


def entry(phase_id, title=AGAIN, description=DESCRIPTION, **fields):
    return {"phase": phase_id, "title": title, "description": description, **fields}


def append(*entries):
    return {"op": "phase_append", "id": "append-plan", "by": MASTER, "phases": list(entries)}


def operations(live, *ops):
    headers = {"X-Ledger-Token": live["admin"]}
    guards = {
        path: json.loads(send(live, "GET", f"/api/v1/ledgers/{SLUG}/{path}", **headers)[1])["revision"]
        for path in ("tasks", "phases")
    }
    body = {"operation_id": uuid.uuid4().hex, "ops": list(ops), "guards": guards}
    path = f"/api/v1/ledgers/{SLUG}/operations"
    status, data, _ = send(
        live, "POST", path, json.dumps(body).encode(), **headers, **{"Content-Type": "application/json"}
    )
    return status, json.loads(data)


def stored(phase_id):
    return next((p for p in core.sync(SLUG)[0]["phases"] if p["id"] == phase_id), None)


def test_a_repeated_phase_add_is_refused_naming_it(live, monkeypatch):
    first = live["phases"][0]
    judge = judged(monkeypatch, Judge(yes={first}))
    add = phase("p9")
    status, reply = operations(live, add)
    assert (status, reply["rejected"], reply["applied"]) == (200, [add["id"]], [])
    assert refusal("p9", first) in reply["_meta"]["warnings"]
    assert stored("p9") is None
    assert judge.asked == ["ledger-duplicate"] and judge.lock_free == [True]


def test_the_page_transport_refuses_a_repeated_phase_too(live, monkeypatch):
    first = live["phases"][0]
    judged(monkeypatch, Judge(yes={first}))
    add = phase("p9")
    status, reply = admin_put(live, add)
    assert (status, reply["rejected"]) == (200, [add["id"]])
    assert refusal("p9", first) in reply["_meta"]["warnings"]
    assert stored("p9") is None


def test_a_plan_append_refuses_only_the_repeated_phase(live, monkeypatch):
    first = live["phases"][0]
    judged(monkeypatch, Judge(yes={first}))
    plan = append(entry("p9"), entry("p10", OTHER, "The inbox drawer folds shut"))
    status, reply = operations(live, plan)
    assert (status, reply["applied"], reply["rejected"]) == (200, ["append-plan"], [])
    assert refusal("p9", first) in reply["_meta"]["warnings"]
    assert stored("p9") is None
    assert stored("p10")["title"] == OTHER


def test_a_plan_append_whose_every_phase_repeats_is_rejected(live, monkeypatch):
    first = live["phases"][0]
    judged(monkeypatch, Judge(yes={first}))
    status, reply = operations(live, append(entry("p9"), entry("p10", "Automate releases", DESCRIPTION)))
    assert (status, reply["applied"], reply["rejected"]) == (200, [], ["append-plan"])
    assert {refusal("p9", first), refusal("p10", first)} <= set(reply["_meta"]["warnings"])
    assert stored("p9") is None and stored("p10") is None


def test_the_not_duplicate_reason_skips_the_check_and_is_stored(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={live["phases"][0]}))
    reason = "this one ships the docs site and that one the package"
    status, reply = operations(live, phase("p9", not_duplicate=reason), append(entry("p10", not_duplicate=reason)))
    assert (status, reply["rejected"]) == (200, [])
    assert stored("p9")["not_duplicate"] == reason and stored("p10")["not_duplicate"] == reason
    assert judge.asked == []


def test_a_classifier_failure_lands_the_phases_with_a_warning(live, monkeypatch):
    judged(monkeypatch, Judge(yes={live["phases"][0]}, error="unavailable"))
    status, reply = operations(live, phase("p9"), append(entry("p10")))
    assert (status, reply["rejected"]) == (200, [])
    for new in ("p9", "p10"):
        assert (
            f"the duplicate check did not run for phase {new} because the classifier did not answer, "
            "so it was added unchecked"
        ) in reply["_meta"]["warnings"]
        assert stored(new)["title"] == AGAIN


def cli(monkeypatch, *argv):
    args = cli_ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, *argv])
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "")
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    return args


def test_the_cli_phase_add_exits_with_the_refusal(live, monkeypatch):
    first = live["phases"][0]
    judged(monkeypatch, Judge(yes={first}))
    with pytest.raises(SystemExit) as refused:
        cli_ledger.cmd_phase(cli(monkeypatch, "phase", "add", "p9", AGAIN, "--description", DESCRIPTION))
    assert refusal("p9", first) in str(refused.value)
    assert stored("p9") is None


def test_the_cli_phase_add_override_lands_with_its_reason(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={live["phases"][0]}))
    argv = ("phase", "add", "p9", AGAIN, "--description", DESCRIPTION, "--not-duplicate", "the docs site")
    cli_ledger.cmd_phase(cli(monkeypatch, *argv))
    assert stored("p9")["not_duplicate"] == "the docs site"
    assert judge.asked == []


def test_the_cli_plan_phases_names_the_refused_repeat_and_appends_the_rest(live, monkeypatch, tmp_path, capsys):
    first = live["phases"][0]
    judged(monkeypatch, Judge(yes={first}))
    plan = tmp_path / "plan.json"
    phases = [entry("p9"), entry("p10", OTHER, "The inbox drawer folds shut")]
    plan.write_text(json.dumps({"phases": [{"id": p.pop("phase"), **p} for p in phases]}))
    cli_ledger.cmd_plan(cli(monkeypatch, "plan", "phases", str(plan)))
    out, err = capsys.readouterr()
    assert json.loads(out)["appended"] == ["p10"]
    assert err.splitlines() == [refusal("p9", first)]
    assert stored("p9") is None and stored("p10")["title"] == OTHER


def test_the_cli_phase_flag_says_what_it_skips():
    parser = cli_ledger.build_parser()
    args = parser.parse_args(["--slug", SLUG, "--as", MASTER, "phase", "add", "p9", AGAIN])
    assert args.not_duplicate == ""
    text = " ".join(next(a for a in parser._subparsers._actions if a.choices).choices["phase"].format_help().split())
    assert (
        "--not-duplicate NOT_DUPLICATE why the phase differs from the one it resembles; skips the duplicate check"
        in text
    )


def test_a_plan_file_phase_carries_its_not_duplicate_reason():
    plan = {"phases": [{"title": AGAIN, "not_duplicate": "the docs site"}, {"title": OTHER}]}
    entries = ledger_phase_cli.append_phases(plan, ["p1"])
    assert [e.get("not_duplicate") for e in entries] == ["the docs site", None]


@pytest.mark.parametrize(
    ("op", "screened"),
    [
        (phase("p9"), True),
        (phase("p9", not_duplicate="the docs site"), False),
        (append(entry("p9"), entry("p10", not_duplicate="the docs site")), True),
        (append(entry("p9", not_duplicate="the docs site")), False),
    ],
)
def test_which_phase_adds_are_screened(op, screened):
    assert ledger_task_duplicates.checked(op) is screened


def test_the_screen_asks_the_finder_once_for_every_screened_phase(monkeypatch):
    asked = []
    monkeypatch.setattr(
        ledger_task_duplicates, "find", lambda doc, adds, kind: asked.append((kind, adds)) or [None] * len(adds)
    )
    add = phase("p8", "Retry the watch")
    plan = append(entry("p9"), entry("p10", not_duplicate="the docs site"), entry("p11", OTHER))
    screen = ledger_task_duplicates.screen({"phases": [{"id": "p1"}]}, [add, plan])
    assert [kind for kind, _ in asked] == ["phase"]
    assert [(a["id"], a["phase"], a["title"]) for a in asked[0][1]] == [
        (add["id"], "p8", "Retry the watch"),
        ("append-plan/p9", "p9", AGAIN),
        ("append-plan/p11", "p11", OTHER),
    ]
    assert screen.seen == frozenset({"phases/p1", "phases/p8", "phases/p9", "phases/p10", "phases/p11"})


def test_a_phase_refusal_names_the_phase_it_repeats():
    match = {"id": "p1", "kind": "phase", "title": "Release automation", "state": "open", "rank": None}
    assert ledger_task_duplicates.refusal(phase("p9"), {**match, "phase_title": None}) == refusal("p9", "p1")


def test_the_finder_pool_for_a_phase_holds_every_phase_in_scope():
    doc = {"phases": [{"id": "p1"}, {"id": "p2", "done": True}, {"id": "p3", "out_of_scope": True}]}
    assert ledger_duplicates._pool(doc, "phase") == [("phase", {"id": "p1"}), ("phase", {"id": "p2", "done": True})]


@pytest.mark.parametrize("reason", [" ", 5])
def test_a_not_duplicate_reason_must_be_plain_words_on_a_phase(reason):
    for op in (phase("p9", not_duplicate=reason), append(entry("p9", not_duplicate=reason))):
        with pytest.raises(ValueError) as refused:
            ledger_phases.check(op)
        assert str(refused.value) == (
            "not_duplicate must say in plain words why the phase differs from the one it resembles"
        )


def test_a_phase_add_with_a_reason_passes_the_check():
    ledger_phases.check(phase("p9", not_duplicate="the docs site"))
    ledger_phases.check(append(entry("p9", not_duplicate="the docs site")))


class Inner:
    def __init__(self, accepted=True):
        self.applied, self.accepted = [], accepted

    def apply(self, doc, op, ctx, apply_op):
        self.applied.append((op, apply_op))
        return self.accepted


def test_an_append_drops_a_phase_repeating_one_that_landed_during_its_check():
    screen = ledger_task_duplicates.Screen(seen=frozenset({"phases/p1", "phases/p9", "phases/p10"}))
    gate = ledger_task_duplicates.Gate(screen, Inner())
    doc = {"phases": [{"id": "p1", "title": "Seen"}, {"id": "p4", "title": f"  {AGAIN.upper()} "}]}
    ctx = SimpleNamespace(refused=[])
    plan = append(entry("p9"), entry("p10", OTHER))
    assert gate.apply(doc, plan, ctx, "apply") is True
    assert gate.inner.applied == [({**plan, "phases": [entry("p10", OTHER)]}, "apply")]
    assert ctx.refused == [
        f'phase p9 repeats phase p4 "  {AGAIN.upper()} ", added while its duplicate check ran. {HINT}'
    ]
    assert screen.warnings == {
        "append-plan/p10": (
            "the duplicate check did not compare phase p10 with p4, added while it ran, so check them for repeats"
        )
    }


def test_an_append_whose_every_phase_was_refused_is_refused_with_each_reason():
    screen = ledger_task_duplicates.Screen(refused={"append-plan/p9": "first", "append-plan/p10": "second"})
    gate = ledger_task_duplicates.Gate(screen, Inner(accepted=False))
    plan = append(entry("p9"), entry("p10", OTHER))
    assert gate.apply({}, plan, None, "apply") is False
    (applied, refuse), ctx = gate.inner.applied[0], SimpleNamespace(refused=[])
    assert applied == plan and refuse({}, {}, ctx) is False
    assert ctx.refused == ["first", "second"]


def test_an_append_with_no_repeat_passes_through_unchanged():
    gate = ledger_task_duplicates.Gate(ledger_task_duplicates.Screen(seen=frozenset({"phases/p9"})), Inner())
    ctx = SimpleNamespace(refused=[])
    plan = append(entry("p9"))
    assert gate.apply({"phases": []}, plan, ctx, "apply") is True
    assert gate.inner.applied == [(plan, "apply")] and ctx.refused == []
    assert gate.screen.warnings == {}


def test_an_append_the_locked_apply_refuses_is_not_flagged():
    screen = ledger_task_duplicates.Screen(seen=frozenset({"phases/p10"}))
    gate = ledger_task_duplicates.Gate(screen, Inner(accepted=False))
    doc = {"phases": [{"id": "p4", "title": "Landed meanwhile"}]}
    assert gate.apply(doc, append(entry("p10", OTHER)), SimpleNamespace(refused=[]), "apply") is False
    assert screen.warnings == {}


def test_a_phase_with_a_reason_is_not_flagged_for_phases_that_landed_during_the_write():
    screen = ledger_task_duplicates.Screen(seen=frozenset({"phases/p9"}))
    gate = ledger_task_duplicates.Gate(screen, Inner())
    doc = {"phases": [{"id": "p4", "title": AGAIN}]}
    assert gate.apply(doc, phase("p9", not_duplicate="the docs site"), None, "apply") is True
    assert screen.warnings == {}


def test_a_phase_add_naming_a_phase_already_on_the_ledger_is_not_screened(monkeypatch):
    asked = []
    monkeypatch.setattr(ledger_task_duplicates, "find", lambda doc, adds, kind: asked.append(adds) or [None])
    screen = ledger_task_duplicates.screen({"phases": [{"id": "p1", "title": AGAIN}]}, [phase("p1"), phase("p9")])
    assert [[add["phase"] for add in adds] for adds in asked] == [["p9"]]
    assert screen.seen == frozenset({"phases/p1", "phases/p9"})
