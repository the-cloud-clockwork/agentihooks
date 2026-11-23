"""The live check of the expert role profile over the shipped role and over a damaged copy of it."""

import shutil
from pathlib import Path

import pytest

from hooks.context import profile_chain
from scripts.feature_checks import expert_role as check

pytestmark = pytest.mark.unit

ROLES = Path(__file__).resolve().parents[1] / "profiles" / "package" / "roles"
HELD = [
    "the expert role holds its persona",
    "the expert role holds its rule",
    "the expert role holds its loop skill",
    "the expert role holds its handoff skill",
    "the expert role holds its stop gate",
    "the expert role holds its identity pin",
    "the expert role holds its prompt guard",
]
STOP = ".claude/conditions/stop-expert_stop.py"
GUARD = ".claude/conditions/pre-bash.rm+bash.rmdir+bash.bash+bash.sh+bash.zsh+bash.eval-prompt_guard.py"


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    shutil.copytree(ROLES / "expert", tmp_path / "expert")
    return tmp_path


@pytest.mark.feature("expert-role-profile")
def test_the_shipped_expert_role_holds_every_part_and_each_is_listed(capsys: pytest.CaptureFixture[str]) -> None:
    assert profile_chain.PACKAGE_ROLES.resolve() == ROLES
    assert check.main() == 0
    assert capsys.readouterr().out.splitlines() == HELD


@pytest.mark.feature("expert-role-profile")
@pytest.mark.parametrize(
    "path,part,line",
    [
        ("CLAUDE.md", "persona", 0),
        (".claude/rules/expert-role.md", "rule", 1),
        (".claude/skills/swarm-expert/SKILL.md", "loop skill", 2),
        (".claude/skills/expert-handoff/SKILL.md", "handoff skill", 3),
        (STOP, "stop gate", 4),
        (
            ".claude/conditions/pre-bash.agentihooks+bash.bash+bash.sh+bash.zsh+bash.eval-identity_pin.py",
            "identity pin",
            5,
        ),
        (GUARD, "prompt guard", 6),
    ],
)
def test_a_role_whose_part_is_gone_or_emptied_fails_by_name(
    copy: Path, capsys: pytest.CaptureFixture[str], path: str, part: str, line: int
) -> None:
    expected = [*HELD]
    expected[line] = f"FAIL: the expert role lacks its {part}: {path}"
    (copy / "expert" / path).write_text("# nothing here\n")
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == expected
    (copy / "expert" / path).unlink()
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == expected


@pytest.mark.feature("expert-role-profile")
def test_a_stop_condition_that_runs_another_gate_fails(copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (copy / "expert" / STOP).write_text(
        'from scripts.gates.entry import main\n\nraise SystemExit(main(["claim-stop"]))\n'
    )
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines()[4] == f"FAIL: the expert role lacks its stop gate: {STOP}"


def test_a_folder_with_no_expert_role_fails_every_part(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(tmp_path) == 1
    assert [line.startswith("FAIL: the expert role lacks its ") for line in capsys.readouterr().out.splitlines()] == [
        True
    ] * 7
