import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core

SLUG = "taskoverlays-2026-01-01"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = core.paths(SLUG)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed", "by": "swarm", "task": "t1", "title": "a", "lane": "eng"}])


def op(kind, n, **fields):
    return {"op": kind, "id": f"{kind}-{n}", "by": "swarm", **fields}


def test_a_task_keeps_the_overlays_its_claimant_wears():
    add = op("task_add", 1, task="t2", title="b", lane="eng", overlays=["tuner", "trader"])
    clear = op("task_update", 2, item="tasks/t2", fields={"overlays": []})
    core.check_op(add)
    core.check_op(clear)
    state, rejected = core.sync(SLUG, ops=[add])
    assert (rejected, state["tasks"][1]["overlays"]) == ([], ["tuner", "trader"])
    assert "overlays" not in state["tasks"][0]
    state, rejected = core.sync(SLUG, ops=[clear])
    assert (rejected, state["tasks"][1]["overlays"]) == ([], [])


@pytest.mark.parametrize(
    "bad,message",
    [
        (
            op("task_update", 3, item="tasks/t1", fields={"overlays": ["a", "b", "c", "d"]}),
            "overlays lists at most 3 overlay names",
        ),
        (
            op("task_update", 4, item="tasks/t1", fields={"overlays": "tuner"}),
            "overlays must be a list of nonempty strings",
        ),
        (
            op("task_add", 5, task="t3", title="c", lane="eng", overlays=["a", "b", "c", "d"]),
            "overlays lists at most 3 overlay names",
        ),
    ],
)
def test_more_than_three_overlays_or_a_bare_name_is_refused(bad, message):
    with pytest.raises(ValueError) as refused:
        core.check_op(bad)
    assert str(refused.value) == message


def test_task_cli_sends_the_overlays_as_a_list(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **f: sent.append((kind, f)))
    for argv in (
        ["task", "add", "t3", "b", "--overlays", "tuner, trader"],
        ["task", "add", "t4", "c"],
        ["task", "set", "t4", "overlays=scout"],
        ["task", "set", "t4", "overlays="],
    ):
        ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "liaison", *argv]))
    assert [f.get("overlays") for _, f in sent[:2]] == [["tuner", "trader"], None]
    assert [f["fields"] for _, f in sent[2:]] == [{"overlays": ["scout"]}, {"overlays": []}]
