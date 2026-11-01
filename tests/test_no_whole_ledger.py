"""Tests for the gate that refuses new whole ledger state reads in the swarm and tick packages."""

import json
import subprocess
from pathlib import Path

import pytest

from scripts import no_whole_ledger

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
CLEAN = "def step(view):\n    return view.tasks\n"
WHOLE = "def step(ledger, slug):\n    return ledger.state(slug)['tasks']\n"


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
    assert no_whole_ledger.main(["--write", "--head", str(tree)]) == 0
    return tree


def _grade(base: Path, head: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = no_whole_ledger.main(["--base", str(base), "--head", str(head)])
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    ("source", "count"),
    [
        ("ledger.state(slug)", 1),
        ("self._ledger.state(slug)", 1),
        ("context.ledger.state(slug)", 1),
        ("LedgerClient().state(slug)", 1),
        ("timing.call(ledger.state, slug)", 1),
        ("ledger.state(a)\nledger.state(b)", 2),
        ("panic.state(store)", 0),
        ("ledger_probe.state(store, slug)", 0),
        ("ledger.metadata(slug)", 0),
        ("Other().state(slug)", 0),
        ("ledger.tasks(slug)", 0),
    ],
)
def test_reads_counts_only_whole_ledger_state_references(source: str, count: int) -> None:
    assert no_whole_ledger.reads(source) == count


def test_a_new_whole_ledger_read_in_a_step_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {"scripts/tick/steps/step.py": CLEAN})
    head = _tree(tmp_path / "head", {"scripts/tick/steps/step.py": WHOLE})
    (head / no_whole_ledger.ALLOWLIST).write_text((base / no_whole_ledger.ALLOWLIST).read_text())
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out == (
        "1 files read the whole ledger state, 0 on the base allowlist, 2 errors\n"
        "::error::scripts/tick/steps/step.py reads the whole ledger state 1 times; the base allowlist allows 0\n"
        "::error::tests/WHOLE_LEDGER_ALLOWLIST.json differs from the head's measurement; "
        "run python -m scripts.no_whole_ledger --write\n"
    )


def test_a_pull_request_cannot_allowlist_its_own_read(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {"scripts/swarm/job.py": CLEAN})
    head = _recorded(tmp_path / "head", {"scripts/swarm/job.py": WHOLE})
    assert no_whole_ledger.load(head) == {"scripts/swarm/job.py": 1}
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out == (
        "1 files read the whole ledger state, 0 on the base allowlist, 1 errors\n"
        "::error::scripts/swarm/job.py reads the whole ledger state 1 times; the base allowlist allows 0\n"
    )


def test_an_allowlisted_read_kept_or_removed_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {"scripts/swarm/job.py": WHOLE + WHOLE.replace("step", "other")})
    kept = _recorded(tmp_path / "kept", {"scripts/swarm/job.py": WHOLE})
    assert _grade(base, kept, capsys) == (
        0,
        "1 files read the whole ledger state, 1 on the base allowlist, 0 errors\n",
    )
    gone = _recorded(tmp_path / "gone", {"scripts/swarm/job.py": CLEAN})
    assert no_whole_ledger.load(gone) == {}
    assert _grade(base, gone, capsys) == (0, "0 files read the whole ledger state, 1 on the base allowlist, 0 errors\n")


def test_files_outside_the_scope_are_not_graded(tmp_path: Path) -> None:
    tree = _recorded(tmp_path / "tree", {"scripts/swarm_ledger/page.py": WHOLE, "hooks/hook.py": WHOLE})
    assert no_whole_ledger.load(tree) == {}


def test_a_base_without_an_allowlist_takes_the_heads_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {"scripts/swarm/job.py": CLEAN})
    head = _recorded(tmp_path / "head", {"scripts/swarm/job.py": WHOLE})
    assert _grade(base, head, capsys) == (0, "1 files read the whole ledger state, 0 on the base allowlist, 0 errors\n")


def test_a_file_that_does_not_parse_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {"scripts/swarm/job.py": CLEAN})
    head = _tree(tmp_path / "head", {"scripts/swarm/job.py": "def broken(:\n"})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out.startswith("::error::cannot parse scripts/swarm/job.py: ")


def test_grading_needs_a_base(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        no_whole_ledger.main([])
    assert raised.value.code == 2
    assert "grading needs --base" in capsys.readouterr().err


def test_the_recorded_allowlist_matches_the_repository() -> None:
    assert no_whole_ledger.load(_ROOT) == no_whole_ledger.measure(_ROOT)


def test_the_allowlist_is_sorted_json() -> None:
    text = (_ROOT / no_whole_ledger.ALLOWLIST).read_text()
    assert text == json.dumps(json.loads(text), indent=1, sort_keys=True) + "\n"
