import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "taskprofile-2026-01-01"
REFUSED = "^profile must be a profile name such as frontend, or empty for the lane profile$"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed", "by": "swarm", "task": "t1", "title": "a", "lane": "eng"}])


def op(kind, n, **fields):
    return {"op": kind, "id": f"{kind}-{n}", "by": "swarm", **fields}


def test_a_task_keeps_the_profile_its_claimant_runs():
    add = op("task_add", 1, task="t2", title="b", lane="eng", profile="frontend")
    set_back = op("task_update", 2, item="tasks/t2", fields={"profile": ""})
    core.check_op(add)
    core.check_op(set_back)
    state, rejected = core.sync(SLUG, ops=[add])
    assert (rejected, state["tasks"][1]["profile"]) == ([], "frontend")
    assert "profile" not in state["tasks"][0]
    state, rejected = core.sync(SLUG, ops=[set_back])
    assert (rejected, state["tasks"][1]["profile"]) == ([], "")


@pytest.mark.parametrize(
    "bad",
    [
        op("task_add", 3, task="t3", title="c", lane="eng", profile="--resume"),
        op("task_add", 4, task="t3", title="c", lane="eng", profile=["frontend"]),
        op("task_add", 5, task="t3", title="c", lane="eng", profile=None),
        op("task_update", 6, item="tasks/t1", fields={"profile": "front end"}),
    ],
)
def test_a_profile_that_is_not_a_plain_name_is_refused(bad):
    with pytest.raises(ValueError, match=REFUSED):
        core.check_op(bad)


def test_a_seeded_task_with_a_bad_profile_is_refused():
    base = {"title": "t", "phases": [], "questions": [], "followups": []}
    with pytest.raises(ValueError, match=REFUSED):
        core.validate({**base, "tasks": [{"id": "t1", "title": "a", "profile": "-x"}]})


def test_task_cli_sends_the_profile(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((kind, f)))
    for argv in (
        ["task", "add", "t3", "b", "--profile", "frontend"],
        ["task", "add", "t4", "c"],
        ["task", "set", "t4", "profile=frontend"],
    ):
        ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "liaison", *argv]))
    assert [f.get("profile") for _, f in sent[:2]] == ["frontend", None]
    assert sent[2][1]["fields"] == {"profile": "frontend"}
