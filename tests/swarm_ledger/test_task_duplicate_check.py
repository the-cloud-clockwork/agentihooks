import inspect
import json
import threading
import uuid
from unittest.mock import patch

import pytest

from hooks.classifier import Answer, ClassifierUnavailable, DecisionResult
from scripts.swarm.ledger_client import LedgerClient, LedgerRefused
from scripts.swarm_ledger import ledger_duplicates, ledger_task_duplicates
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
    def __init__(self, yes=(), error=None, hold=None):
        self.yes, self.error, self.hold, self.asked, self.lock_free = set(yes), error, hold, [], []

    def __call__(self, state, questions, *, purpose):
        self.asked.append(purpose)
        self.lock_free.append(core.LOCK.acquire(blocking=False) and (core.LOCK.release() or True))
        if self.hold:
            self.hold.wait(5)
        if self.error:
            raise self.error
        return DecisionResult(
            {
                name: Answer("noul", noul=0.9 if any(f" {i} " in q.instructions for i in self.yes) else 0.05)
                for name, q in questions.items()
            },
            "unit-test",
        )


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
    monkeypatch.setattr(ledger_duplicates, "decide", judge)
    return judge


def operations(live, *ops):
    body = {"operation_id": uuid.uuid4().hex, "ops": list(ops), "guards": guards(live)}
    headers = {"X-Ledger-Token": live["admin"], "Content-Type": "application/json"}
    status, data, _ = send(live, "POST", f"/api/v1/ledgers/{SLUG}/operations", json.dumps(body).encode(), **headers)
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
    judged(monkeypatch, Judge(yes={"t1"}, error=ClassifierUnavailable("down")))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert UNANSWERED in reply["_meta"]["warnings"]
    assert stored("t9")["title"] == REPEAT


def test_a_classifier_past_its_budget_lands_the_task_with_a_warning(live, monkeypatch):
    hold = threading.Event()
    judged(monkeypatch, Judge(yes={"t1"}, hold=hold))
    monkeypatch.setattr(ledger_task_duplicates, "BUDGET_S", 0.05)
    try:
        status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    finally:
        hold.set()
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
    judged(monkeypatch, Judge(error=ClassifierUnavailable("down")))
    cli("t9", REPEAT, "--phase", live["phases"][0])
    assert UNANSWERED in capsys.readouterr().err
    assert stored("t9") is not None


def test_an_empty_not_duplicate_reason_is_refused(live):
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0], not_duplicate=" "))
    assert status == 400 and "not_duplicate" in reply["error"]["message"]


def test_a_broken_check_lands_the_task_naming_its_error(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}, error=KeyError("answers")))
    status, reply = operations(live, task("t9", REPEAT, live["phases"][0]))
    assert (status, reply["rejected"]) == (200, [])
    assert (
        "the duplicate check did not run for task t9 because it failed with KeyError, so it was added unchecked"
        in reply["_meta"]["warnings"]
    )
    assert stored("t9") is not None


def test_a_timeout_error_inside_the_check_is_named_not_counted_as_slow(live, monkeypatch):
    judged(monkeypatch, Judge(yes={"t1"}, error=TimeoutError("socket")))
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
