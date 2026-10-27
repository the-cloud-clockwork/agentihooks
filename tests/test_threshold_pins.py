import io
import json
import subprocess
import tarfile
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


@pytest.fixture(scope="module")
def committed(tmp_path_factory) -> Path:
    # Mutation runs rewrite this module's source in place, so the repository checks read the commit.
    root = tmp_path_factory.mktemp("committed")
    archive = subprocess.run(["git", "-C", str(_ROOT), "archive", "HEAD"], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(root, filter="data")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def test_an_unchanged_or_tightened_gate_holds(tmp_path):
    base = _tree(tmp_path / "base", {"scripts/gate.py": GATE})
    tighter = GATE.replace("LINES = 300", "LINES = 250").replace('"hooks/", ', '"hooks/", "tests/", ')
    tighter += "def other():\n    global OTHER\n"
    head = _tree(tmp_path / "head", {"scripts/gate.py": tighter})
    assert threshold_pins.pin_errors(base, base, PINS) == ([], 3)
    assert threshold_pins.pin_errors(base, head, PINS) == ([], 3)


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
    errors, _ = threshold_pins.pin_errors(base, head, PINS)
    assert len(errors) == 1
    assert message in errors[0]


def test_a_removed_gate_or_constant_is_red(tmp_path):
    base = _tree(tmp_path / "base", {"scripts/gate.py": GATE})
    gone = _tree(tmp_path / "gone", {"scripts/other.py": ""})
    renamed = _tree(tmp_path / "renamed", {"scripts/gate.py": GATE.replace("LINES =", "LIMIT =")})
    assert "no longer pinned" in threshold_pins.pin_errors(base, gone, PINS)[0][0]
    with pytest.raises(threshold_pins.PinError, match="must bind LINES once"):
        threshold_pins.pin_errors(base, renamed, PINS)


@pytest.mark.parametrize(
    "rebinding",
    [
        "LINES = 400\n",
        "LINES += 100\n",
        "LINES: int = 400\n",
        'RULES["C901"] = 99\n',
        'del RULES["C901"]\n',
        'RULES.update({"C901": 99})\n',
        "def widen():\n    global LINES\n    LINES = 400\n",
        "LINES, _ = 400, 0\n",
        "(LINES := 400)\n",
        "for LINES in (400,):\n    pass\n",
        "with open(__file__) as LINES:\n    pass\n",
        "import os as LINES\n",
        "from os import LINES\n",
        "def LINES():\n    pass\n",
        "async def LINES():\n    pass\n",
        "class LINES:\n    pass\n",
        "del LINES\n",
        'globals()["LINES"] = 400\n',
        'vars()["LINES"] = 400\n',
        'setattr(__import__(__name__), "LINES", 400)\n',
        'locals()["LINES"] = 400\n',
        'exec("LINES = 400")\n',
        'eval("400")\n',
        'compile("LINES = 400", "", "exec")\n',
        "import sys\nsys.modules[__name__].LINES = 400\n",
        'import sys\nsys.modules[__name__].__dict__["LINES"] = 400\n',
        'import sys\nsys.modules[__name__].__setattr__("LINES", 400)\n',
        "match 400:\n    case LINES:\n        pass\n",
        "match [400]:\n    case [*LINES]:\n        pass\n",
        'match {"a": 1}:\n    case {**LINES}:\n        pass\n',
        "import sys\nsys.modules[__name__].__dict__.update(LINES=400)\n",
        'import sys\nkey = "LINES"\nsys._getframe().f_globals[key] = 400\n',
        'import sys\nkey = "LINES"\nsys._getframe().f_locals[key] = 400\n',
    ],
)
def test_a_second_binding_or_mutation_of_a_pinned_name_is_red(tmp_path, rebinding):
    base = _tree(tmp_path / "base", {"scripts/gate.py": GATE})
    head = _tree(tmp_path / "head", {"scripts/gate.py": GATE + rebinding})
    with pytest.raises(threshold_pins.PinError, match="must bind (LINES|RULES) once"):
        threshold_pins.pin_errors(base, head, PINS)


def test_an_unknown_pin_kind_is_red(tmp_path):
    base = _tree(tmp_path / "base", {"scripts/gate.py": GATE})
    with pytest.raises(threshold_pins.PinError, match="unknown pin kind 'at least'"):
        threshold_pins.pin_errors(base, base, (("scripts/gate.py", "LINES", "at least"),))


def test_a_pin_the_base_lacks_grades_nothing(tmp_path):
    base = _tree(tmp_path / "base", {"scripts/other.py": ""})
    head = _tree(tmp_path / "head", {"scripts/gate.py": GATE})
    assert threshold_pins.pin_errors(base, head, PINS) == ([], 0)


def test_a_pin_the_base_lacks_does_not_stop_the_next_pin(tmp_path):
    base = _tree(tmp_path / "base", {"scripts/gate.py": GATE})
    head = _tree(tmp_path / "head", {"scripts/gate.py": GATE.replace("LINES = 300", "LINES = 400")})
    pins = (("scripts/absent.py", "LINES", "at most"), *PINS)
    errors, compared = threshold_pins.pin_errors(base, head, pins)
    assert compared == 3
    assert len(errors) == 1
    assert "LINES rose from 300 to 400" in errors[0]


def test_every_allowlist_is_graded_after_an_absent_or_gone_one(tmp_path):
    other = "tests/Y_ALLOWLIST.json"
    base = _tree(tmp_path / "base", {ALLOWLIST: "{}", other: json.dumps({"a.py": 1})})
    head = _tree(tmp_path / "head", {other: json.dumps({"a.py": 2})})
    assert threshold_pins.allowlist_errors(base, head, ("tests/ABSENT_ALLOWLIST.json", ALLOWLIST, other)) == (
        [
            f"{ALLOWLIST} is gone, so its gate grades nothing",
            f"{other}: a.py rose from 1 to 2; allowlists only shrink",
        ],
        2,
    )


@pytest.mark.parametrize(
    ("base", "head", "errors"),
    [
        ({"a.py": 1}, 3, ["changed from {'a.py': 1} to 3"]),
        (3, "3", ["changed from 3 to '3'"]),
        (["x"], ["x"], []),
        ("x", "y", ["changed from 'x' to 'y'"]),
        (2, 1, []),
    ],
)
def test_a_changed_allowlist_entry_shape_is_red(base, head, errors):
    assert threshold_pins.shrunk(base, head, "") == errors


def test_tracked_outside_a_repository_is_red(tmp_path):
    with pytest.raises(threshold_pins.PinError, match="git ls-files failed in"):
        threshold_pins.tracked(tmp_path)


def test_the_check_requires_both_trees():
    with pytest.raises(SystemExit):
        threshold_pins.main(["--head", str(_ROOT)])
    with pytest.raises(SystemExit):
        threshold_pins.main(["--base", str(_ROOT)])


def test_an_unchanged_repository_is_green(committed, capsys):
    assert threshold_pins.main(["--base", str(committed), "--head", str(committed)]) == 0
    pins, allowlists = len(threshold_pins.PINS), len(threshold_pins.SHRINK_ONLY)
    assert f"{pins} thresholds compared, {allowlists} allowlists compared, 0 errors" in capsys.readouterr().out


def test_a_rebound_pin_fails_the_check(committed, tmp_path, capsys):
    rebound = (committed / "scripts/size_limits.py").read_text() + "FUNCTION_LINES = 400\n"
    head = _tree(tmp_path / "head", {"scripts/size_limits.py": rebound})
    assert threshold_pins.main(["--base", str(committed), "--head", str(head)]) == 1
    assert "::error::scripts/size_limits.py must bind FUNCTION_LINES once" in capsys.readouterr().out


def test_an_allowlist_only_shrinks(tmp_path):
    counts = {"a.py": {"C901": 14}, "b.py": 3}
    base = _tree(tmp_path / "base", {ALLOWLIST: json.dumps(counts)})
    shrunk = _tree(tmp_path / "shrunk", {ALLOWLIST: json.dumps({"a.py": {"C901": 13}})})
    grown = _tree(tmp_path / "grown", {ALLOWLIST: json.dumps({"a.py": {"C901": 15}, "b.py": 3, "c.py": 1})})
    gone = _tree(tmp_path / "gone", {"tests/other.json": "{}"})
    assert threshold_pins.allowlist_errors(base, shrunk, (ALLOWLIST,)) == ([], 1)
    assert threshold_pins.allowlist_errors(base, grown, (ALLOWLIST,)) == (
        [
            f"{ALLOWLIST}: c.py is new; the base does not allow it; allowlists only shrink",
            f"{ALLOWLIST}: a.py C901 rose from 14 to 15; allowlists only shrink",
        ],
        1,
    )
    assert threshold_pins.allowlist_errors(base, gone, (ALLOWLIST,)) == (
        [f"{ALLOWLIST} is gone, so its gate grades nothing"],
        1,
    )


def test_an_unregistered_floor_or_allowlist_is_red(tmp_path):
    head = _tree(
        tmp_path / "head",
        {
            ALLOWLIST: "{}",
            "tests/NEW_ALLOWLIST.json": "{}",
            "x/floors.json": "{}",
            "tests/fixtures/A_ALLOWLIST.json": "",
            "evidence/E_ALLOWLIST.json": "",
        },
    )
    errors = threshold_pins.registry_errors(head, (ALLOWLIST,), ())
    assert errors == [
        "tests/NEW_ALLOWLIST.json is a floor or allowlist the threshold pin does not register",
        "x/floors.json is a floor or allowlist the threshold pin does not register",
    ]


def test_every_pin_and_allowlist_names_a_real_literal_in_this_repository(committed):
    assert threshold_pins.pin_errors(committed, committed) == ([], len(threshold_pins.PINS))
    assert threshold_pins.allowlist_errors(committed, committed) == ([], len(threshold_pins.SHRINK_ONLY))
    assert threshold_pins.registry_errors(committed) == []
    for path in threshold_pins.SHRINK_ONLY:
        assert (committed / path).is_file(), path
    for path, module in threshold_pins.GRADED_BY:
        assert (committed / path).is_file(), path
        assert (committed / (module.replace(".", "/") + ".py")).is_file(), module


def test_the_size_plant_turns_the_check_red(committed, tmp_path, capsys):
    planted = (committed / "scripts/size_limits.py").read_text().replace("FUNCTION_LINES = 300", "FUNCTION_LINES = 400")
    head = _tree(tmp_path / "head", {"scripts/size_limits.py": planted})
    assert threshold_pins.main(["--base", str(committed), "--head", str(head)]) == 1
    assert "scripts/size_limits.py FUNCTION_LINES rose from 300 to 400" in capsys.readouterr().out
