import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scripts import pr_body

pytestmark = pytest.mark.unit

SECTIONS = (
    "Assumptions",
    "What could break",
    "What will not break",
    "Features that may disconnect",
    "Dependencies",
    "Resources",
    "Why the fix",
    "Why the feature",
    "Notes for the reviewer",
)


@pytest.fixture
def api() -> ModuleType:
    return pr_body


def body_without(omitted: str = "") -> str:
    return "\n\n".join(f"## {name}\n\nEvidence for {name}." for name in SECTIONS if name != omitted)


def recon_text() -> str:
    sections = [
        ("What will be built", SECTIONS[:1] + SECTIONS[6:8]),
        ("Code areas touched", SECTIONS[3:5]),
        ("What might break", SECTIONS[1:2]),
        ("Regression tests", SECTIONS[8:]),
        ("Resources needed", SECTIONS[5:6]),
        ("Protection plan", SECTIONS[2:3]),
    ]
    return "\n\n".join(
        f"## {heading}\n\n" + "\n\n".join(f"### {name}\n\nRecon evidence for {name}." for name in names)
        for heading, names in sections
    )


@pytest.mark.feature("pull-request-recon-context")
def test_complete_body_passes(api: ModuleType) -> None:
    assert api.missing_sections("## Summary\n\nRetained.\n\n" + body_without()) == []


@pytest.mark.feature("pull-request-recon-context")
@pytest.mark.parametrize("name", SECTIONS)
def test_each_missing_section_is_named(api: ModuleType, name: str) -> None:
    assert api.missing_sections(body_without(name)) == [name]


@pytest.mark.feature("pull-request-recon-context")
@pytest.mark.parametrize(
    "empty",
    ["", " \n\t", "<!-- fill this section -->", "<!-- first\nsecond -->"],
    ids=["blank", "space", "comment", "multiline-comment"],
)
def test_empty_sections_are_named(api: ModuleType, empty: str) -> None:
    assert api.missing_sections(f"## Assumptions\n{empty}\n\n" + body_without("Assumptions")) == ["Assumptions"]


@pytest.mark.feature("pull-request-recon-context")
def test_fenced_and_commented_headings_do_not_supply_sections(api: ModuleType) -> None:
    text = "<!--\n" + body_without() + "\n-->\n```markdown\n" + body_without() + "\n```\n"
    assert api.missing_sections(text) == list(SECTIONS)


@pytest.mark.feature("pull-request-recon-context")
def test_unclosed_comments_do_not_supply_sections(api: ModuleType) -> None:
    assert api.missing_sections("<!--\n" + body_without()) == list(SECTIONS)
    assert api.missing_sections(body_without().replace("## ", "<!-- explanation -->\n## ")) == []


@pytest.mark.feature("pull-request-recon-context")
@pytest.mark.parametrize("fence", ["```", "~~~~"], ids=["backtick", "tilde"])
def test_fenced_comment_openers_preserve_visible_sections(api: ModuleType, fence: str) -> None:
    text = f"## Assumptions\n\n{fence}html\n<!-- literal example\n{fence}\n\n" + body_without("Assumptions")
    assert api.missing_sections(text) == []


@pytest.mark.feature("pull-request-recon-context")
@pytest.mark.parametrize(
    ("delimiter", "example"), [("`", "<!--"), ("``", "<!-- ` literal")], ids=["single", "multiple"]
)
def test_inline_comment_examples_preserve_visible_sections(api: ModuleType, delimiter: str, example: str) -> None:
    text = f"## Assumptions\n\n{delimiter}{example}{delimiter} is literal code.\n\n" + body_without("Assumptions")
    assert api.missing_sections(text) == []


@pytest.mark.feature("pull-request-recon-context")
@pytest.mark.parametrize(
    "example",
    ["> ## Assumptions\n> Keep this quoted example.", "- ## Assumptions\n\n  Keep this list example."],
    ids=["quote", "list"],
)
def test_nested_heading_examples_are_preserved(api: ModuleType, example: str) -> None:
    assert api.missing_sections(example + "\n\n" + body_without("Assumptions")) == ["Assumptions"]
    original = f"## Summary\n\n{example}\n\n## Checklist\n\n- [x] Reviewed.\n"
    filled = api.fill(recon_text(), original)
    assert example in filled
    assert "## Summary\n" in filled
    assert "- [x] Reviewed." in filled
    assert api.missing_sections(filled) == []


@pytest.mark.feature("pull-request-recon-context")
def test_fill_extracts_recon_and_preserves_body(api: ModuleType) -> None:
    original = (
        "## Summary\n\nKeep this evidence.\n\n## Assumptions\n\n<!-- fill -->\n\n## Checklist\n\n- [x] Reviewed.\n"
    )
    filled = api.fill(recon_text(), original)
    assert api.missing_sections(filled) == []
    assert "Keep this evidence." in filled
    assert "- [x] Reviewed." in filled
    for name in SECTIONS:
        assert filled.count(f"## {name}\n") == 1
        assert f"Recon evidence for {name}." in filled
    assert api.fill(recon_text(), filled) == filled


def intent_block() -> str:
    return "<!-- agentihooks intent -->\n## Parent intent\n\n**Project:** Kept intent.\n<!-- /agentihooks intent -->"


@pytest.mark.feature("pull-request-recon-context")
@pytest.mark.parametrize(
    "layout,tail",
    [
        ("{sections}\n\n{intent}\n", ""),
        ("{intent}\n\n{sections}\n", ""),
        ("{sections}\n\n{intent}\n\n## Checklist\n\n- [x] Reviewed.\n", "\n\n## Checklist\n\n- [x] Reviewed."),
    ],
)
def test_fill_keeps_the_intent_block_whole(api: ModuleType, layout: str, tail: str) -> None:
    original = "## Summary\n\nKeep this evidence.\n\n" + layout.format(sections=body_without(), intent=intent_block())
    filled = api.fill(recon_text(), original)
    assert filled == f"## Summary\n\nKeep this evidence.\n\n{api.fill(recon_text())}\n{intent_block()}{tail}\n"
    assert api.fill(recon_text(), filled) == filled


@pytest.mark.feature("pull-request-recon-context")
def test_fill_before_an_intent_block_alone(api: ModuleType) -> None:
    assert api.fill(recon_text(), f"{intent_block()}\n") == f"{api.fill(recon_text())}\n{intent_block()}\n"


@pytest.mark.feature("pull-request-recon-context")
def test_fill_refuses_missing_recon_details(api: ModuleType) -> None:
    recon = recon_text().replace("### Resources\n\nRecon evidence for Resources.", "Production data unchanged.")
    with pytest.raises(ValueError, match="Resources"):
        api.fill(recon)


@pytest.mark.feature("pull-request-recon-context")
def test_check_event_names_every_missing_section(
    api: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"body": "## Summary\n\nA change."}}))
    assert api.main(["check", "--event", str(event)]) == 1
    assert capsys.readouterr().out.splitlines() == [f"Missing or empty section: {name}" for name in SECTIONS]
    event.write_text(json.dumps({"pull_request": {"body": body_without()}}))
    assert api.main(["check", "--event", str(event)]) == 0
    assert capsys.readouterr().out == "Pull request body sections passed.\n"


@pytest.mark.feature("pull-request-recon-context")
def test_fill_command_uses_recon_fixture(api: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    recon = tmp_path / "recon.md"
    body = tmp_path / "body.md"
    recon.write_text(recon_text())
    body.write_text("## Summary\n\nOriginal evidence.\n")
    assert api.main(["fill", "--recon", str(recon), "--body", str(body)]) == 0
    assert capsys.readouterr().out == body.read_text()
    assert api.missing_sections(body.read_text()) == []
    assert "Original evidence." in body.read_text()


@pytest.mark.feature("pull-request-recon-context")
def test_installed_command_ignores_a_foreign_scripts_package(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    package = foreign / "scripts"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('raise RuntimeError("foreign scripts imported")\n')
    body = tmp_path / "body.md"
    body.write_text(body_without())
    shadowed = subprocess.run(
        [sys.executable, "-m", "scripts.pr_body", "check", "--body", str(body)],
        cwd=foreign,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert shadowed.returncode != 0
    assert "foreign scripts imported" in shadowed.stderr
    command = shutil.which("agentihooks-pr-body")
    assert command is not None
    checked = subprocess.run(
        [command, "check", "--body", str(body)],
        cwd=foreign,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout == "Pull request body sections passed.\n"


@pytest.mark.feature("pull-request-recon-context")
def test_invalid_recon_is_refused(api: ModuleType) -> None:
    with pytest.raises(ValueError, match="Invalid recon"):
        api.fill("## What will be built\n\nIncomplete recon.")


@pytest.mark.feature("pull-request-recon-context")
def test_body_file_check_and_new_body_fill(api: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    recon = tmp_path / "recon.md"
    body = tmp_path / "body.md"
    recon.write_text(recon_text())
    assert api.main(["fill", "--recon", str(recon), "--body", str(body)]) == 0
    assert capsys.readouterr().out == body.read_text()
    assert api.main(["check", "--body", str(body)]) == 0
    assert capsys.readouterr().out == "Pull request body sections passed.\n"


@pytest.mark.feature("pull-request-recon-context")
@pytest.mark.parametrize(
    "payload",
    ["{", "{}", '{"pull_request": {"body": 42}}', '{"pull_request": []}'],
    ids=["malformed", "missing-body", "wrong-type", "wrong-container"],
)
def test_bad_event_fails_closed(
    api: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    event = tmp_path / "event.json"
    event.write_text(payload)
    assert api.main(["check", "--event", str(event)]) == 1
    assert capsys.readouterr().out.startswith("Pull request body error: ")


@pytest.mark.feature("pull-request-recon-context")
def test_null_body_names_all_sections(api: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    event = tmp_path / "event.json"
    event.write_text('{"pull_request": {"body": null}}')
    assert api.main(["check", "--event", str(event)]) == 1
    assert capsys.readouterr().out.splitlines() == [f"Missing or empty section: {name}" for name in SECTIONS]
