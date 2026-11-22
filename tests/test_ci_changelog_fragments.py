import json
import re
from pathlib import Path

import pytest
import yaml

from scripts.ci_changelog_fragments import SECTIONS, expected_name, heading_problems, main, section

ROOT = Path(__file__).resolve().parents[1]
RED_FIXTURE = ROOT / "tests/fixtures/ci_changelog_fragments/red.md"
CLEAN_FIXTURE = ROOT / "tests/fixtures/ci_changelog_fragments/clean.md"
UNHEADED = "its first line is no section heading, so the release notes file its entries under Changed"
UNKNOWN = "the heading Fixes names none of the sections Added, Changed, Deprecated, Removed, Fixed, Security"
STEP = "Refuse changelog fragments named off the branch or opening without a section heading"


def _tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _trees(tmp_path: Path, added: dict[str, str]) -> tuple[str, str]:
    merged = {"changelog.d/README.md": "# Changelog fragments\n", "changelog.d/old.md": "- Merged unheaded.\n"}
    return str(_tree(tmp_path / "base", merged)), str(_tree(tmp_path / "head", merged | added))


def test_the_red_fixture_reports_the_unheaded_start_and_the_unknown_section_in_line_order() -> None:
    assert heading_problems(RED_FIXTURE.read_text()) == [UNHEADED, UNKNOWN]


def test_the_clean_fixture_reports_nothing() -> None:
    assert heading_problems(CLEAN_FIXTURE.read_text()) == []


@pytest.mark.parametrize("text", ["", "\n  \n"])
def test_a_fragment_without_a_written_line_has_no_section_heading(text: str) -> None:
    assert heading_problems(text) == [UNHEADED]


def test_a_section_heading_with_trailing_spaces_names_its_section() -> None:
    assert heading_problems("### Fixed  \n\n- An entry.\n") == []


def test_the_branch_names_the_fragment_with_every_slash_as_a_dash() -> None:
    assert expected_name("team/eng/1") == "changelog.d/team-eng-1.md"


def test_fragments_named_off_the_branch_or_opening_without_a_heading_are_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base, head = _trees(
        tmp_path,
        {
            "changelog.d/12311.fixed.md": "- Ships under Changed.\n",
            "changelog.d/eng-1.md": CLEAN_FIXTURE.read_text(),
            "changelog.d/notes/eng-1.md": RED_FIXTURE.read_text(),
        },
    )
    assert main(["--base", base, "--head", head, "--branch", "eng-1"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Changelog fragments: 3 added, 5 problems",
        "::error::changelog.d/12311.fixed.md: a pull request from the branch eng-1 adds its fragment as "
        "changelog.d/eng-1.md",
        f"::error::changelog.d/12311.fixed.md: {UNHEADED}",
        "::error::changelog.d/notes/eng-1.md: a pull request from the branch eng-1 adds its fragment as "
        "changelog.d/eng-1.md",
        f"::error::changelog.d/notes/eng-1.md: {UNHEADED}",
        f"::error::changelog.d/notes/eng-1.md: {UNKNOWN}",
    ]


def test_the_fragment_named_from_the_branch_under_section_headings_is_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base, head = _trees(tmp_path, {"changelog.d/team-eng-1.md": CLEAN_FIXTURE.read_text()})
    assert main(["--base", base, "--head", head, "--branch", "team/eng-1"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Changelog fragments: 1 added, 0 problems"]


def test_an_empty_branch_name_grades_nothing_and_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base, head = _trees(tmp_path, {"changelog.d/eng-1.md": CLEAN_FIXTURE.read_text()})
    assert main(["--base", base, "--head", head, "--branch", ""]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "::error::no branch name was given, so the check cannot name the fragment a pull request adds"
    ]


@pytest.mark.parametrize("side", ["--base", "--head"])
def test_a_tree_without_the_changelog_folder_grades_nothing_and_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], side: str
) -> None:
    base, head = _trees(tmp_path, {"changelog.d/eng-1.md": CLEAN_FIXTURE.read_text()})
    trees = {"--base": base, "--head": head} | {side: str(tmp_path / "elsewhere")}
    assert main([*(word for flag, path in trees.items() for word in (flag, path)), "--branch", "eng-1"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"::error::{tmp_path / 'elsewhere'} carries no changelog.d folder, so the check graded no fragment"
    ]


def test_a_fragment_that_is_not_utf8_is_red_with_its_decode_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base, head = _trees(tmp_path, {})
    (Path(head) / "changelog.d/eng-1.md").write_bytes(b"### Fixed\n\n- \xff\n")
    assert main(["--base", base, "--head", head, "--branch", "eng-1"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "::error::'utf-8' codec can't decode byte 0xff in position 13: invalid start byte"
    ]


@pytest.mark.parametrize("dropped", ["--base", "--head", "--branch"])
def test_each_argument_is_required(dropped: str) -> None:
    given = {"--base": "base", "--head": "head", "--branch": "eng-1"}
    with pytest.raises(SystemExit) as exit_info:
        main([word for flag, value in given.items() if flag != dropped for word in (flag, value)])
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "line",
    [
        "### Added",
        "## Fixed",
        "###\tChanged",
        "### Fixed  ",
        "###   Security",
        "#### Fixed",
        "# Fixed",
        "###Fixed",
        "  ### Fixed",
        "##X Fixed",
        "##",
        "### ",
        "###  ",
        "##\t ",
        "##\t\t ",
        "- An entry.",
    ],
)
def test_a_line_reads_as_the_heading_the_release_notes_read(line: str) -> None:
    found = re.search(r're\.fullmatch\(r"([^"]+)", line\)', (ROOT / "scripts/ci_release.sh").read_text())
    assert found
    match = re.fullmatch(found.group(1), line)
    assert section(line) == (match.group(1) if match else None)


def test_the_sections_are_the_ones_the_release_notes_order() -> None:
    notes = (ROOT / "scripts/ci_release.sh").read_text()
    assert f"order = {json.dumps(list(SECTIONS))}" in notes


def test_lint_grades_the_pull_request_branch_with_the_protected_grader() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    step = next(step for step in workflow["jobs"]["lint"]["steps"] if step.get("name") == STEP)
    assert step["if"] == "${{ !cancelled() }}"
    assert step["env"] == {"BRANCH": "${{ github.head_ref || github.ref_name }}"}
    assert step["run"] == (
        'cd "$RUNNER_TEMP/grader"\n'
        "python -m scripts.ci_changelog_fragments"
        ' --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE" --branch "$BRANCH"\n'
    )


@pytest.mark.feature("changelog-fragment-required-on-pull-request")
@pytest.mark.parametrize("in_base", [False, True])
def test_a_pull_request_that_adds_no_branch_fragment_is_red_naming_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, in_base: bool
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    base, head = _trees(tmp_path, {})
    if in_base:
        for tree in (base, head):
            _tree(Path(tree), {"changelog.d/team-eng-1.md": CLEAN_FIXTURE.read_text()})
    assert main(["--base", base, "--head", head, "--branch", "team/eng-1"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Changelog fragments: 0 added, 1 problems",
        "::error::changelog.d/team-eng-1.md: a pull request from the branch team/eng-1 adds no fragment at this path",
    ]


@pytest.mark.feature("changelog-fragment-required-on-pull-request")
@pytest.mark.parametrize("event", ["workflow_dispatch", "push", "merge_group", None])
def test_a_run_outside_a_pull_request_that_adds_no_fragment_stays_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, event: str | None
) -> None:
    if event is None:
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    else:
        monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    base, head = _trees(tmp_path, {})
    assert main(["--base", base, "--head", head, "--branch", "team/eng-1"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Changelog fragments: 0 added, 0 problems"]


@pytest.mark.feature("changelog-fragment-required-on-pull-request")
def test_a_pull_request_that_adds_its_branch_fragment_is_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    base, head = _trees(tmp_path, {"changelog.d/team-eng-1.md": CLEAN_FIXTURE.read_text()})
    assert main(["--base", base, "--head", head, "--branch", "team/eng-1"]) == 0
    assert capsys.readouterr().out.splitlines() == ["Changelog fragments: 1 added, 0 problems"]


@pytest.mark.feature("changelog-fragment-required-on-pull-request")
def test_a_pull_request_that_adds_only_a_misnamed_fragment_is_red_naming_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    base, head = _trees(tmp_path, {"changelog.d/notes.md": CLEAN_FIXTURE.read_text()})
    assert main(["--base", base, "--head", head, "--branch", "team/eng-1"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "Changelog fragments: 1 added, 2 problems",
        "::error::changelog.d/notes.md: a pull request from the branch team/eng-1 adds its fragment as "
        "changelog.d/team-eng-1.md",
        "::error::changelog.d/team-eng-1.md: a pull request from the branch team/eng-1 adds no fragment at this path",
    ]
