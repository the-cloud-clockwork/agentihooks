import pytest

from hooks.targets import normalizer
from profiles.package.roles._guards import no_code_edits


@pytest.fixture
def task_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "test-swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "task1")
    return tmp_path


def task_root(home):
    return home / ".agentihooks" / "swarm" / "test-swarm" / "tasks" / "task1"


def reason(home, target, role="planner"):
    return no_code_edits.deny_reason(
        {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(home / "repo")},
        role,
        home,
    )


@pytest.mark.parametrize("role", ["master", "planner", "qa"])
@pytest.mark.parametrize("file", ["progress.md", "proof.md", "nested/notes.md"])
def test_role_can_write_its_own_task_folder(task_home, role, file):
    assert reason(task_home, task_root(task_home) / file, role) == ""


@pytest.mark.parametrize(
    "target",
    ["task10/progress.md", "task2/proof.md", "../state.json", "task1/../task2/progress.md"],
)
def test_other_swarm_paths_stay_denied(task_home, target):
    assert reason(task_home, task_root(task_home).parent / target)


def test_repository_stays_denied(task_home):
    assert reason(task_home, task_home / "repo" / "module.py")


@pytest.mark.parametrize("key", ["AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK"])
@pytest.mark.parametrize("value", [None, "", "..", ".", "../other", "/absolute", "a/b"])
def test_missing_or_unsafe_identity_never_opens_swarm_folder(task_home, monkeypatch, key, value):
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    assert reason(task_home, task_root(task_home) / "progress.md")
    roots = no_code_edits._allowed_roots("qa", task_home)
    assert roots == [(task_home / "scratchpad").resolve()]


def test_task_symlink_cannot_escape(task_home):
    task_root(task_home).mkdir(parents=True)
    repo = task_home / "repo"
    repo.mkdir()
    (task_root(task_home) / "linked").symlink_to(repo, target_is_directory=True)
    assert reason(task_home, task_root(task_home) / "linked" / "module.py")


@pytest.mark.parametrize(
    "prefix", [".agentihooks", ".agentihooks/swarm/test-swarm/tasks", ".agentihooks/swarm/test-swarm/tasks/task1"]
)
def test_task_root_symlink_never_opens_repository(task_home, prefix):
    repo = task_home / "repo"
    repo.mkdir()
    link = task_home / prefix
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(repo, target_is_directory=True)
    assert reason(task_home, repo / "module.py")
    assert reason(task_home, task_root(task_home) / "progress.md")


def test_master_marker_does_not_open_a_task_folder(task_home, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "master")
    assert reason(task_home, task_root(task_home).parent / "master" / "progress.md")


def patch_payload(home, headers, inputs=None):
    body = "*** Begin Patch\n" + "\n".join(headers) + "\n*** End Patch"
    args = {"patch": body, **(inputs or {})}
    tool, args = normalizer._codex_tool_call("apply_patch", args)
    return {"tool_name": tool, "tool_input": args, "cwd": str(home / "repo")}


@pytest.mark.parametrize("operation", ["Add", "Update", "Delete"])
def test_patch_carries_every_target(task_home, operation):
    first = task_home / "scratchpad" / "notes.md"
    second = task_home / "repo" / "module.py"
    payload = patch_payload(task_home, [f"*** Add File: {first}", f"*** {operation} File: {second}"])
    assert payload["tool_input"]["file_path"] == str(first)
    assert payload["tool_input"]["file_paths"] == [str(first), str(second)]
    assert no_code_edits.deny_reason(payload, "planner", task_home)


def test_patch_move_destination_is_judged(task_home):
    source = task_root(task_home) / "progress.md"
    destination = task_home / "repo" / "module.py"
    payload = patch_payload(task_home, [f"*** Update File: {source}", f"*** Move to: {destination}"])
    assert payload["tool_input"]["file_paths"] == [str(source), str(destination)]
    assert no_code_edits.deny_reason(payload, "planner", task_home)


def test_all_allowed_patch_targets_pass(task_home):
    first = task_home / "scratchpad" / "notes.md"
    second = task_root(task_home) / "progress.md"
    third = task_root(task_home) / "proof.md"
    payload = patch_payload(
        task_home, [f"*** Add File: {first}", f"*** Update File: {second}", f"*** Move to: {third}"]
    )
    assert no_code_edits.deny_reason(payload, "planner", task_home) == ""


def test_supplied_paths_cannot_hide_patch_targets(task_home):
    allowed = str(task_home / "scratchpad" / "notes.md")
    denied = str(task_home / "repo" / "module.py")
    payload = patch_payload(task_home, [f"*** Delete File: {denied}"], {"file_path": allowed, "file_paths": [allowed]})
    assert payload["tool_input"]["file_path"] == allowed
    assert payload["tool_input"]["file_paths"] == [denied]
    assert no_code_edits.deny_reason(payload, "planner", task_home)


def test_patch_without_targets_is_denied(task_home):
    assert no_code_edits.deny_reason(patch_payload(task_home, []), "planner", task_home)


def test_relative_patch_target_is_resolved_against_cwd(task_home):
    allowed = task_home / "scratchpad" / "notes.md"
    payload = patch_payload(task_home, [f"*** Add File: {allowed}", "*** Delete File: module.py"])
    assert no_code_edits.deny_reason(payload, "planner", task_home)
