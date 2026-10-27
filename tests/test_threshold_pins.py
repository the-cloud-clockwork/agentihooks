import json
import subprocess
from pathlib import Path

import pytest

from scripts import threshold_pins

_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

GATE = 'RULES = {"C901": 12, "PLR0913": 7}\nLINES = 300\nSCOPE = ("hooks/", "scripts/")\n'
PINS = (
    ("scripts/gate.py", "RULES", "at most"),
    ("scripts/gate.py", "LINES", "at most"),
    ("scripts/gate.py", "SCOPE", "covers"),
)
ALLOWLIST = "tests/X_ALLOWLIST.json"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-f", *files], check=True)
    return root


def _allowlist(counts: dict) -> str:
    return json.dumps(counts)


def test_an_unchanged_or_tightened_gate_holds(tmp_path):
    base = _tree(tmp_path / "base", {"scripts/gate.py": GATE})
    tighter = GATE.replace("LINES = 300", "LINES = 250").replace('"hooks/", ', '"hooks/", "tests/", ')
    head = _tree(tmp_path / "head", {"scripts/gate.py": tighter})
    assert threshold_pins.pin_errors(base, base, PINS) == []
    assert threshold_pins.pin_errors(base, head, PINS) == []


@pytest.mark.parametrize(
    ("loosen", "message"),
    [
        (("LINES = 300", "LINES = 400"), "LINES rose from 300 to 400"),
        (('"PLR0913": 7', '"PLR0913": 9'), "RULES PLR0913 rose from 7 to 9"),
        (('"C901": 12, ', ""), "RULES drops C901"),
        (('"hooks/", "scripts/"', '"hooks/",'), "SCOPE drops ['scripts/']"),
    ],
)
def test_a_loosened_threshold_is_red(tmp_path, loosen, message):
    base = _tree(tmp_path / "base", {"scripts/gate.py": GATE})
    head = _tree(tmp_path / "head", {"scripts/gate.py": GATE.replace(*loosen)})
    errors = threshold_pins.pin_errors(base, head, PINS)
    assert len(errors) == 1
    assert message in errors[0]


def test_a_removed_gate_or_constant_is_red(tmp_path):
    base = _tree(tmp_path / "base", {"scripts/gate.py": GATE})
    gone = _tree(tmp_path / "gone", {"scripts/other.py": ""})
    renamed = _tree(tmp_path / "renamed", {"scripts/gate.py": GATE.replace("LINES =", "LIMIT =")})
    assert "no longer pinned" in threshold_pins.pin_errors(base, gone, PINS)[0]
    with pytest.raises(threshold_pins.PinError, match="defines no literal LINES"):
        threshold_pins.pin_errors(base, renamed, PINS)


def test_a_pin_the_base_lacks_grades_nothing(tmp_path):
    base = _tree(tmp_path / "base", {"scripts/other.py": ""})
    head = _tree(tmp_path / "head", {"scripts/gate.py": GATE})
    assert threshold_pins.pin_errors(base, head, PINS) == []


def test_an_allowlist_only_shrinks(tmp_path):
    counts = {"a.py": {"C901": 14}, "b.py": 3}
    base = _tree(tmp_path / "base", {ALLOWLIST: _allowlist(counts)})
    shrunk = _tree(tmp_path / "shrunk", {ALLOWLIST: _allowlist({"a.py": {"C901": 13}})})
    grown = _tree(tmp_path / "grown", {ALLOWLIST: _allowlist({"a.py": {"C901": 15}, "b.py": 3, "c.py": 1})})
    assert threshold_pins.allowlist_errors(base, shrunk, (ALLOWLIST,)) == []
    errors = threshold_pins.allowlist_errors(base, grown, (ALLOWLIST,))
    assert errors == [
        f"{ALLOWLIST}: c.py is new; the base does not allow it; allowlists only shrink",
        f"{ALLOWLIST}: a.py C901 rose from 14 to 15; allowlists only shrink",
    ]


def test_an_unregistered_floor_or_allowlist_is_red(tmp_path):
    head = _tree(
        tmp_path / "head",
        {
            ALLOWLIST: "{}",
            "tests/NEW_ALLOWLIST.json": "{}",
            "x/floors.json": "{}",
            "tests/fixtures/A_ALLOWLIST.json": "",
        },
    )
    errors = threshold_pins.registry_errors(head, (ALLOWLIST,), ())
    assert errors == [
        "tests/NEW_ALLOWLIST.json is a floor or allowlist the threshold pin does not register",
        "x/floors.json is a floor or allowlist the threshold pin does not register",
    ]


def test_every_pin_and_allowlist_names_a_real_literal_in_this_repository():
    assert threshold_pins.pin_errors(_ROOT, _ROOT) == []
    assert threshold_pins.registry_errors(_ROOT) == []
    for path in threshold_pins.SHRINK_ONLY:
        assert (_ROOT / path).is_file(), path
    for path, module in threshold_pins.GRADED_BY:
        assert (_ROOT / path).is_file(), path
        assert (_ROOT / (module.replace(".", "/") + ".py")).is_file(), module


def test_the_size_plant_turns_the_check_red(tmp_path, capsys):
    planted = (_ROOT / "scripts/size_limits.py").read_text().replace("FUNCTION_LINES = 300", "FUNCTION_LINES = 400")
    head = _tree(tmp_path / "head", {"scripts/size_limits.py": planted})
    assert threshold_pins.main(["--base", str(_ROOT), "--head", str(head)]) == 1
    assert "scripts/size_limits.py FUNCTION_LINES rose from 300 to 400" in capsys.readouterr().out
