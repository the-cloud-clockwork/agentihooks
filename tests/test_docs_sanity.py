"""The docs sanity scanner flags internal addresses, host names, personal emails, token shapes and private keywords."""

from pathlib import Path

import pytest

from scripts import docs_sanity

FIXTURES = Path(__file__).parent / "fixtures" / "docs_sanity"
TAIL = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"


@pytest.mark.feature("docs-sanity-scan")
def test_the_red_fixture_fails_with_every_hit_as_file_line_rule(capsys: pytest.CaptureFixture[str]) -> None:
    red = (FIXTURES / "red.md").as_posix()

    assert docs_sanity.main([red]) == 1

    assert capsys.readouterr().out.splitlines() == [
        f"{red}:3: private-ipv4",
        f"{red}:4: private-ipv4",
        f"{red}:5: email",
        f"{red}:6: private-host",
    ]


@pytest.mark.feature("docs-sanity-scan")
def test_the_clean_fixture_passes_with_documentation_values(capsys: pytest.CaptureFixture[str]) -> None:
    assert docs_sanity.main([(FIXTURES / "clean.md").as_posix()]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.feature("docs-sanity-scan")
@pytest.mark.parametrize(
    ("rule", "sample"),
    [
        ("github-token", "ghp" + "_" + TAIL),
        ("aws-access-key", "AKIA" + "Q7R8S9T0U1V2W3X4"),
        ("slack-token", "xox" + "b-1234567890-abcdefghij"),
        ("anthropic-key", "sk-" + "ant-" + TAIL),
        ("openai-key", "sk" + "-" + TAIL),
        ("google-api-key", "AIza" + TAIL[:35]),
        ("jwt", "eyJ" + TAIL[:12] + ".eyJ" + TAIL[:12] + "." + TAIL[:12]),
        ("private-key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ],
)
def test_each_token_shape_is_flagged_by_its_own_rule(rule: str, sample: str) -> None:
    assert docs_sanity.line_rules(f"value {sample} here", ()) == [rule]


@pytest.mark.feature("docs-sanity-scan")
def test_a_vendor_sample_key_marked_example_is_not_a_token() -> None:
    assert docs_sanity.line_rules("key " + "AKIA" + "IOSFODNN7" + "EXAMPLE", ()) == []


@pytest.mark.feature("docs-sanity-scan")
def test_keywords_from_the_environment_and_a_file_hit_without_printing_the_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "page.md"
    doc.write_text("clean line\nmentions Projectwombat once\nand quokkahost twice\n", encoding="utf-8")
    words = tmp_path / "words.txt"
    words.write_text("# private\nquokkahost\n", encoding="utf-8")
    monkeypatch.setenv("DOCS_SANITY_KEYWORDS", "projectwombat, unused")

    code = docs_sanity.main(["--keywords-env", "DOCS_SANITY_KEYWORDS", "--keywords-file", str(words), str(doc)])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.splitlines() == [f"{doc.as_posix()}:2: keyword", f"{doc.as_posix()}:3: keyword"]
    assert "wombat" not in captured.out + captured.err
    assert "3 private keyword(s) loaded" in captured.err


@pytest.mark.feature("docs-sanity-scan")
def test_directories_are_walked_and_excluded_globs_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.md").write_text("host at 10.9.8.7\n", encoding="utf-8")
    (tmp_path / "skip.md").write_text("host at 10.9.8.7\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00 10.9.8.7")

    code = docs_sanity.main(["--exclude", "*/skip.md", str(tmp_path)])

    assert code == 1
    assert capsys.readouterr().out.splitlines() == [f"{(tmp_path / 'nested' / 'a.md').as_posix()}:1: private-ipv4"]


@pytest.mark.feature("docs-sanity-scan")
def test_a_missing_path_is_a_usage_error(tmp_path: Path) -> None:
    assert docs_sanity.main([str(tmp_path / "absent")]) == 2
