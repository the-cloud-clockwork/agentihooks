import shlex
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks.context import conditions
from scripts.swarm import naming, publish

pytestmark = pytest.mark.unit

FEATURE = "conditions-publish-task-worktree"
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
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", agent.name)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REPO", str(primary))
    monkeypatch.chdir(primary)
    return roots


def _command(*files: str) -> str:
    return shlex.join(["agentihooks", "swarm", SLUG, "publish", "--message", "Fix condition", "--", *files])


def _guard(command: str, cwd: Path) -> str | None:
    return conditions.write_guard("Bash", {"command": command}, SID, str(cwd))


@pytest.mark.feature(FEATURE)
def test_publish_resolves_relative_conditions_in_the_recorded_worktree(checkouts: dict[str, Path]) -> None:
    assert _guard(_command(FILE), checkouts["primary"]) is None


@pytest.mark.feature(FEATURE)
def test_publish_uses_the_session_worktree_before_the_recorded_worktree(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conditions, "_live_dirs", lambda: [checkouts["recorded"] / ".claude" / "conditions"])
    assert _guard(_command(FILE), checkouts["current"]) is None


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("target", ["primary", "live"])
def test_publish_keeps_live_and_dev_condition_paths_refused(checkouts: dict[str, Path], target: str) -> None:
    assert _guard(_command(str(checkouts[target] / FILE)), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature(FEATURE)
def test_publish_keeps_a_live_layer_inside_the_selected_worktree_refused(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conditions, "_live_dirs", lambda: [checkouts["recorded"] / ".claude" / "conditions"])
    assert _guard(_command(FILE), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("command", [f"touch {FILE}", f"git add -- {FILE}", f"echo true > {FILE}"])
def test_other_commands_keep_session_directory_resolution(checkouts: dict[str, Path], command: str) -> None:
    assert _guard(command, checkouts["primary"]) == conditions.GATE_MESSAGE
    assert _guard(command, checkouts["current"]) is None


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("prefix", ["echo ", "other "])
def test_publish_text_inside_another_command_keeps_current_resolution(checkouts: dict[str, Path], prefix: str) -> None:
    assert _guard(prefix + _command(FILE), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("operator", [";", "&&", "||", "|", "\n"])
def test_publish_does_not_exempt_an_adjacent_condition_write(checkouts: dict[str, Path], operator: str) -> None:
    command = f"{_command(FILE)} {operator} touch {FILE}"
    assert _guard(command, checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature(FEATURE)
def test_publish_lookup_failure_keeps_the_guard_closed(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publish, "LedgerClient", Mock(side_effect=RuntimeError("unavailable")))
    assert _guard(_command(FILE), checkouts["primary"]) == conditions.GATE_MESSAGE


@pytest.mark.feature(FEATURE)
def test_publish_without_condition_paths_does_not_read_task_state(
    checkouts: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = Mock(side_effect=AssertionError("unexpected task lookup"))
    monkeypatch.setattr(publish, "LedgerClient", lookup)
    assert _guard(_command("README.md"), checkouts["primary"]) is None
    lookup.assert_not_called()
