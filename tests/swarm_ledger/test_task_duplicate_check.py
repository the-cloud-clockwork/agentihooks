import inspect
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.swarm.ledger_client import LedgerClient, LedgerRefused
from scripts.swarm_ledger import ledger, ledger_duplicates, ledger_task_duplicates, ledger_tasks
from tests.swarm_ledger.test_ledger_authority import MASTER, SLUG, admin_put, cli_ledger, core, new_ledger, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

pytestmark = pytest.mark.xdist_group("fakeredis")

authority_live = _authority_live
REPEAT = "Publish the package to PyPI from the main branch"
OPEN_REFUSAL = (
    'task t9 repeats task t1 "Publish the wheel to PyPI from main" (open, rank low, phase Release automation): '
    'it is on the ledger and will be built. If it is a different change, add it with --not-duplicate "<why it differs>"'
)
DONE_REFUSAL = (
    'task t9 repeats task t2 "Fold the chat panel on header click" (done, rank normal, phase Ledger page): '
    'it is already built. If it is a different change, add it with --not-duplicate "<why it differs>"'
)
UNANSWERED = (
    "the duplicate check did not run for task t9 because the classifier did not answer, so it was added unchecked"
)


class Judge:
    def __init__(self, yes=(), error=None, hold=0):
        self.spec, self.asked, self.lock_free = json.dumps([sorted(yes), error, hold]), [], []
        self.find = ledger_task_duplicates.find

    def __call__(self, doc, adds):
        self.asked.append("ledger-duplicate")
        self.lock_free.append(core.LOCK.acquire(blocking=False) and (core.LOCK.release() or True))
        return self.find(doc, adds)


@pytest.fixture
def live(authority_live, monkeypatch):
    content = {
        "title": "Duplicates",
        "phases": [
            {"title": "Release automation", "description": "Ship releases from CI"},
            {"title": "Ledger page", "description": "Page layout"},
        ],
    }
    page = new_ledger.render(new_ledger.build_doc(content), SLUG, authority_live["port"])
    html, document = core.paths(SLUG)
    html.write_text(page)
    document.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    first, second = (phase["id"] for phase in state["phases"])
    seed = [
        task("t1", "Publish the wheel to PyPI from main", first, "swarm", rank="low"),
        task("t2", "Fold the chat panel on header click", second, "swarm"),
    ]
    seed[0]["description"] = "Run the publish workflow on main after the release tag"
    seed[1]["description"] = "The chat panel folds like every other section"
    core.sync(SLUG, ops=seed)
    core.sync(
        SLUG,
        ops=[{"op": "task_update", "id": "seed-done", "by": "swarm", "item": "tasks/t2", "fields": {"state": "done"}}],
    )
    return {**authority_live, "admin": core.read_token(page), "phases": (first, second)}


def task(task_id, title, phase, by=MASTER, **fields):
    return {
        "op": "task_add",
        "id": uuid.uuid4().hex,
        "by": by,
        "task": task_id,
        "title": title,
        "lane": "eng",
        "phase": phase,
        **fields,
    }


def judged(monkeypatch, judge):
    monkeypatch.setattr(
        ledger_task_duplicates, "CHILD", (sys.executable, "-m", "tests.swarm_ledger.duplicate_child", judge.spec)
    )
    monkeypatch.setattr(ledger_task_duplicates, "find", judge)
    return judge


def operations(live, *ops, method="POST"):
    body = {"operation_id": uuid.uuid4().hex, "ops": list(ops), "guards": guards(live)}
    headers = {"X-Ledger-Token": live["admin"], "Content-Type": "application/json"}
    status, data, _ = send(live, method, f"/api/v1/ledgers/{SLUG}/operations", json.dumps(body).encode(), **headers)
    return status, json.loads(data)


def guards(live):
    headers = {"X-Ledger-Token": live["admin"]}
    _, data, _ = send(live, "GET", f"/api/v1/ledgers/{SLUG}/tasks", **headers)
    return {"tasks": json.loads(data)["revision"]}


def stored(task_id):
    return next((t for t in core.sync(SLUG)[0]["tasks"] if t["id"] == task_id), None)


def test_the_api_refuses_a_repeat_naming_its_id_state_rank_and_phase(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"t1"}))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert status == 200
    assert len(reply["rejected"]) == 1 and reply["applied"] == []
    assert OPEN_REFUSAL in reply["_meta"]["warnings"]
    assert stored("t9") is None
    assert judge.asked == ["ledger-duplicate"] and judge.lock_free == [True]


def test_a_repeat_of_a_done_task_says_it_is_already_built(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t2"}))
    status, reply = operations(live, task("t9", "Fold the chat panel when its header is clicked", live["phases"][1]))
    assert status == 200 and DONE_REFUSAL in reply["_meta"]["warnings"]
    assert stored("t9") is None


def test_the_api_put_transport_refuses_a_repeat_too(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]), method="PUT")
    assert status == 200 and OPEN_REFUSAL in reply["_meta"]["warnings"]
    assert stored("t9") is None


def test_a_distinct_task_lands_without_a_warning(live, monkeypatch):
    judged(monkeypatch, Judge())
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert not [w for w in reply["_meta"]["warnings"] if "duplicate" in w]
    assert stored("t9")["title"] == REPEAT


def test_the_not_duplicate_reason_skips_the_check_and_is_stored(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"t1"}))
    reason = "the wheel task covers the library and this one the command line package"
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0], not_duplicate=reason))
    assert (status, reply["rejected"]) == (200, [])
    assert stored("t9")["not_duplicate"] == reason
    assert judge.asked == []


def test_a_classifier_failure_lands_the_task_with_a_warning(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}, error="unavailable"))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert UNANSWERED in reply["_meta"]["warnings"]
    assert stored("t9")["title"] == REPEAT


def test_a_classifier_past_its_budget_lands_the_task_with_a_warning(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}, hold=30))
    monkeypatch.setattr(ledger_task_duplicates, "BUDGET_S", 0.05)
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert (
        "the duplicate check did not run for task t9 because it took longer than 0.05 seconds, so it was added unchecked"
        in reply["_meta"]["warnings"]
    )
    assert stored("t9") is not None


def test_the_page_transport_refuses_a_repeat_too(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}))
    status, reply = admin_put(live, task("t9", REPEAT, live["phases"][0]))
    assert status == 200 and len(reply["rejected"]) == 1
    assert OPEN_REFUSAL in reply["_meta"]["warnings"]
    assert stored("t9") is None


def test_the_tick_plan_and_release_tasks_are_never_checked(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"t1", "t2"}))
    first = live["phases"][0]
    client = LedgerClient(service=True)
    with patch.dict("os.environ", {"AGENTIHOOKS_AGENT_NAME": "", "AGENTIHOOKS_SWARM": ""}):
        client.add_task(
            SLUG, {"task": f"plan-{first}", "title": REPEAT, "lane": "plan", "kind": "plan", "phase": first}, "swarm"
        )
        client.add_task(
            SLUG, {"task": f"release-{first}", "title": REPEAT, "lane": "eng", "kind": "code", "phase": first}, "swarm"
        )
    assert judge.asked == []
    assert stored(f"plan-{first}") and stored(f"release-{first}")


def test_the_ledger_client_raises_the_refusal(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}))
    with patch.dict("os.environ", {"AGENTIHOOKS_AGENT_NAME": "", "AGENTIHOOKS_SWARM": ""}):
        with pytest.raises(LedgerRefused, match="it is on the ledger and will be built"):
            LedgerClient(service=True).add_task(
                SLUG, {"task": "t9", "title": REPEAT, "lane": "eng", "phase": live["phases"][0]}, MASTER
            )


def cli(*argv):
    args = cli_ledger.build_parser().parse_args(["--slug", SLUG, "--as", MASTER, "task", "add", *argv])
    with patch.dict("os.environ", {"AGENTIHOOKS_AGENT_NAME": "", "AGENTIHOOKS_SWARM": ""}):
        cli_ledger.cmd_task(args)


def test_the_cli_exits_with_the_refusal(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}))
    with pytest.raises(SystemExit) as refused:
        cli("t9", REPEAT, "--phase", live["phases"][0])
    assert OPEN_REFUSAL in str(refused.value)
    assert stored("t9") is None


def test_the_cli_override_lands_with_its_reason(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"t1"}))
    cli("t9", REPEAT, "--phase", live["phases"][0], "--not-duplicate", "a different package")
    assert stored("t9")["not_duplicate"] == "a different package"
    assert judge.asked == []


def test_the_cli_prints_the_warning_when_the_check_did_not_run(live, monkeypatch, capsys):
    judged(monkeypatch, Judge(error="unavailable"))
    cli("t9", REPEAT, "--phase", live["phases"][0])
    assert UNANSWERED in capsys.readouterr().err
    assert stored("t9") is not None


def test_an_empty_not_duplicate_reason_is_refused(live):
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0], not_duplicate=" "))
    assert status == 400 and "not_duplicate" in reply["error"]["message"]


def test_a_broken_check_lands_the_task_naming_its_error(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}, error="KeyError"))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert (
        "the duplicate check did not run for task t9 because it failed with KeyError, so it was added unchecked"
        in reply["_meta"]["warnings"]
    )
    assert stored("t9") is not None


def test_a_timeout_error_inside_the_check_is_named_not_counted_as_slow(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}, error="TimeoutError"))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert (
        "the duplicate check did not run for task t9 because it failed with TimeoutError, so it was added unchecked"
        in reply["_meta"]["warnings"]
    )


def test_a_refused_repeat_still_needs_a_current_revision(live, monkeypatch):
    stale = guards(live)
    core.sync(SLUG, ops=[task("t8", "Rename the ledger page title", live["phases"][1])])
    judge = judged(monkeypatch, Judge(yes={"t1"}))
    body = {"operation_id": uuid.uuid4().hex, "ops": [task("t9", REPEAT, live["phases"][0])], "guards": stale}
    headers = {"X-Ledger-Token": live["admin"], "Content-Type": "application/json"}
    status, data, _ = send(live, "POST", f"/api/v1/ledgers/{SLUG}/operations", json.dumps(body).encode(), **headers)
    assert status == 409 and json.loads(data)["error"]["code"] == "revision_conflict"
    assert judge.asked == ["ledger-duplicate"] and stored("t9") is None


def test_a_plan_id_from_any_writer_but_the_tick_is_checked(live, monkeypatch):
    judge = judged(monkeypatch, Judge(yes={"t1"}))
    first = live["phases"][0]
    status, reply = operations(live, task(f"plan-{first}", REPEAT, first, kind="plan", lane="plan"))
    assert status == 200 and len(reply["rejected"]) == 1
    assert judge.asked == ["ledger-duplicate"] and stored(f"plan-{first}") is None


def test_the_budget_leaves_the_cli_time_for_the_locked_apply():
    assert ledger_task_duplicates.BUDGET_S * 2 <= inspect.signature(cli_ledger.request).parameters["timeout"].default


def test_the_server_marker_for_an_unchecked_add_matches_the_finder():
    assert ledger_task_duplicates.UNCHECKED == ledger_duplicates.UNCHECKED


def test_unreadable_finder_output_lands_the_task_naming_the_error(live, monkeypatch):
    monkeypatch.setattr(ledger_task_duplicates, "CHILD", (sys.executable, "-c", "print('not json')"))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert (
        "the duplicate check did not run for task t9 because it failed with JSONDecodeError, so it was added unchecked"
        in reply["_meta"]["warnings"]
    )


def test_a_finder_that_dies_silently_is_named_as_such(live, monkeypatch):
    monkeypatch.setattr(ledger_task_duplicates, "CHILD", (sys.executable, "-c", "raise SystemExit(3)"))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (
        "the duplicate check did not run for task t9 because it failed with no message, so it was added unchecked"
        in reply["_meta"]["warnings"]
    )


def test_an_overrun_kills_every_process_the_finder_started(monkeypatch, tmp_path):
    marker = tmp_path / "grandchild.pid"
    spawn = (
        "import subprocess, sys, time; "
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(marker)!r}, 'w').write(str(g.pid)); time.sleep(30)"
    )
    monkeypatch.setattr(ledger_task_duplicates, "CHILD", (sys.executable, "-c", spawn))
    monkeypatch.setattr(ledger_task_duplicates, "BUDGET_S", 2)
    with pytest.raises(subprocess.TimeoutExpired):
        ledger_task_duplicates.find({}, [])
    grandchild = int(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and alive(grandchild):
        time.sleep(0.05)
    assert not alive(grandchild)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/status")
    return not (status.exists() and "\nState:\tZ" in status.read_text())


def test_the_finder_receives_only_the_lists_it_reads(monkeypatch, tmp_path):
    seen = tmp_path / "request.json"
    copy = f"import sys; open({str(seen)!r}, 'w').write(sys.stdin.read()); print('[null]')"
    monkeypatch.setattr(ledger_task_duplicates, "CHILD", (sys.executable, "-c", copy))
    doc = {"tasks": [{"id": "t1"}], "phases": [], "followups": [], "_meta": {"seeds": ["a" * 1000]}}
    assert ledger_task_duplicates.find(doc, [{"task": "t9"}]) == [None]
    sent = json.loads(seen.read_text())
    assert sent == {
        "doc": {"tasks": [{"id": "t1"}], "phases": [], "followups": []},
        "kind": "task",
        "items": [{"task": "t9"}],
    }
    ledger_task_duplicates.find({"tasks": []}, [])
    assert json.loads(seen.read_text())["doc"] == {"tasks": [], "phases": [], "followups": []}


def test_the_server_never_loads_the_classifier_or_its_home_configuration():
    probe = (
        "import sys, scripts.swarm_ledger.ledger_server, scripts.swarm_ledger.api.mutations; "
        "print(sorted(m for m in sys.modules if m.startswith(('hooks.classifier', 'hooks.config'))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=ledger_task_duplicates.ROOT, capture_output=True, text=True, check=True
    ).stdout
    assert out.splitlines()[-1] == "[]"


def finder_main(monkeypatch, capsys, doc, items, judge=None):
    if judge:
        monkeypatch.setattr(ledger_duplicates, "decide", judge)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"doc": doc, "kind": "task", "items": items})))
    ledger_duplicates.main()
    return json.loads(capsys.readouterr().out)


def yes_to_all(state, questions, *, purpose):
    from hooks.classifier import Answer, DecisionResult

    return DecisionResult({name: Answer("noul", noul=0.9) for name in questions}, "unit-test")


def test_the_finder_entry_writes_each_match_as_json(monkeypatch, capsys):
    held = {"tasks": [{"id": "t1", "title": "Publish the wheel to PyPI", "state": "open", "phase": "p1"}]}
    held["phases"] = [{"id": "p1", "title": "Release"}]
    items = [{"task": "t9", "title": "Publish the wheel to PyPI from main"}, {"task": "t8", "title": "Fold chat"}]
    found = finder_main(monkeypatch, capsys, held, items, yes_to_all)
    assert found == [
        {
            "id": "t1",
            "kind": "task",
            "title": "Publish the wheel to PyPI",
            "state": "open",
            "rank": "normal",
            "phase": "p1",
            "phase_title": "Release",
            "probability": 0.9,
        },
        None,
    ]


def test_the_finder_entry_marks_an_unanswered_check(monkeypatch, capsys):
    held = {"tasks": [{"id": "t1", "title": "Publish the wheel to PyPI", "state": "open"}]}
    found = finder_main(monkeypatch, capsys, held, [{"task": "t9", "title": "Publish the wheel to PyPI"}])
    assert found == [ledger_duplicates.UNCHECKED]


def test_the_finder_answer_is_its_last_output_line(monkeypatch):
    monkeypatch.setattr(ledger_task_duplicates, "CHILD", (sys.executable, "-c", "print('noise'); print('[null]')"))
    assert ledger_task_duplicates.find({}, [{"task": "t9"}]) == [None]


def test_the_finder_runs_from_the_repository_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    where = "import json, os; print(json.dumps([os.getcwd()]))"
    monkeypatch.setattr(ledger_task_duplicates, "CHILD", (sys.executable, "-c", where))
    assert ledger_task_duplicates.find({}, []) == [str(ledger_task_duplicates.ROOT)]


def test_a_refusal_without_a_phase_says_none():
    match = {"id": "t1", "title": "Fold chat", "state": "open", "rank": "low", "phase_title": None}
    assert ledger_task_duplicates.refusal({"task": "t9"}, match) == (
        'task t9 repeats task t1 "Fold chat" (open, rank low, phase none): it is on the ledger and will be built. '
        'If it is a different change, add it with --not-duplicate "<why it differs>"'
    )


@pytest.mark.parametrize(
    ("op", "screened"),
    [
        ({"op": "task_add", "by": "swarm", "task": "plan-p1", "phase": "p1"}, False),
        ({"op": "task_add", "by": "swarm", "task": "release-p1", "phase": "p1"}, False),
        ({"op": "task_add", "by": "swarm", "task": "plan-"}, False),
        ({"op": "task_add", "by": "swarm", "task": "plan-p2", "phase": "p1"}, True),
        ({"op": "task_add", "by": "swarm", "task": "t9", "phase": "p1"}, True),
        ({"op": "task_add", "by": MASTER, "task": "plan-p1", "phase": "p1"}, True),
        ({"op": "task_add", "by": MASTER, "task": "t9", "not_duplicate": "a different package"}, False),
        ({"op": "task_update", "by": MASTER, "item": "tasks/t9"}, False),
    ],
)
def test_which_adds_are_screened(op, screened):
    assert ledger_task_duplicates.checked(op) is screened


@pytest.mark.parametrize(
    ("stderr", "error"),
    [("Traceback (most recent call last):\nKeyError: 'answers'\n", "KeyError"), ("ValueError: a: b", "ValueError")],
)
def test_a_dead_finder_is_named_by_its_last_error_line(stderr, error):
    assert ledger_task_duplicates._error(stderr) == error


def test_a_dead_finder_without_output_says_so():
    assert ledger_task_duplicates._error("  \n") == "no message"


@pytest.mark.parametrize("reason", ["", "  ", 5])
def test_a_not_duplicate_reason_must_be_plain_words(reason):
    op = {
        "op": "task_add",
        "id": "x",
        "by": MASTER,
        "task": "t9",
        "title": "Fold chat",
        "lane": "eng",
        "not_duplicate": reason,
    }
    with pytest.raises(ValueError, match="^not_duplicate must say in plain words why the task differs from the one"):
        ledger_tasks.check(op)


def test_a_task_add_without_a_reason_passes_the_check():
    ledger_tasks.check({"op": "task_add", "id": "x", "by": MASTER, "task": "t9", "title": "Fold chat", "lane": "eng"})


def test_the_cli_flag_says_what_it_skips():
    parser = ledger.build_parser()
    args = parser.parse_args(["--slug", SLUG, "--as", MASTER, "task", "add", "t9", "Fold", "chat"])
    assert args.not_duplicate == ""
    text = " ".join(next(a for a in parser._subparsers._actions if a.choices).choices["task"].format_help().split())
    assert (
        "--not-duplicate NOT_DUPLICATE why the task differs from the one it resembles; skips the duplicate check"
        in text
    )


def test_the_page_transport_lands_an_unchecked_add_with_its_warning(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}, error="unavailable"))
    status, reply = admin_put(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert UNANSWERED in reply["_meta"]["warnings"]
    assert stored("t9") is not None
