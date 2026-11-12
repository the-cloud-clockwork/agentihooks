"""The docs sanity scanner flags internal addresses, host names, personal emails, token shapes and private keywords."""

import codecs
import json
from pathlib import Path

import pytest

from scripts import docs_sanity

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ("docs", "evidence", "changelog.d", "README.md")
BASELINE = ROOT / "tests" / "DOCS_SANITY_BASELINE.json"
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
        f"{red}:7: lab-name",
        f"{red}:8: lab-name",
        f"{red}:9: cloud-host",
        f"{red}:10: private-ipv6",
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
        ("user:pw@mailhost.io and x@y@mailhost.io and x@host.mailhost.i0", []),
        ("scp o/r-git@github.com:o/r.git", []),
        ("git@github.com:org/repo.git", []),
        ("--flag.local -cache.lan", []),
        ("swarm.example.internal then buildbox.lan", ["private-host"]),
        ("BuildBox.LAN", ["private-host"]),
    ],
)
def test_each_edge_of_the_built_in_rules(line: str, rules: list[str]) -> None:
    assert docs_sanity.line_rules(line, ()) == rules


@pytest.mark.feature("docs-sanity-scan")
@pytest.mark.parametrize(
    ("line", "rules"),
    [
        ("deployed by the Anton cluster", ["lab-name"]),
        ("the anton_hive and anton2 nodes", ["lab-name"]),
        ("clone the-cloud-clockwork/AntonCore", ["lab-name"]),
        ("an antonym from Antonio in Santon", []),
        ("node ip-192-168-1-20 is up", ["cloud-host"]),
        ("node ip-203-0-113-9 is up", []),
        ("instance i-0a1b2c3d4e5f60718 restarted", ["cloud-host"]),
        ("build i-12345 and sha 0a1b2c3d4e5f60718aa", []),
        ("legacy instance i-0a1b2c3d", ["cloud-host"]),
        ("peer fd00:1234::5", ["private-ipv6"]),
        ("peer fd00::5", ["private-ipv6"]),
        ("at 12:30 and ratio 1:2:3 the peer fd00::5", ["private-ipv6"]),
        ("docs 2001:db8::1, loopback ::1 and time 12:30:45", []),
    ],
)
def test_each_edge_of_the_lab_host_and_ipv6_rules(line: str, rules: list[str]) -> None:
    assert docs_sanity.line_rules(line, ()) == rules


@pytest.mark.feature("docs-sanity-scan")
@pytest.mark.parametrize(
    "line",
    [
        "set DB_" + "PASS" + "WORD=" + TAIL[:14],
        "api_" + "key: '" + TAIL[:20] + "'",
        '"auth' + 'Token": "' + TAIL[:24] + '"',
        "Authorization: " + "Bea" + "rer " + TAIL[:20],
        "clone https://bot:" + TAIL[:12] + "@git.example.com/r.git",
        "pass" + "word_valid: " + TAIL[:11],
        "api_token_hid=" + TAIL[:10],
        "tok" + "en: " + TAIL[:8],
    ],
)
def test_a_password_or_token_like_value_is_flagged(line: str) -> None:
    assert docs_sanity.line_rules(line, ()) == ["secret-value"]


@pytest.mark.feature("docs-sanity-scan")
@pytest.mark.parametrize(
    "line",
    [
        "token: ${{ secrets.GH_TOKEN }}",
        "pass" + "word: <your password>",
        "api_key=$API_KEY and secret_ref=MY_SECRET_NAME",
        "Authorization: " + "Bea" + "rer $TOKEN",
        "pass" + "wd=1:4.13+dfsg1-1 is a package version",
        "token_budget: 50000 and sec" + "ret=get_secret",
        "https://user:secret-placeholder@github.com/org/repo.git",
        "token_file=/run/secrets/x1y2z3w4",
        "secretName: ingress-tls2 and tokenRef: vault-ref1",
        "tok" + "en: " + TAIL[:7],
        "tok" + "en: ~/keys/a1b2c3d4e5",
        "tok" + "en: TheExample123",
    ],
)
def test_a_placeholder_reference_or_version_is_not_a_secret(line: str) -> None:
    assert docs_sanity.line_rules(line, ()) == []


@pytest.mark.feature("docs-sanity-scan")
def test_a_baseline_holds_existing_lab_names_and_fails_only_on_growth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    page = tmp_path / "page.md"
    page.write_text("the anton lab\nanton again\nhost 10.1.2.3\n", encoding="utf-8")
    baseline = tmp_path / "baseline.json"

    assert docs_sanity.main(["--write-baseline", str(baseline), str(page)]) == 1
    assert baseline.read_text() == json.dumps({page.as_posix(): {"lab-name": 2}}, indent=2) + "\n"
    capsys.readouterr()
    assert docs_sanity.main(["--baseline", str(baseline), str(page)]) == 1
    assert "docs sanity: 1 hit(s), 2 held by the baseline" in capsys.readouterr().err

    page.write_text("the anton lab\nanton again\n", encoding="utf-8")
    assert docs_sanity.main(["--baseline", str(baseline), str(page)]) == 0
    page.write_text("the lab\n", encoding="utf-8")
    assert docs_sanity.main(["--baseline", str(baseline), str(page)]) == 0
    page.write_text("anton\nanton\nanton\n", encoding="utf-8")
    assert docs_sanity.main(["--baseline", str(baseline), str(page)]) == 1
    assert capsys.readouterr().out.splitlines()[-3:] == [f"{page.as_posix()}:{n}: lab-name" for n in (1, 2, 3)]


@pytest.mark.feature("docs-sanity-scan")
def test_a_baseline_never_holds_addresses_or_secrets(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("host 10.1.2.3\n", encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({page.as_posix(): {"private-ipv4": 5, "lab-name": 1}}), encoding="utf-8")

    assert docs_sanity.load_baseline(str(baseline)) == {page.as_posix(): {"lab-name": 1}}
    assert docs_sanity.main(["--baseline", str(baseline), str(page)]) == 1
    assert docs_sanity.main(["--baseline", str(tmp_path / "absent.json"), str(page)]) == 2


@pytest.mark.feature("docs-sanity-scan")
def test_the_public_docs_pass_and_the_baseline_matches_them_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    present = tuple(path for path in PUBLIC if (ROOT / path).exists())
    found = docs_sanity.scan(present, ())
    held = {
        path: rules
        for path, rules in json.loads(BASELINE.read_text()).items()
        if any(path == root or path.startswith(root + "/") for root in present)
    }

    assert "docs" in present
    assert docs_sanity.beyond_baseline(found, docs_sanity.load_baseline(str(BASELINE))) == []
    assert docs_sanity.count_hits(found) == held


@pytest.mark.feature("docs-sanity-scan")
def test_a_written_baseline_sorts_rules_and_indents_two_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    page = tmp_path / "page.md"
    page.write_text("the anton quokkahost\n", encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    monkeypatch.setenv("DOCS_SANITY_KEYWORDS", "quokkahost")

    assert (
        docs_sanity.main(["--keywords-env", "DOCS_SANITY_KEYWORDS", "--write-baseline", str(baseline), str(page)]) == 1
    )
    assert baseline.read_text() == (
        "{\n" + f'  "{page.as_posix()}": {{\n' + '    "keyword": 1,\n    "lab-name": 1\n  }\n}\n'
    )
    capsys.readouterr()
