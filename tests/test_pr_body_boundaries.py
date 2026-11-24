import io
import json
from pathlib import Path

import pytest

from scripts import pr_body
from tests.test_pr_body import SECTIONS, body_without, recon_text

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "argv",
    [[], ["check"], ["fill", "--body", "body.md"], ["fill", "--recon", "recon.md"]],
    ids=["command", "source", "recon", "body"],
)
@pytest.mark.feature("pull-request-recon-context")
def test_required_command_and_sources_are_usage_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        pr_body.main(argv)
    assert error.value.code == 2


@pytest.mark.feature("pull-request-recon-context")
def test_wrong_body_type_names_the_contract(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    event = tmp_path / "event.json"
    event.write_text('{"pull_request": {"body": 42}}', encoding="utf-8")
    assert pr_body.main(["check", "--event", str(event)]) == 1
    assert capsys.readouterr().out == "Pull request body error: pull_request.body must be a string or null\n"


@pytest.mark.feature("pull-request-recon-context")
def test_fill_without_an_existing_body_has_only_recon_sections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = "\n\n".join(f"## {name}\n\nRecon evidence for {name}." for name in SECTIONS) + "\n"
    assert pr_body.fill(recon_text()) == expected
    recon = tmp_path / "recon.md"
    body = tmp_path / "body.md"
    recon.write_text(recon_text(), encoding="utf-8")
    assert pr_body.main(["fill", "--recon", str(recon), "--body", str(body)]) == 0
    assert body.read_bytes() == expected.encode("utf-8")
    assert capsys.readouterr().out == expected


@pytest.mark.feature("pull-request-recon-context")
def test_multiple_missing_recon_details_are_named_in_order() -> None:
    recon = recon_text()
    for name in ("Assumptions", "Resources"):
        recon = recon.replace(f"### {name}\n\nRecon evidence for {name}.", "Review evidence pending.")
    with pytest.raises(ValueError) as error:
        pr_body.fill(recon)
    assert str(error.value) == "Recon review details missing or empty: Assumptions, Resources"


@pytest.mark.feature("pull-request-recon-context")
def test_utf8_author_evidence_survives_an_ascii_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def text_encoding(encoding: str | None, stacklevel: int = 2) -> str:
        return "ascii" if encoding is None else encoding

    monkeypatch.setattr(io, "text_encoding", text_encoding)
    event = tmp_path / "event.json"
    body = tmp_path / "body.md"
    recon = tmp_path / "recon.md"
    evidence = "Café evidence."
    text = body_without() + f"\n{evidence}\n"
    event.write_bytes(json.dumps({"pull_request": {"body": text}}, ensure_ascii=False).encode("utf-8"))
    assert pr_body.main(["check", "--event", str(event)]) == 0
    assert capsys.readouterr().out == "Pull request body sections passed.\n"
    body.write_bytes(text.encode("utf-8"))
    assert pr_body.main(["check", "--body", str(body)]) == 0
    assert capsys.readouterr().out == "Pull request body sections passed.\n"
    original = f"## Summary\n\n{evidence}\n"
    body.write_bytes(original.encode("utf-8"))
    recon.write_bytes(recon_text().replace("Recon evidence", "Café recon evidence").encode("utf-8"))
    expected = (
        original.rstrip()
        + "\n\n"
        + "\n\n".join(f"## {name}\n\nCafé recon evidence for {name}." for name in SECTIONS)
        + "\n"
    )
    assert pr_body.main(["fill", "--recon", str(recon), "--body", str(body)]) == 0
    assert body.read_bytes() == expected.encode("utf-8")
    assert capsys.readouterr().out == expected
