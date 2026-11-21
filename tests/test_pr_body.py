import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

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
    return importlib.import_module("scripts.pr_body")


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
