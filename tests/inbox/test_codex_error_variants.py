"""Codex error variants the account refusal classifier and the test fixtures rely on stay in the app server schema."""

import ast
import json
import os
import runpy
import subprocess
import sys
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
                {"type": "object", "properties": {"httpConnectionFailed": {"properties": {"httpStatusCode": {}}}}},
                {"type": ["string", "object"]},
            ]
        }
    }
}
WRITTEN = '[\n  "badRequest",\n  "httpConnectionFailed",\n  "usageLimitExceeded"\n]\n'


def snapshot() -> set[str]:
    return set(json.loads(SNAPSHOT.read_text()))


def is_info(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == INFO


def assigned(target: ast.expr, value: ast.expr | None) -> list[ast.expr]:
    """What one assignment target writes under codexErrorInfo: the value, or the variant key its subscripts name."""
    if value is None:
        return []
    written = value
    while isinstance(target, ast.Subscript):
        if is_info(target.slice):
            return [written]
        written, target = target.slice, target.value
    return []


def values(node: ast.AST) -> list[ast.expr]:
    """The values one node writes under codexErrorInfo: a dict entry, a keyword argument or an assignment."""
    if isinstance(node, ast.Dict):
        return [value for key, value in zip(node.keys, node.values) if is_info(key)]
    if isinstance(node, ast.keyword) and node.arg == INFO:
        return [node.value]
    if isinstance(node, ast.Assign):
        return [written for target in node.targets for written in assigned(target, node.value)]
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return assigned(node.target, node.value)
    return []


def names(value: ast.expr) -> set[str] | None:
    """The variant names a codexErrorInfo literal writes, a string or the string keys of a dict, else None."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}
    if isinstance(value, ast.Dict):
        keys = {key.value for key in value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        return keys if len(keys) == len(value.keys) else None
    return None


def fixture_values() -> list[tuple[str, ast.expr]]:
    """Every value the test sources write under codexErrorInfo, with the file and line that writes it."""
    found: list[tuple[str, ast.expr]] = []
    for path in TESTS.rglob("*.py"):
        source = path.read_bytes()
        if INFO.encode() in source:
            nodes = ast.walk(ast.parse(source))
            found += [(f"{path.relative_to(TESTS)}:{value.lineno}", value) for node in nodes for value in values(node)]
    return found


def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    """Put a codex on PATH that writes SCHEMA for the schema generator call, then exits with status."""
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(SCHEMA))
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$*" == "app-server generate-json-schema --out $4" ]] || exit 2\n'
        f'cp "{schema}" "$4/codex_app_server_protocol.v2.schemas.json"\n'
        f"exit {status}\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}{os.pathsep}{os.environ['PATH']}")


def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from tmp_path as the repository root and return the snapshot path there."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests" / "inbox").mkdir(parents=True)
    return tmp_path / "tests" / "inbox" / "codex_error_variants.json"


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_account_refusal_variant_is_listed_by_the_app_server_schema() -> None:
    assert codex.UNAUTHORIZED in snapshot()


@pytest.mark.feature("codex-error-variant-snapshot")
def test_every_variant_a_test_fixture_writes_is_listed_by_the_app_server_schema() -> None:
    """Every codexErrorInfo value in the tests is a literal; the placeholder x stays a variant upstream never lists."""
    written = fixture_values()
    assert [site for site, value in written if names(value) is None] == []
    used = {name for _, value in written for name in names(value) or ()}
    assert {codex.UNAUTHORIZED, "httpConnectionFailed", "responseStreamDisconnected"} <= used, f"{len(written)} sites"
    assert sorted(used - UNLISTED - snapshot()) == []
    assert not UNLISTED & snapshot()


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_snapshot_holds_what_the_script_writes() -> None:
    assert SNAPSHOT.read_text() == json.dumps(sorted(snapshot()), indent=2) + "\n"


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_script_writes_the_sorted_variant_names_the_installed_codex_generates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_codex(tmp_path, monkeypatch, 0)
    written_to = repository(tmp_path, monkeypatch)
    assert codex_error_variants.main() == 0
    assert written_to.read_text() == WRITTEN


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_module_entry_point_exits_with_the_status_of_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_codex(tmp_path, monkeypatch, 0)
    written_to = repository(tmp_path, monkeypatch)
    monkeypatch.delitem(sys.modules, codex_error_variants.__name__)
    with pytest.raises(SystemExit) as exited:
        runpy.run_module(codex_error_variants.__name__, run_name="__main__")
    assert (exited.value.code, written_to.read_text()) == (0, WRITTEN)


@pytest.mark.feature("codex-error-variant-snapshot")
def test_the_script_writes_nothing_when_codex_fails_to_generate_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_codex(tmp_path, monkeypatch, 1)
    written_to = repository(tmp_path, monkeypatch)
    with pytest.raises(subprocess.CalledProcessError) as failed:
        codex_error_variants.main()
    assert (failed.value.returncode, written_to.exists()) == (1, False)
