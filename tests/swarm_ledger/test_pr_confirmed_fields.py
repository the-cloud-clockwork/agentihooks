"""The ledger task update takes the confirmed pull request number and head that swarm pr records."""

from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_tasks, new_ledger
from tests.swarm_ledger import legacy_page

SLUG = "prconfirmed-2026-01-01"
ENGINEER = "engineer@abcdef-0002"
HEAD = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = legacy_page.paths(SLUG, core)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed", "by": "swarm", "task": "t1", "title": "a", "lane": "eng"}])


def op(fields: dict) -> dict:
    return {"op": "task_update", "id": "pr-1", "by": ENGINEER, "item": "tasks/t1", "fields": fields}


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
def test_a_task_update_records_the_confirmed_pull_request_number_and_head() -> None:
    fields = {"state": "pr", "pr_url": "https://github.com/o/r/pull/7", "pr_number": 7, "pr_head": HEAD}
    core.check_op(op(fields))
    state, _ = core.sync(SLUG, ops=[op(fields)])
    task = next(t for t in state["tasks"] if t["id"] == "t1")
    assert (task["state"], task["pr_number"], task["pr_head"]) == ("pr", 7, HEAD)


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
@pytest.mark.parametrize(
    "fields",
    [
        {"state": "pr", "pr_number": 11024, "pr_head": HEAD},
        {"pr_number": 1},
        {"pr_head": HEAD},
        {"pr_head": ""},
        {"state": "pr", "pr_url": "https://github.com/o/r/pull/7"},
    ],
)
def test_the_ledger_check_takes_the_fields_swarm_pr_writes(fields: dict) -> None:
    ledger_tasks.check(op(fields))


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
@pytest.mark.parametrize(
    "fields,refusal",
    [
        ({"pr_number": "7"}, "pr_number must be a positive integer"),
        ({"pr_number": True}, "pr_number must be a positive integer"),
        ({"pr_number": 0}, "pr_number must be a positive integer"),
        ({"pr_number": -3}, "pr_number must be a positive integer"),
        ({"pr_number": 7.5}, "pr_number must be a positive integer"),
        ({"pr_head": 7}, "task_update may set only"),
        ({"pr_head": "not a hash"}, "pr_head must be a lowercase commit hash"),
        ({"pr_head": "ABCDEF0123"}, "pr_head must be a lowercase commit hash"),
        ({"pr_head": "0123456789abcdef\n"}, "pr_head must be a lowercase commit hash"),
    ],
)
def test_a_task_update_refuses_a_malformed_pull_request_number_or_head(fields: dict, refusal: str) -> None:
    with pytest.raises(ValueError, match=refusal):
        ledger_tasks.check(op(fields))


@pytest.mark.feature("ledger-task-update-takes-confirmed-pull-request")
@pytest.mark.parametrize("bad", [7, None])
def test_the_head_check_refuses_a_head_that_is_no_string(bad: object) -> None:
    with pytest.raises(ValueError, match="pr_head must be a lowercase commit hash"):
        ledger_tasks.check_confirmed_pull({"pr_head": bad})
