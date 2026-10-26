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


def test_task_branch_setting_updates_the_swarm_repository_map(monkeypatch):
    from types import SimpleNamespace

    sent = []
    task = {"repository_branches": {"/workspace/agentihooks": "deleted", "/workspace/bundle": "bundle-work"}}
    store = SimpleNamespace(config=lambda slug: {SLUG: SimpleNamespace(repo="/workspace/agentihooks")}[slug])
    monkeypatch.setattr(ledger.ports, "swarm", lambda: SimpleNamespace(store=SimpleNamespace(connect=lambda: store)))
    monkeypatch.setattr(ledger, "resource", lambda slug, path: {(SLUG, "tasks/t1"): task}[slug, path])
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append((kind, fields)))
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "swarm", "task", "set", "t1", "branch=dev"])
    ledger.cmd_task(args)
    assert sent == [
        (
            "task_update",
            {
                "item": "tasks/t1",
                "fields": {
                    "branch": "dev",
                    "repository_branches": {"/workspace/agentihooks": "dev", "/workspace/bundle": "bundle-work"},
                },
            },
        )
    ]


def test_clearing_task_branch_removes_only_the_swarm_repository_entry(monkeypatch):
    from types import SimpleNamespace

    sent = []
    store = SimpleNamespace(config=lambda slug: SimpleNamespace(repo="/workspace/agentihooks"))
    monkeypatch.setattr(ledger.ports, "swarm", lambda: SimpleNamespace(store=SimpleNamespace(connect=lambda: store)))
    monkeypatch.setattr(
        ledger,
        "resource",
        lambda slug, path: {
            "repository_branches": {"/workspace/agentihooks": "deleted", "/workspace/bundle": "bundle-work"}
        },
    )
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append(fields))
    args = ledger.build_parser().parse_args(
        [
            "--slug",
            SLUG,
            "--as",
            "swarm",
            "task",
            "set",
            "t1",
            "branch=",
            "repository_branches./workspace/extra=extra-work",
        ]
    )
    ledger.cmd_task(args)
    assert sent[0]["fields"] == {
        "branch": "",
        "repository_branches": {"/workspace/bundle": "bundle-work", "/workspace/extra": "extra-work"},
    }


def test_branch_setting_on_a_ledger_without_a_swarm_keeps_its_original_fields(monkeypatch):
    from types import SimpleNamespace

    def missing(slug):
        raise ValueError("no swarm")

    store = SimpleNamespace(config=missing)
    monkeypatch.setattr(
        ledger.ports,
        "swarm",
        lambda: SimpleNamespace(store=SimpleNamespace(connect=lambda: store, SwarmError=ValueError)),
    )
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append(fields))
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "swarm", "task", "set", "t1", "branch=work"])
    ledger.cmd_task(args)
    assert sent == [{"item": "tasks/t1", "fields": {"branch": "work"}}]


@pytest.mark.parametrize("branch,expected", [("work", {"/workspace/agentihooks": "work"}), ("", {})])
def test_task_branch_setting_starts_with_an_empty_repository_map(monkeypatch, branch, expected):
    from types import SimpleNamespace

    store = SimpleNamespace(config=lambda slug: {SLUG: SimpleNamespace(repo="/workspace/agentihooks")}[slug])
    monkeypatch.setattr(ledger.ports, "swarm", lambda: SimpleNamespace(store=SimpleNamespace(connect=lambda: store)))
    monkeypatch.setattr(ledger, "resource", lambda slug, path: {(SLUG, "tasks/t1"): {}}[slug, path])
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent.append(fields))
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "swarm", "task", "set", "t1", f"branch={branch}"])
    ledger.cmd_task(args)
    assert sent == [{"item": "tasks/t1", "fields": {"branch": branch, "repository_branches": expected}}]


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


@pytest.mark.parametrize("corrected", ["recorded", ""])
def test_a_corrected_top_level_branch_keeps_its_repository_entry_in_step(corrected):
    add = {"op": "task_add", "id": "add", "by": "swarm", "task": "t1", "title": "continue", "lane": "ci"}
    add["repository_branches"] = {"/workspace/agentihooks": "predecessor", "/workspace/bundle": "bundle-work"}
    core.check_op(add)
    core.sync(SLUG, ops=[add])
    first = {"op": "task_update", "id": "first", "by": "swarm", "item": "tasks/t1", "fields": {"branch": "predecessor"}}
    core.check_op(first)
    core.sync(SLUG, ops=[first])
    fix = {"op": "task_update", "id": "fix", "by": "swarm", "item": "tasks/t1", "fields": {"branch": corrected}}
    core.check_op(fix)
    state, rejected = core.sync(SLUG, ops=[fix])
    assert rejected == []
    expected = {"/workspace/bundle": "bundle-work"}
    if corrected:
        expected["/workspace/agentihooks"] = corrected
    assert state["tasks"][0]["branch"] == corrected
    assert state["tasks"][0]["repository_branches"] == expected


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
