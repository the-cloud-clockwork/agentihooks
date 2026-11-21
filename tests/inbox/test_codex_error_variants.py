"""The Codex error variants the account refusal classifier and the test fixtures rely on stay in the app server schema."""

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import codex_error_variants
from scripts.inbox import codex

TESTS = Path(__file__).resolve().parents[1]
SNAPSHOT = Path(__file__).with_name("codex_error_variants.json")
INFO = "codexErrorInfo"
UNLISTED = frozenset({"x"})
SCHEMA = {
    "definitions": {
        "CodexErrorInfo": {
            "anyOf": [
                {"type": "string", "enum": ["usageLimitExceeded", "badRequest"]},
                {
                    "type": "object",
                    "required": ["httpConnectionFailed"],
                    "properties": {"httpConnectionFailed": {"properties": {"httpStatusCode": {}}}},
                },
                {"type": ["string", "object"]},
            ]
        }
    }
}


def snapshot() -> set[str]:
    return set(json.loads(SNAPSHOT.read_text()))


def is_info(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == INFO


def names(value: ast.expr) -> set[str]:
    """The variant names a codexErrorInfo value literal writes: a string, or the string keys of a dict."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}
    if isinstance(value, ast.Dict):
        return {key.value for key in value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    return set()


def written(node: ast.AST) -> set[str]:
    """The variant names one node writes as a codexErrorInfo dict entry or subscript assignment."""
    if isinstance(node, ast.Dict):
        return {name for key, value in zip(node.keys, node.values) if is_info(key) for name in names(value)}
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Subscript) and is_info(t.slice) for t in node.targets):
        return names(node.value)
    return set()


def fixture_variants() -> set[str]:
    """Every codexErrorInfo variant the test sources write."""
    sources = (path.read_bytes() for path in TESTS.rglob("*.py"))
    return {
        name
        for source in sources
        if INFO.encode() in source
        for node in ast.walk(ast.parse(source))
        for name in written(node)
    }


def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int) -> Path:
    """Put a codex on PATH that writes SCHEMA for the schema generator call, chdir to tmp_path and return the snapshot path."""
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$*" == "app-server generate-json-schema --out $4" ]] || exit 2\n'
        f'cp "{schema}" "$4/{codex_error_variants.SCHEMA}"\n'
        f"exit {status}\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.chdir(tmp_path)
    (tmp_path / codex_error_variants.SNAPSHOT).parent.mkdir(parents=True)
    return tmp_path / codex_error_variants.SNAPSHOT


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_account_refusal_variant_is_listed_by_the_app_server_schema() -> None:
    assert codex.UNAUTHORIZED in snapshot()


@pytest.mark.feature("codex-error-variant-snapshot")
def test_every_variant_a_test_fixture_writes_is_listed_by_the_app_server_schema() -> None:
    """The placeholder x stands for a variant upstream does not list, so it must stay out of the schema."""
    used = fixture_variants()
    assert {codex.UNAUTHORIZED, "httpConnectionFailed", "responseStreamDisconnected"} <= used
    assert sorted(used - UNLISTED - snapshot()) == []
    assert not UNLISTED & snapshot()


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_snapshot_holds_what_the_script_writes() -> None:
    assert SNAPSHOT.read_text() == json.dumps(sorted(snapshot()), indent=2) + "\n"


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_script_writes_the_sorted_variant_names_the_installed_codex_generates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_to = fake_codex(tmp_path, monkeypatch, 0)
    assert codex_error_variants.main() == 0
    assert written_to.read_text() == '[\n  "badRequest",\n  "httpConnectionFailed",\n  "usageLimitExceeded"\n]\n'


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_script_writes_nothing_when_codex_fails_to_generate_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written_to = fake_codex(tmp_path, monkeypatch, 1)
    with pytest.raises(subprocess.CalledProcessError):
        codex_error_variants.main()
    assert not written_to.exists()
