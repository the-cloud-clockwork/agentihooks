"""The live check of the pull request body feature over this checkout and over damaged copies of it."""

import shutil
from pathlib import Path

import pytest

from scripts import pr_body
from scripts.feature_checks import pr_body as check

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ".github/workflows/gate-protected.yml"
HELD = [
    *(f"the checkout holds its {section} template section" for section in pr_body.SECTIONS),
    "the checkout holds its protected body check",
    "the checkout holds its engineer fill step",
    "the checkout holds its engineer check step",
]


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    for path in (check.TEMPLATE, check.SKILL, WORKFLOW):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, tmp_path / path)
    return tmp_path


@pytest.mark.feature("pull-request-recon-context")
def test_the_checkout_holds_every_part_and_each_is_listed(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.ROOT == ROOT
    assert check.main() == 0
    assert capsys.readouterr().out.splitlines() == HELD


@pytest.mark.feature("pull-request-recon-context")
@pytest.mark.parametrize(
    "path,mark,line",
    [
        (check.TEMPLATE, "## Assumptions\n", 0),
        (check.TEMPLATE, "## Notes for the reviewer\n", 8),
        (WORKFLOW, "python -m scripts.pr_body check --event", 9),
        (check.SKILL, "agentihooks-pr-body fill", 10),
        (check.SKILL, "agentihooks-pr-body check", 11),
    ],
)
def test_a_part_whose_mark_is_gone_fails_by_name(
    copy: Path, capsys: pytest.CaptureFixture[str], path: str, mark: str, line: int
) -> None:
    file = copy / path
    file.write_text(file.read_text(encoding="utf-8").replace(mark, ""), encoding="utf-8")
    expected = [*HELD]
    expected[line] = HELD[line].replace("the checkout holds its", "FAIL: the checkout lacks its") + f": {path}"
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == expected


@pytest.mark.feature("pull-request-recon-context")
def test_a_missing_file_fails_every_part_it_carries(copy: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (copy / check.SKILL).unlink()
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == [
        *HELD[:10],
        f"FAIL: the checkout lacks its engineer fill step: {check.SKILL}",
        f"FAIL: the checkout lacks its engineer check step: {check.SKILL}",
    ]


@pytest.mark.feature("pull-request-recon-context")
def test_a_folder_in_place_of_a_file_fails_every_part_it_carries(
    copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (copy / check.SKILL).unlink()
    (copy / check.SKILL).mkdir()
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == [
        *HELD[:10],
        f"FAIL: the checkout lacks its engineer fill step: {check.SKILL}",
        f"FAIL: the checkout lacks its engineer check step: {check.SKILL}",
    ]
