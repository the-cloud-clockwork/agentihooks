import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import digests
from scripts.ci_mutation import stats_cache
from scripts.ci_mutation.stats_dependencies import StoredStats
from scripts.dep_manifest import MODULE, unit_hashes

DOC = Path(__file__).resolve().parents[2] / "docs" / "reference" / "ci-lab.md"

TREE = {
    "pyproject.toml": b'[project]\nname = "fixture"\nversion = "0.1.0"\n',
    "uv.lock": b'version = 1\n\n[[package]]\nname = "fixture"\nversion = "0.1.0"\n',
    "scripts/app.py": b"import os\n\nHOME = os.environ.get('HOME', '')\n\n\ndef answer() -> int:\n    return 42\n",
    "tests/test_app.py": b"from scripts.app import answer\n\n\ndef test_answer() -> None:\n    assert answer() == 42\n",
}
BLOBS = {path: digests.blob_id(data) for path, data in TREE.items()}
UNITS = unit_hashes(TREE["scripts/app.py"].decode())
SHA256 = {path: hashlib.sha256(data).hexdigest() for path, data in TREE.items()}

KINDS = ("static", "test", "stats", "mutant", "grade")
RECORDS = {
    "static": digests.StaticInputs,
    "test": digests.TestInputs,
    "stats": digests.StatsInputs,
    "mutant": digests.MutantInputs,
    "grade": digests.GradeInputs,
    "toolchain": digests.Toolchain,
}
GOLDEN = {
    "static": "static-v1-4b187fa1f86aed0defbd09ab7e347535ff7dc1ebe68ce7632699bb2dcae59ffb",
    "test": "test-v1-28f019682121264e77614e24d678af401a7a77368780a802738e2ec4ce4c0b77",
    "stats": "stats-v4-575ab5687b8d822c805980e1ad15d07559b0bf7f37e60b03e2c48ee53c714d5a",
    "mutant": "mutant-v1-d7fa3cbcb90e677e8e1844e25015e79435052a2a335515e5f8f4a4cb5519509b",
    "grade": "grade-v1-3239f51cc667199b67c460ae7e6c8ad5b90dc61f64ca223571b6211149514455",
}


def toolchain() -> digests.Toolchain:
    return digests.Toolchain(
        interpreter="3.12.7",
        tools={"ruff": "0.8.0", "basedpyright": "1.40.2"},
        lockfile=BLOBS["uv.lock"],
        pyproject=BLOBS["pyproject.toml"],
        image="sha256:" + "1" * 64,
        environment={"TZ": "UTC", "LANG": "C.UTF-8"},
    )


def cases() -> dict[str, tuple[Callable[[Any], str], Any]]:
    chain = toolchain()
    return {
        "static": (
            digests.static_key,
            digests.StaticInputs(
                gate="ruff",
                head={path: BLOBS[path] for path in ("scripts/app.py", "tests/test_app.py")},
                base={"scripts/app.py": BLOBS["scripts/app.py"]},
                grader="2" * 64,
                toolchain=chain,
            ),
        ),
        "test": (
            digests.test_key,
            digests.TestInputs(
                tests={"tests/test_app.py::test_answer": UNITS["answer"]},
                opened={"pyproject.toml": BLOBS["pyproject.toml"]},
                invocation=("pytest", "-p", "tests.pass_record", "tests/test_app.py"),
                toolchain=chain,
            ),
        ),
        "stats": (
            digests.stats_key,
            digests.StatsInputs(
                path="tests/test_app.py",
                inputs={"scripts/app.py": UNITS["answer"], "tests/test_app.py": UNITS[MODULE]},
                scope={"scripts/app.py": (6, 7)},
                measured=True,
                discovery="3" * 64,
                sources={"pyproject.toml": SHA256["pyproject.toml"], "uv.lock": SHA256["uv.lock"]},
                options=("-p", "no:cacheprovider"),
                lockfile="8" * 64,
            ),
        ),
        "mutant": (
            digests.mutant_key,
            digests.MutantInputs(
                module="scripts/app.py",
                function="answer",
                definition=UNITS["answer"],
                module_code=UNITS[MODULE],
                names=frozenset({"scripts.app.x_answer__mutmut_1", "scripts.app.x_answer__mutmut_2"}),
                mutmut="3.3.1",
                runner="4" * 64,
                covering={"tests/test_app.py::test_answer": UNITS["answer"]},
                toolchain=chain,
            ),
        ),
        "grade": (
            digests.grade_key,
            digests.GradeInputs(
                repository="the-cloud-clockwork/agentihooks",
                head="5" * 40,
                base="6" * 40,
                tier="pull_request",
                grader="7" * 40,
            ),
        ),
    }


def flips(value: Any) -> list[Any]:
    if is_dataclass(value) and not isinstance(value, type):
        return [
            replace(value, **{item.name: changed})
            for item in fields(value)
            for changed in flips(getattr(value, item.name))
        ]
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, int):
        return [value + 1]
    if isinstance(value, str):
        return [value + "x"]
    if isinstance(value, Mapping):
        first = next(iter(value))
        rest = {name: item for name, item in value.items() if name != first}
        return [
            rest,
            {**value, "added": value[first]},
            {first + "x": value[first], **rest},
            *({**value, first: changed} for changed in flips(value[first])),
        ]
    if isinstance(value, frozenset):
        return [value | {"added"}, value - {min(value)}]
    if isinstance(value, tuple):
        return [value[:-1], (*value, value[-1]), value[::-1], *((changed, *value[1:]) for changed in flips(value[0]))]
    raise AssertionError(f"no flip for {value!r}")


def reorder(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return replace(value, **{item.name: reorder(getattr(value, item.name)) for item in fields(value)})
    if isinstance(value, Mapping):
        return {name: reorder(item) for name, item in reversed(list(value.items()))}
    if isinstance(value, frozenset):
        return frozenset(sorted(value, reverse=True))
    return value


def test_digest_is_sha256_of_canonical_json() -> None:
    record = {"b": [1, {"d": "é", "c": None}], "a": True}
    expected = hashlib.sha256(b'{"a":true,"b":[1,{"c":null,"d":"\\u00e9"}]}').hexdigest()
    assert digests.digest(record) == expected
    assert digests.digest(dict(reversed(record.items()))) == expected


def test_blob_id_is_the_git_blob_id() -> None:
    assert digests.blob_id(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert digests.blob_id(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


@pytest.mark.parametrize("kind", KINDS)
def test_identical_inputs_in_any_order_give_one_key(kind: str) -> None:
    build, inputs = cases()[kind]
    assert reorder(inputs) == inputs
    assert build(reorder(inputs)) == build(inputs)


@pytest.mark.parametrize("kind", KINDS)
def test_flipping_any_single_input_gives_a_new_key(kind: str) -> None:
    build, inputs = cases()[kind]
    base = build(inputs)
    variants = flips(inputs)
    assert len(variants) >= len(fields(inputs))
    for variant in variants:
        assert build(variant) != base, variant


@pytest.mark.parametrize("kind", KINDS)
def test_golden_keys_for_the_fixture_tree(kind: str) -> None:
    build, inputs = cases()[kind]
    assert build(inputs) == GOLDEN[kind]


@pytest.mark.parametrize("name", ("uv.lock", "pyproject.toml"))
@pytest.mark.parametrize("kind", ("static", "test", "stats", "mutant"))
def test_a_lockfile_or_pyproject_change_rekeys_every_unit_kind(kind: str, name: str) -> None:
    build, inputs = cases()[kind]
    bumped = TREE[name] + b'\n[tool.bumped]\nvalue = "1.0"\n'
    if kind == "stats":
        changed = replace(inputs, sources={**inputs.sources, name: hashlib.sha256(bumped).hexdigest()})
    else:
        field = {"uv.lock": "lockfile", "pyproject.toml": "pyproject"}[name]
        changed = replace(inputs, toolchain=replace(inputs.toolchain, **{field: digests.blob_id(bumped)}))
    assert build(changed) != build(inputs)


def test_mutant_names_enter_the_key_sorted(monkeypatch: pytest.MonkeyPatch) -> None:
    _, inputs = cases()["mutant"]
    names = frozenset(f"scripts.app.x_answer__mutmut_{number}" for number in range(1, 40))
    records: list[dict[str, Any]] = []
    real = digests.digest
    monkeypatch.setattr(digests, "digest", lambda record: records.append(record) or real(record))
    digests.mutant_key(replace(inputs, names=names))
    assert records[0]["inputs"]["names"] == sorted(names)


@pytest.mark.parametrize("kind", KINDS)
def test_every_key_carries_its_kind_and_schema_version(kind: str) -> None:
    build, inputs = cases()[kind]
    prefix, _, value = build(inputs).rpartition("-")
    assert prefix == f"{kind}-v{digests.SCHEMAS[kind]}"
    assert re.fullmatch(r"[0-9a-f]{64}", value)


@pytest.mark.parametrize("kind", KINDS)
def test_a_schema_bump_is_a_cold_cache(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    build, inputs = cases()[kind]
    before = build(inputs).rpartition("-")[2]
    monkeypatch.setitem(digests.SCHEMAS, kind, digests.SCHEMAS[kind] + 1)
    prefix, _, after = build(inputs).rpartition("-")
    assert prefix == f"{kind}-v{digests.SCHEMAS[kind]}"
    assert after != before


def test_stats_key_is_the_stats_cache_identity(tmp_path: Path) -> None:
    _, inputs = cases()["stats"]
    values = (stats_cache.SCHEMA, dict(inputs.sources), list(inputs.options), inputs.lockfile)
    context = {**dict(zip(stats_cache.CONTEXT_FIELDS, values)), "inventory": ["scripts/app.py"]}
    stored = StoredStats(tmp_path, tmp_path, context, {"scripts/app.py": [6, 7]})
    scope = stored.scope(dict(inputs.inputs))
    identity = stored.identity(inputs.path, dict(inputs.inputs), scope, inputs.measured, inputs.discovery)
    assert digests.SCHEMAS["stats"] == stats_cache.SCHEMA
    assert digests.stats_key(inputs) == f"stats-v{stats_cache.SCHEMA}-{identity}"


def test_no_key_reads_the_day_the_run_the_branch_or_the_author() -> None:
    names = {item.name for record in RECORDS.values() for item in fields(record)}
    assert not names & {"day", "date", "time", "run", "run_id", "branch", "author"}


def test_the_specification_names_every_input_per_kind() -> None:
    section = DOC.read_text().split("\n## Digest specification\n", 1)[1].split("\n## ", 1)[0]
    listed: dict[str, set[str]] = {}
    for kind, name in re.findall(r"^\| `(\w+)` \| `(\w+)` \|", section, re.MULTILINE):
        listed.setdefault(kind, set()).add(name)
    assert listed == {kind: {item.name for item in fields(record)} for kind, record in RECORDS.items()}


def test_a_gate_with_no_commit_id_read_hashes_as_it_did_before_the_commit_id_key() -> None:
    _, inputs = cases()["static"]
    assert inputs.commit_ids == {}
    assert digests.static_key(inputs) == GOLDEN["static"]


def test_two_head_shas_over_equal_trees_key_differently_and_one_head_sha_keys_once() -> None:
    _, inputs = cases()["static"]
    first = replace(inputs, commit_ids={"github.event.pull_request.head.sha": "a" * 40})
    second = replace(inputs, commit_ids={"github.event.pull_request.head.sha": "b" * 40})
    repeated = replace(inputs, commit_ids={"github.event.pull_request.head.sha": "a" * 40})
    assert digests.static_key(first) != digests.static_key(second)
    assert digests.static_key(first) == digests.static_key(repeated)
    assert digests.static_key(inputs) != digests.static_key(first)
