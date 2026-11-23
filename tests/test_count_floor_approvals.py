"""The count floor's approvals mode: a new signed retirement of a pull request head needs a code owner's approval."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parent.parent
REVIEWED = "c" * 40
NODE = "tests/test_mod.py::test_retired"
SIGNED = {"reader": "Standards reader", "reason": "the head removes the behaviour it held"}
OWNERS = {".github/CODEOWNERS": "/tests/ @engineer\n/test-retirements/ @Retirement-Owner\n"}
REFUSED = f"::error::{NODE} is retired without a code owner's approval of the pull request head.\n"
UNREAD = "The pull request reviews could not be read, so no retirement is admitted\n"


def _retirement(nodeid: str, ruling: dict[str, str]) -> dict[str, str]:
    return {f"test-retirements/{hashlib.sha256(nodeid.encode()).hexdigest()}.json": json.dumps({nodeid: ruling})}


def _review(user: str, state: str, commit: str = REVIEWED) -> str:
    return json.dumps({"user": user, "state": state, "commit_id": commit}) + "\n"


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _hold(
    tmp_path: Path,
    head: dict[str, str],
    reviews: str | None,
    reviewed: str | None = REVIEWED,
    base: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    file = tmp_path / "reviews.jsonl"
    if reviews is not None:
        file.write_text(reviews)
    env = {name: value for name, value in os.environ.items() if name != "TEST_COUNT_REVIEWED_HEAD"}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.count_floor",
            "--approvals",
            "--base",
            str(_tree(tmp_path / "base", OWNERS if base is None else base)),
            "--head",
            str(_tree(tmp_path / "head", head)),
        ],
        cwd=ROOT,
        env={**env, "TEST_COUNT_REVIEWS": str(file)}
        | ({} if reviewed is None else {"TEST_COUNT_REVIEWED_HEAD": reviewed}),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("reviews", "held"),
    [
        (_review("retirement-owner", "APPROVED"), False),
        (_review("retirement-owner", "CHANGES_REQUESTED") + _review("retirement-owner", "APPROVED"), False),
        ("", True),
        (_review("engineer", "APPROVED"), True),
        (_review("retirement-owner", "APPROVED", "d" * 40), True),
        (_review("retirement-owner", "APPROVED") + _review("retirement-owner", "CHANGES_REQUESTED"), True),
    ],
    ids=["approved", "approved after changes", "no review", "not an owner", "earlier head", "withdrawn"],
)
@pytest.mark.feature("count-floor-retirement-needs-code-owner-approval")
def test_the_approvals_mode_fails_a_new_signed_retirement_no_code_owner_approved_on_the_head(
    tmp_path: Path, reviews: str, held: bool
) -> None:
    result = _hold(tmp_path, _retirement(NODE, SIGNED), reviews)
    assert result.returncode == (1 if held else 0), result.stdout + result.stderr
    assert (REFUSED in result.stdout) == held
    named = next(iter(_retirement(NODE, SIGNED)))
    refusal = (
        f"{named} is not approved on the pull request head by a code owner (@Retirement-Owner), so it admits nothing\n"
    )
    assert (refusal in result.stdout) == held
    assert UNREAD not in result.stdout


@pytest.mark.parametrize("reviews", [None, "{not json\n"], ids=["missing", "malformed"])
@pytest.mark.feature("count-floor-retirement-needs-code-owner-approval")
def test_the_approvals_mode_fails_a_new_signed_retirement_whose_reviews_cannot_be_read(
    tmp_path: Path, reviews: str | None
) -> None:
    result = _hold(tmp_path, _retirement(NODE, SIGNED), reviews)
    assert result.returncode == 1, result.stdout + result.stderr
    assert UNREAD in result.stdout
    assert REFUSED in result.stdout


@pytest.mark.feature("count-floor-retirement-needs-code-owner-approval")
def test_the_approvals_mode_fails_a_retirement_whose_file_has_no_code_owner_on_the_base(tmp_path: Path) -> None:
    result = _hold(tmp_path, _retirement(NODE, SIGNED), _review("retirement-owner", "APPROVED"), base={})
    assert result.returncode == 1, result.stdout + result.stderr
    assert REFUSED in result.stdout


@pytest.mark.parametrize(
    ("head", "base"),
    [
        ({}, OWNERS),
        (_retirement(NODE, {**SIGNED, "reader": "engineer"}), OWNERS),
        (_retirement(NODE, SIGNED), {**OWNERS, **_retirement(NODE, SIGNED)}),
    ],
    ids=["no retirement", "unsigned", "already on the base"],
)
@pytest.mark.feature("count-floor-retirement-needs-code-owner-approval")
def test_the_approvals_mode_holds_only_new_signed_retirements(
    tmp_path: Path, head: dict[str, str], base: dict[str, str]
) -> None:
    result = _hold(tmp_path, head, None, base=base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error::" not in result.stdout
    assert UNREAD not in result.stdout


@pytest.mark.parametrize("reviewed", ["", None], ids=["empty", "unset"])
@pytest.mark.feature("count-floor-retirement-needs-code-owner-approval")
def test_the_approvals_mode_passes_without_a_reviewed_head(tmp_path: Path, reviewed: str | None) -> None:
    result = _hold(tmp_path, _retirement(NODE, SIGNED), None, reviewed=reviewed)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error::" not in result.stdout
    assert UNREAD not in result.stdout


@pytest.mark.feature("count-floor-retirement-needs-code-owner-approval")
def test_the_approvals_mode_passes_a_head_the_floor_refuses_because_it_collects_nothing(tmp_path: Path) -> None:
    base = {**OWNERS, "tests/test_mod.py": "def test_kept():\n    assert True\n\ndef test_lost():\n    assert True\n"}
    head = {"tests/test_mod.py": "def test_kept():\n    assert True\n"}
    result = _hold(tmp_path, head, None, base=base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    floor = subprocess.run(
        [sys.executable, "-m", "tests.count_floor", "--base", str(tmp_path / "base"), "--head", str(tmp_path / "head")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert floor.returncode == 1, floor.stdout + floor.stderr
    assert "head collects 1 tests, base floor 2" in floor.stdout


@pytest.mark.parametrize("side", [["--record", "record.json"], ["--retire", NODE]], ids=["record", "retire"])
@pytest.mark.feature("count-floor-retirement-needs-code-owner-approval")
def test_the_approvals_mode_is_refused_beside_another_mode(tmp_path: Path, side: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tests.count_floor", "--approvals", *side, "--head", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "--approvals grades a head against a base, so it needs --base" in result.stderr
    assert list(tmp_path.iterdir()) == []
