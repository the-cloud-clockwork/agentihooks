"""The docs sanity scanner flags internal addresses, host names, personal emails, token shapes and private keywords."""

import codecs
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
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary 10.9.8.7")

    code = docs_sanity.main(["--exclude", "*/skip.md", str(tmp_path)])

    assert code == 1
    assert capsys.readouterr().out.splitlines() == [f"{(tmp_path / 'nested' / 'a.md').as_posix()}:1: private-ipv4"]


@pytest.mark.feature("docs-sanity-scan")
def test_an_example_label_passes_only_right_before_the_private_suffix() -> None:
    assert docs_sanity.line_rules("swarm.example.internal", ()) == []
    assert docs_sanity.line_rules("db.example.corp.lan", ()) == ["private-host"]
    assert docs_sanity.line_rules("example.internal and nats.nats.svc.cluster.local", ()) == []
    assert docs_sanity.line_rules("a dot file named .settings.local, a module x.local_entry", ()) == []
    assert docs_sanity.line_rules("reach buildbox.lan.", ()) == ["private-host"]


@pytest.mark.feature("docs-sanity-scan")
def test_a_text_file_that_is_not_utf8_is_still_scanned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    page = tmp_path / "a-latin.md"
    page.write_bytes("café at 10.9.8.7\n".encode("latin-1"))
    little = tmp_path / "b-little.md"
    little.write_bytes(
        codecs.BOM_UTF16_LE + "x".encode("utf-16-le") + b"\x00\xd8" + "\nhost at 10.9.8.7\n".encode("utf-16-le")
    )
    big = tmp_path / "c-big.md"
    big.write_bytes(codecs.BOM_UTF16_BE + "title\nhost at 10.9.8.7\n".encode("utf-16-be"))

    assert docs_sanity.main([str(page), str(little), str(big)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        f"{page.as_posix()}:1: private-ipv4",
        f"{little.as_posix()}:2: private-ipv4",
        f"{big.as_posix()}:2: private-ipv4",
    ]


@pytest.mark.feature("docs-sanity-scan")
def test_a_missing_path_is_a_usage_error(tmp_path: Path) -> None:
    assert docs_sanity.main([str(tmp_path / "absent")]) == 2
    assert docs_sanity.main(["--keywords-file", str(tmp_path / "absent.txt"), str(tmp_path)]) == 2


@pytest.mark.feature("docs-sanity-scan")
def test_every_missing_path_is_named_on_standard_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first, second = tmp_path / "one", tmp_path / "two"

    assert docs_sanity.main([str(first), str(second)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"docs sanity: no such path: {first}, {second}\n"


@pytest.mark.feature("docs-sanity-scan")
def test_no_keyword_source_loads_no_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCS_SANITY_UNSET", raising=False)

    assert docs_sanity.load_keywords(None, None) == []
    assert docs_sanity.load_keywords("DOCS_SANITY_UNSET", None) == []


@pytest.mark.feature("docs-sanity-scan")
@pytest.mark.parametrize(
    "line",
    [
        "bad 999.1.1.1 then 10.1.1.1",
        "ask team@example.com or jane.doe@mailhost.io",
        "clone git@github.com:o/r.git and mail jane.doe@mailhost.io",
    ],
)
def test_a_passing_match_does_not_hide_a_later_hit_on_the_line(line: str) -> None:
    assert len(docs_sanity.line_rules(line, ())) == 1


@pytest.mark.feature("docs-sanity-scan")
@pytest.mark.parametrize(
    ("line", "rules"),
    [
        ("see 10.0.0.5.", ["private-ipv4"]),
        ("at 10.0.0.5:8080", ["private-ipv4"]),
        ("version 10.1.2.3.4 and 1000.1.1.1", []),
        ("a@b.io", ["email"]),
        ("mail jane@mailhost.io.", ["email"]),
        ("@mailhost.io and x@y and x@y.c and x@y.12 and x@.io and x@y..io", []),
        ("https://user:pw@github.com/org/repo.git", []),
        ("scp o/r-git@github.com:o/r.git", []),
        ("git@github.com:org/repo.git", []),
        ("--flag.local -cache.lan", []),
        ("swarm.example.internal then buildbox.lan", ["private-host"]),
        ("BuildBox.LAN", ["private-host"]),
    ],
)
def test_each_edge_of_the_built_in_rules(line: str, rules: list[str]) -> None:
    assert docs_sanity.line_rules(line, ()) == rules
