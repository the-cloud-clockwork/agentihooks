import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import legacy_page

SLUG = "task-repositories"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": []}
    html_path, _ = legacy_page.paths(SLUG, core)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")


def test_task_repositories_survive_add_update_and_clear():
    add = {
        "op": "task_add",
        "id": "add",
        "by": "swarm",
        "task": "t1",
        "title": "bundle fix",
        "lane": "eng",
        "repositories": ["/workspace/agentihooks-bundle"],
    }
    core.check_op(add)
    state, rejected = core.sync(SLUG, ops=[add])
    assert rejected == []
    assert state["tasks"][0]["repositories"] == ["/workspace/agentihooks-bundle"]
    for n, repositories in enumerate((["/workspace/agentihooks", "/workspace/agentihooks-bundle"], [])):
        update = {
            "op": "task_update",
            "id": f"update-{n}",
            "by": "swarm",
            "item": "tasks/t1",
            "fields": {"repositories": repositories},
        }
        core.check_op(update)
        state, rejected = core.sync(SLUG, ops=[update])
        assert rejected == []
        assert state["tasks"][0]["repositories"] == repositories


def test_task_cli_sends_explicit_repositories(monkeypatch):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append((kind, fields)))
    for argv in (
        ["task", "add", "t1", "bundle fix", "--repositories", "/workspace/agentihooks-bundle"],
        ["task", "set", "t1", "repositories=/workspace/agentihooks,/workspace/agentihooks-bundle"],
        ["task", "set", "t1", "repositories="],
    ):
        ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "swarm", *argv]))
    assert sent[0][1]["repositories"] == ["/workspace/agentihooks-bundle"]
    assert sent[1][1]["fields"] == {"repositories": ["/workspace/agentihooks", "/workspace/agentihooks-bundle"]}
    assert sent[2][1]["fields"] == {"repositories": []}


@pytest.mark.parametrize("value", ("bundle", [""], [3], ["relative/repository"]))
def test_task_repositories_require_absolute_local_paths(value):
    with pytest.raises(ValueError) as error:
        core.check_op(
            {"op": "task_update", "id": "bad", "by": "swarm", "item": "tasks/t1", "fields": {"repositories": value}}
        )

    expected = (
        "repositories must list absolute local repository paths"
        if value == ["relative/repository"]
        else "repositories must be a list of nonempty strings"
    )
    assert str(error.value) == expected


def test_repository_publication_branches_are_persisted():
    add = {"op": "task_add", "id": "add", "by": "swarm", "task": "t1", "title": "bundle fix", "lane": "eng"}
    add["repository_branches"] = {"/workspace/agentihooks-bundle": "bundle-fix"}
    core.check_op(add)
    initial, refused = core.sync(SLUG, ops=[add])
    assert refused == []
    assert initial["tasks"][0]["repository_branches"] == {"/workspace/agentihooks-bundle": "bundle-fix"}
    branches = {"/workspace/agentihooks": "hooks-fix", "/workspace/agentihooks-bundle": "bundle-fix"}
    update = {
        "op": "task_update",
        "id": "update",
        "by": "swarm",
        "item": "tasks/t1",
        "fields": {"repository_branches": branches},
    }
    core.check_op(update)
    state, rejected = core.sync(SLUG, ops=[update])
    assert rejected == []
    assert state["tasks"][0]["repository_branches"] == branches


@pytest.mark.parametrize(
    "branches", ([], {"relative": "branch"}, {"/repo": None}, {"/repo": ""}, {"/repo": "bad branch"})
)
def test_repository_publication_branches_are_validated(branches):
    with pytest.raises(ValueError) as error:
        core.check_op(
            {
                "op": "task_update",
                "id": "bad",
                "by": "swarm",
                "item": "tasks/t1",
                "fields": {"repository_branches": branches},
            }
        )

    assert str(error.value) == "repository_branches must map absolute local repository paths to branch names"


def test_task_help_explains_explicit_repository_selection(capsys):
    with pytest.raises(SystemExit) as exit_info:
        ledger.build_parser().parse_args(["--slug", SLUG, "task", "--help"])
    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    text = output.split("\n  --repositories REPOSITORIES", 1)[1].split("\n  --rank", 1)[0]
    assert " ".join(text.split()) == "comma separated absolute local repository paths; defaults to the swarm repository"
