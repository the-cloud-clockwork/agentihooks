"""Tests for the gate that holds the tick step pattern against a shrink only allowlist."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import tick_pattern

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
BUDGETS = 'MEASURED = {\n    "beat": Timing(1.0, CRITICAL_S),\n    "odd": Timing(1.0, 30.0),\n}\n'
FULL = 'CallStep("beat", "g", call, needs=n, reads=r, writes=w, lane=l, effects=e, feature="f")\n'
STEP = "scripts/tick/steps/one.py"
TICK = "def run_tick():\n    return 1\n"


def _tree(root: Path, files: dict[str, str]) -> Path:
    files = {tick_pattern.BUDGETS: BUDGETS, **files}
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
    assert tick_pattern.main(["--write", "--head", str(tree)]) == 0
    return tree


def _grade(base: Path, head: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = tick_pattern.main(["--base", str(base), "--head", str(head)])
    return code, capsys.readouterr().out


def test_a_fully_declared_step_is_clean(tmp_path: Path) -> None:
    tree = _recorded(tmp_path, {STEP: FULL})
    assert tick_pattern.load(tree) == {}


@pytest.mark.parametrize(
    ("source", "offences"),
    [
        (FULL.replace('"beat"', '"lost"'), ["step lost: no budget row"]),
        (FULL.replace('"beat"', '"odd"'), ["step odd: interval outside CRITICAL_S, WIDE_S, SLOW_S"]),
        ('CallStep("beat", "g", call, n, r, w, l, effects=e, feature="f")\n', []),
        ('WriteStep("beat", "g", call, w, n, r, lane=l, effects=e, feature="f")\n', []),
        (
            'steps.CallStep("beat", "g", call)\n',
            [f"step beat: no {field}" for field in ("effects", "feature", "lane", "needs", "reads", "writes")],
        ),
        ('register("beat", lane=l, needs=n, reads=r, writes=w, effects=e)\n', ["step beat: no feature"]),
        ("CallStep(name, 'g', call)\nregister(step)\n", []),
    ],
)
def test_step_offences(tmp_path: Path, source: str, offences: list[str]) -> None:
    tree = _recorded(tmp_path, {STEP: source})
    assert tick_pattern.load(tree) == ({STEP: offences} if offences else {})


def test_a_step_without_a_floor_row_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {STEP: FULL})
    head = _recorded(tmp_path / "head", {STEP: FULL + FULL.replace('"beat"', '"new"')})
    assert _grade(base, head, capsys) == (
        1,
        "1 tick pattern offences in 1 files, 0 on the base allowlist, 1 errors\n"
        f"::error::{STEP} adds step new: no budget row; the base allowlist does not hold it\n",
    )


def test_a_new_def_in_the_tick_module_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {tick_pattern.TICK: TICK})
    head = _tree(tmp_path / "head", {tick_pattern.TICK: TICK + "class Extra:\n    pass\n"})
    (head / tick_pattern.ALLOWLIST).write_text((base / tick_pattern.ALLOWLIST).read_text())
    assert _grade(base, head, capsys) == (
        1,
        "2 tick pattern offences in 1 files, 1 on the base allowlist, 2 errors\n"
        f"::error::{tick_pattern.TICK} adds def Extra; the base allowlist does not hold it\n"
        f"::error::{tick_pattern.ALLOWLIST} differs from the head's measurement; "
        "run python -m scripts.tick_pattern --write\n",
    )


def test_a_planted_banned_import_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {STEP: FULL})
    head = _recorded(tmp_path / "head", {STEP: "from scripts.swarm import refresh_job\n" + FULL})
    assert _grade(base, head, capsys) == (
        1,
        "1 tick pattern offences in 1 files, 0 on the base allowlist, 1 errors\n"
        f"::error::{STEP} adds import scripts.swarm.refresh_job; the base allowlist does not hold it\n",
    )


@pytest.mark.parametrize(
    ("source", "offences"),
    [
        ("import hooks.classifier.api\n", ["import hooks.classifier.api"]),
        (
            "from scripts.inbox.store import InboxStore\n",
            ["import scripts.inbox.store", "import scripts.inbox.store.InboxStore"],
        ),
        ("from .. import inbox\n", ["import relative .."]),
        ("from scripts.tick.ports import Tools\nimport scripts.inboxes\n", []),
    ],
)
def test_banned_imports(tmp_path: Path, source: str, offences: list[str]) -> None:
    tree = _recorded(tmp_path, {STEP: source})
    assert tick_pattern.load(tree) == ({STEP: offences} if offences else {})


def test_adapters_and_the_tick_module_may_import_adapted_implementations(tmp_path: Path) -> None:
    source = "import scripts.inbox\n"
    tree = _recorded(tmp_path, {"scripts/tick/adapters/inbox.py": source, tick_pattern.TICK: source})
    assert tick_pattern.load(tree) == {}


def test_a_pull_request_cannot_allowlist_its_own_offence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {tick_pattern.TICK: TICK})
    head = _recorded(tmp_path / "head", {tick_pattern.TICK: TICK + "def more():\n    pass\n"})
    assert tick_pattern.load(head) == {tick_pattern.TICK: ["def more", "def run_tick"]}
    assert _grade(base, head, capsys) == (
        1,
        "2 tick pattern offences in 1 files, 1 on the base allowlist, 1 errors\n"
        f"::error::{tick_pattern.TICK} adds def more; the base allowlist does not hold it\n",
    )


def test_an_allowlisted_offence_kept_or_removed_is_green(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    both = TICK + "def more():\n    pass\n"
    base = _recorded(tmp_path / "base", {tick_pattern.TICK: both})
    kept = _recorded(tmp_path / "kept", {tick_pattern.TICK: both})
    assert _grade(base, kept, capsys) == (0, "2 tick pattern offences in 1 files, 2 on the base allowlist, 0 errors\n")
    fewer = _recorded(tmp_path / "fewer", {tick_pattern.TICK: TICK})
    assert _grade(base, fewer, capsys) == (0, "1 tick pattern offences in 1 files, 2 on the base allowlist, 0 errors\n")
    gone = _recorded(tmp_path / "gone", {tick_pattern.TICK: "X = 1\n"})
    assert tick_pattern.load(gone) == {}
    assert _grade(base, gone, capsys) == (0, "0 tick pattern offences in 0 files, 2 on the base allowlist, 0 errors\n")


def test_a_base_without_an_allowlist_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", {tick_pattern.TICK: TICK})
    head = _recorded(tmp_path / "head", {tick_pattern.TICK: TICK})
    assert _grade(base, head, capsys) == (
        1,
        "1 tick pattern offences in 1 files, 0 on the base allowlist, 1 errors\n"
        f"::error::{tick_pattern.ALLOWLIST} is missing from the base\n",
    )


def test_files_outside_the_scope_are_not_graded(tmp_path: Path) -> None:
    files = {"scripts/swarm/other.py": TICK, "hooks/hook.py": 'CallStep("lost", "g", c)\n', STEP: TICK}
    tree = _recorded(tmp_path, files)
    assert tick_pattern.load(tree) == {}


def test_a_file_that_does_not_parse_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _recorded(tmp_path / "base", {STEP: FULL})
    head = _tree(tmp_path / "head", {STEP: "def broken(:\n"})
    code, out = _grade(base, head, capsys)
    assert code == 1
    assert out.startswith(f"::error::cannot parse {STEP}: ")


def test_grading_needs_a_base(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        tick_pattern.main([])
    assert raised.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith(": error: grading needs --base")


def test_grading_defaults_the_head_to_the_working_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _recorded(tmp_path / "base", {STEP: FULL})
    head = _recorded(tmp_path / "head", {STEP: FULL})
    monkeypatch.chdir(head)
    assert tick_pattern.main(["--base", str(base)]) == 0
    assert capsys.readouterr().out == "0 tick pattern offences in 0 files, 0 on the base allowlist, 0 errors\n"


def test_floors_reads_only_the_measured_table() -> None:
    source = 'OTHER = {"x": Timing(1, WIDE_S)}\nMEASURED: dict = {"a": Timing(1, SLOW_S), "b": Timing(1), 3: 4}\n'
    assert tick_pattern.floors(source) == {"a": "SLOW_S", "b": ""}


@pytest.mark.skipif("MUTANT_UNDER_TEST" in os.environ, reason="the mutated copy duplicates every function body")
def test_the_recorded_allowlist_matches_the_repository() -> None:
    assert tick_pattern.load(_ROOT) == tick_pattern.measure(_ROOT)


def test_the_allowlist_is_sorted_json() -> None:
    text = (_ROOT / tick_pattern.ALLOWLIST).read_text()
    assert text == json.dumps(json.loads(text), indent=1, sort_keys=True) + "\n"
