import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import legacy_page

SLUG = "taskharness-2026-01-01"
REFUSED = "^requires_harness must be claude or codex, or empty for any harness$"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = legacy_page.paths(SLUG, core)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed", "by": "swarm", "task": "t1", "title": "a", "lane": "eng"}])


def op(kind, n, **fields):
    return {"op": kind, "id": f"{kind}-{n}", "by": "swarm", **fields}


@pytest.mark.feature("task-requires-harness")
def test_a_task_keeps_the_harness_it_requires():
    add = op("task_add", 1, task="t2", title="b", lane="eng", requires_harness="claude")
    set_back = op("task_update", 2, item="tasks/t2", fields={"requires_harness": ""})
    core.check_op(add)
    core.check_op(set_back)
    state, rejected = core.sync(SLUG, ops=[add])
    assert (rejected, state["tasks"][1]["requires_harness"]) == ([], "claude")
    assert "requires_harness" not in state["tasks"][0]
    state, rejected = core.sync(SLUG, ops=[set_back])
    assert (rejected, state["tasks"][1]["requires_harness"]) == ([], "")


@pytest.mark.feature("task-requires-harness")
@pytest.mark.parametrize(
    "bad",
    [
        op("task_add", 3, task="t3", title="c", lane="eng", requires_harness="copilot"),
        op("task_add", 4, task="t3", title="c", lane="eng", requires_harness=["claude"]),
        op("task_update", 5, item="tasks/t1", fields={"requires_harness": "Claude"}),
    ],
)
def test_a_harness_other_than_claude_or_codex_is_refused(bad):
    with pytest.raises(ValueError, match=REFUSED):
        core.check_op(bad)


@pytest.mark.feature("task-requires-harness")
def test_task_cli_sends_the_required_harness(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((kind, f)))
    for argv in (
        ["task", "add", "t3", "b", "--requires-harness", "claude"],
        ["task", "add", "t4", "c"],
        ["task", "set", "t4", "requires_harness=codex"],
    ):
        ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "liaison", *argv]))
    assert [f.get("requires_harness") for _, f in sent[:2]] == ["claude", None]
    assert sent[2][1]["fields"] == {"requires_harness": "codex"}
