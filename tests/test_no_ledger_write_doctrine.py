"""Tests for the gate that refuses new prose teaching an agent to write to the ledger."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import no_ledger_write_doctrine as gate

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
WRITE_LINE = 'agentihooks ledger --slug <slug> --as <name> task add <id> "<title>"\n'
READ_LINE = "agentihooks ledger --slug {slug} get phases/{phase}.\n"
API_LINE = 'agentihooks api --slug <slug> --as <name> task add <id> "<title>"\n'


def _tree(root: Path, files: dict[str, str]) -> Path:
    (root / "tests").mkdir(parents=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-f", *files], check=True)
    return root


def _recorded(root: Path, files: dict[str, str]) -> Path:
    tree = _tree(root, files)
    assert gate.main(["--write", "--head", str(tree)]) == 0
    return tree


def _grade(base: Path, head: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = gate.main(["--base", str(base), "--head", str(head)])
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    ("source", "count"),
    [
        (WRITE_LINE, 1),
        ('agentihooks ledger comment tasks/t1 "ok"\n', 1),
        ('agentihooks ledger relay tasks/<id> "<text>"\n', 1),
        (WRITE_LINE + WRITE_LINE, 2),
        (READ_LINE, 0),
        ("agentihooks ledger --slug <slug> get tasks/<id>\n", 0),
        ("agentihooks ledger --slug <slug> tasks --state open\n", 0),
        (API_LINE, 0),
        ("agentihooks ledger (`new`, `serve`, `watch`, `chat`)\n", 0),
    ],
)
def test_hits_counts_only_ledger_write_doctrine_lines(source: str, count: int) -> None:
    assert gate.hits(source) == count


def test_a_new_ledger_write_line_in_a_skill_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {"docs/example.md": READ_LINE})
    head = _tree(tmp_path / "head", {"docs/example.md": WRITE_LINE})
    (head / gate.ALLOWLIST).write_text((base / gate.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "docs/example.md teaches a ledger write 1 times; the base allowlist allows 0" in out


def test_rewriting_to_the_api_alias_clears_the_finding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {"docs/example.md": WRITE_LINE})
    fixed = _recorded(tmp_path / "fixed", {"docs/example.md": API_LINE})
    assert gate.load(fixed) == {}
    assert _grade(base, fixed, capsys) == (0, "0 files teach a ledger write, 1 on the base allowlist, 0 errors\n")


def test_files_outside_the_scope_are_not_graded(tmp_path: Path) -> None:
    files = {
        "scripts/swarm_ledger/ledger.py": WRITE_LINE,
        "tests/swarm/test_cli.py": WRITE_LINE,
        "docs/example.md": WRITE_LINE,
    }
    tree = _recorded(tmp_path / "tree", files)
    assert gate.load(tree) == {"docs/example.md": 1}


def test_the_ledger_implementation_skill_is_graded_like_any_other(tmp_path: Path) -> None:
    files = {"profiles/package/skills/swarm-ledger/SKILL.md": WRITE_LINE}
    tree = _recorded(tmp_path / "tree", files)
    assert gate.load(tree) == {"profiles/package/skills/swarm-ledger/SKILL.md": 1}


def test_the_triage_priorities_script_is_excluded(tmp_path: Path) -> None:
    files = {"profiles/package/skills/triage-priorities/scripts/list_priorities.py": WRITE_LINE}
    tree = _recorded(tmp_path / "tree", files)
    assert gate.load(tree) == {}


def test_a_missing_base_allowlist_allows_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {"docs/example.md": READ_LINE})
    head = _recorded(tmp_path / "head", {"docs/example.md": WRITE_LINE})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "docs/example.md teaches a ledger write 1 times; the base allowlist allows 0" in out


def test_a_file_over_its_own_allowance_names_that_allowance(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {"docs/example.md": WRITE_LINE})
    head = _recorded(tmp_path / "head", {"docs/example.md": WRITE_LINE * 3})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert "docs/example.md teaches a ledger write 3 times; the base allowlist allows 1" in out


def test_a_file_at_its_allowance_is_not_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {"docs/example.md": WRITE_LINE})
    same = _recorded(tmp_path / "same", {"docs/example.md": WRITE_LINE})
    assert _grade(base, same, capsys) == (0, "1 files teach a ledger write, 1 on the base allowlist, 0 errors\n")


def test_an_allowlist_that_differs_from_the_head_measurement_names_the_write_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _recorded(tmp_path / "base", {"docs/example.md": READ_LINE})
    head = _tree(tmp_path / "head", {"docs/example.md": WRITE_LINE})
    (head / gate.ALLOWLIST).write_text((base / gate.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert (
        f"{gate.ALLOWLIST} differs from the head's measurement; run python -m scripts.no_ledger_write_doctrine --write"
        in out
    )


def test_a_file_that_cannot_be_decoded_names_itself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    head = tmp_path / "head"
    (head / "tests").mkdir(parents=True)
    (head / "docs").mkdir(parents=True)
    (head / "docs" / "example.md").write_bytes(b"\xff\xfe")
    code, out = _grade(tmp_path / "base", head, capsys)
    assert code == 1
    assert out.startswith("::error::cannot read docs/example.md: ")


def test_write_records_one_space_indented_sorted_json(tmp_path: Path) -> None:
    tree = _recorded(tmp_path, {"docs/b.md": WRITE_LINE, "profiles/a.md": WRITE_LINE + WRITE_LINE})
    expected = '{\n "docs/b.md": 1,\n "profiles/a.md": 2\n}\n'
    assert (tree / gate.ALLOWLIST).read_text() == expected


def test_the_recorded_allowlist_matches_the_repository() -> None:
    assert gate.load(_ROOT) == gate.measure(_ROOT)


def test_the_allowlist_is_sorted_json() -> None:
    text = (_ROOT / gate.ALLOWLIST).read_text()
    assert text == json.dumps(json.loads(text), indent=1, sort_keys=True) + "\n"
