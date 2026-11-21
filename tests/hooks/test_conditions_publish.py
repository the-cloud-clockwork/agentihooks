import shlex
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks.context import conditions
from scripts.swarm import naming, publish

pytestmark = pytest.mark.unit

SLUG = "publish-proof"
SID = "conditions-publish-proof"
FILE = ".claude/conditions/pre-bash-proof.sh"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "dev")
    _git(
        primary,
        "-c",
        "user.name=Proof",
        "-c",
        "user.email=proof@example.test",
        "commit",
        "--allow-empty",
        "-qm",
        "seed",
    )
    _git(primary, "remote", "add", "origin", "https://github.com/acme/project.git")
    agent = publish.AgentRecord(name="engineer@123-1", lane="eng", task="fix")
    roots = {"primary": primary, "recorded": tmp_path / "recorded", "current": tmp_path / "current"}
    for key, branch in (("recorded", "recorded"), ("current", naming.plain(agent.name))):
        _git(primary, "worktree", "add", "-qb", branch, str(roots[key]))
    live = tmp_path / "live"
    live.mkdir()
    roots["live"] = live
    for root in roots.values():
        path = root / FILE
        path.parent.mkdir(parents=True)
        path.write_text("true\n")
    task = {"id": agent.task, "claimed_by": agent.name, "state": "claimed", "branch": "recorded"}
    ledger = Mock()
    ledger.task.return_value = task
    monkeypatch.setattr(publish, "LedgerClient", Mock(return_value=ledger))
    monkeypatch.setattr(conditions, "_live_dirs", lambda: [live / ".claude" / "conditions"])
    monkeypatch.setattr(conditions, "is_armed", lambda session_id: False)
    monkeypatch.setattr(conditions, "_requested_on_task", lambda session_id: False)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", agent.task)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", agent.lane)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", agent.name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REPO", str(primary))
    monkeypatch.chdir(primary)
    return roots


WRAPPER = "/usr/bin/python3 -m scripts.github_app_cli exec "


def _command(*files: str) -> str:
    return shlex.join(["agentihooks", "swarm", SLUG, "publish", "--message", "Fix condition", "--", *files])


def _wrapped(*files: str) -> str:
    return WRAPPER + _command(*files)


def _guard(command: str, cwd: Path | None) -> str | None:
    return conditions.write_guard("Bash", {"command": command}, SID, None if cwd is None else str(cwd))


@pytest.mark.feature("conditions-publish-task-worktree")
def test_publish_resolves_relative_conditions_in_the_recorded_worktree(checkouts: dict[str, Path]) -> None:
    command = _command(FILE)
    assert conditions._staged_command(command, str(checkouts["primary"]))
    assert _guard(command, checkouts["primary"]) is None


@pytest.mark.feature("conditions-publish-task-worktree")
@pytest.mark.parametrize("wrapper", [WRAPPER, "python -m scripts.github_app_cli exec -- "])
def test_publish_through_the_github_app_wrapper_uses_the_task_worktree(
    checkouts: dict[str, Path], wrapper: str
) -> None:
    assert _guard(wrapper + _command(FILE), checkouts["primary"]) is None


@pytest.mark.feature("conditions-publish-task-worktree")
def test_publish_outside_the_repository_uses_the_swarm_repository(checkouts: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    assert _guard(_wrapped(FILE), outside) is None


@pytest.mark.feature("conditions-publish-task-worktree")
def test_publish_checks_every_named_file_in_the_task_worktree(checkouts: dict[str, Path]) -> None:
    assert _guard(_wrapped(FILE, "README.md"), checkouts["primary"]) is None
    assert _guard(_wrapped(FILE, str(checkouts["live"] / FILE)), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature("conditions-publish-task-worktree")
def test_publish_uses_the_session_worktree_before_the_recorded_worktree(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conditions, "_live_dirs", lambda: [checkouts["recorded"] / ".claude" / "conditions"])
    assert _guard(_wrapped(FILE), checkouts["current"]) is None


@pytest.mark.feature("conditions-publish-task-worktree")
@pytest.mark.parametrize("target", ["primary", "live"])
def test_publish_keeps_live_and_dev_condition_paths_refused(checkouts: dict[str, Path], target: str) -> None:
    assert _guard(_wrapped(str(checkouts[target] / FILE)), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature("conditions-publish-task-worktree")
def test_publish_keeps_a_live_layer_inside_the_selected_worktree_refused(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conditions, "_live_dirs", lambda: [checkouts["recorded"] / ".claude" / "conditions"])
    assert _guard(_wrapped(FILE), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature("conditions-publish-task-worktree")
def test_publish_lookup_failure_keeps_the_guard_closed(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publish, "LedgerClient", Mock(side_effect=RuntimeError("unavailable")))
    assert _guard(_wrapped(FILE), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature("conditions-publish-task-worktree")
@pytest.mark.parametrize(
    ("variable", "value"),
    [("AGENTIHOOKS_SWARM", "other-swarm"), ("AGENTIHOOKS_AGENT_NAME", ""), ("AGENTIHOOKS_SWARM_LANE", None)],
)
def test_publish_for_another_session_keeps_the_guard_closed(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch, variable: str, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)
    assert _guard(_wrapped(FILE), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature("conditions-publish-task-worktree")
def test_publish_without_a_session_folder_keeps_the_guard_closed(checkouts: dict[str, Path]) -> None:
    assert _guard(_wrapped(FILE), None) == conditions.GATE_MESSAGE


@pytest.mark.feature("conditions-publish-task-worktree")
@pytest.mark.parametrize("command", [f"touch {FILE}", f"echo true > {FILE}"])
def test_other_commands_keep_session_directory_resolution(checkouts: dict[str, Path], command: str) -> None:
    assert _guard(command, checkouts["primary"]) == conditions.GATE_MESSAGE
    assert _guard(command, checkouts["current"]) is None


@pytest.mark.feature("conditions-publish-task-worktree")
@pytest.mark.parametrize("prefix", ["other ", "false "])
def test_publish_text_inside_another_command_keeps_current_resolution(checkouts: dict[str, Path], prefix: str) -> None:
    assert _guard(prefix + _command(FILE), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature("conditions-publish-task-worktree")
@pytest.mark.parametrize("operator", [";", "&&", "||", "|", "\n"])
def test_publish_does_not_exempt_an_adjacent_condition_write(checkouts: dict[str, Path], operator: str) -> None:
    for command in (_command(FILE), _wrapped(FILE)):
        assert _guard(f"{command} {operator} touch {FILE}", checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature("conditions-publish-task-worktree")
def test_read_only_commands_keep_their_existing_admission(checkouts: dict[str, Path]) -> None:
    for command in (f"git add -- {FILE}", "echo " + _command(FILE), _command(str(checkouts["primary"] / FILE))):
        assert _guard(command, checkouts["primary"]) is None


@pytest.mark.feature("conditions-publish-task-worktree")
def test_publish_without_condition_paths_does_not_read_task_state(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = Mock(side_effect=AssertionError("unexpected task lookup"))
    monkeypatch.setattr(publish, "LedgerClient", lookup)
    command = WRAPPER + shlex.join(
        ["agentihooks", "swarm", SLUG, "publish", "--message", f"Explain {FILE}", "README.md"]
    )
    assert _guard(command, checkouts["primary"]) == conditions.GATE_MESSAGE
    lookup.assert_not_called()


@pytest.mark.feature("conditions-publish-task-worktree")
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("agentihooks swarm s publish --message m -- a --b", ("s", ["a", "--b"])),
        ("agentihooks swarm --as n s publish -m m a b", ("s", ["a", "b"])),
        ("agentihooks swarm --as=n s publish --message=m --via lab --direct a", ("s", ["a"])),
        ("agentihooks swarm s publish --via=lab -m 'm n' a", ("s", ["a"])),
        ("/usr/bin/python3 -m scripts.github_app_cli exec agentihooks swarm s publish -m m a", ("s", ["a"])),
        ("python -m scripts.github_app_cli exec -- agentihooks swarm s publish -m m a", ("s", ["a"])),
        ("python -m scripts.github_app_cli exec -- /tmp/x/agentihooks swarm s publish -m m a", None),
        ("./agentihooks swarm s publish -m m a", None),
        ("python-evil -m scripts.github_app_cli exec agentihooks swarm s publish -m m a", None),
        ("agentihooks swarm s publish -m m -- ~/x", None),
        ("agentihooks swarm s publish -m m a > b", None),
        ("agentihooks swarm s publish -m `id` a", None),
        ("agentihooks swarm s publish -m m {a,b}", None),
        ("agentihooks swarm s prepare", None),
        ("agentihooks swarm s publish -x a", None),
        ("agentihooks swarm s publish -mm a", None),
        ("agentihooks swarm s publish -m m a; touch b", None),
        ('agentihooks swarm s publish -m "$(touch b)" a', None),
        ("agentihooks swarm s publish -m m .claude/conditions/*", None),
        ("agentihooks swarm s publish -m 'm", None),
        ("agentihooks swarm --as", None),
        ("agentihooks msg send x y", None),
        ("python -m scripts.other exec agentihooks swarm s publish -m m a", None),
        ("python -m scripts.github_app_cli exec", None),
        ("bash -m scripts.github_app_cli exec agentihooks swarm s publish -m m a", None),
        ("other swarm s publish -m m a", None),
    ],
)
def test_publish_call_reads_the_slug_and_file_names(command: str, expected: tuple[str, list[str]] | None) -> None:
    assert conditions._publish_call(command) == expected
