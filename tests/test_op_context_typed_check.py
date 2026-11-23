"""The live check of the typed op context over stand-in modules, over the registry of the ledger core and over the
Semgrep rule that holds the same shape."""

import inspect
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.feature_checks import op_context_typed as check
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.op_context import OpContext

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
RULE = ROOT / ".semgrep" / "features" / "ledger-op-context-typed.yml"
FIXTURE = "ledger-op-context-typed.test.py"
MEMBERS = ("at", "rev", "events", "refused", "meta", "slug", "dirty", "stamps", "author", "record", "stamp")
HELD = "the concrete context holds every member of the Protocol"


def taking(name: str, annotation: Any = inspect.Parameter.empty) -> Any:
    """A new function of that name that takes the op context under the given annotation, or untyped."""

    def apply(doc: dict[str, Any], op: dict[str, Any], ctx) -> bool:
        return True

    apply.__qualname__ = name
    if annotation is not inspect.Parameter.empty:
        apply.__annotations__["ctx"] = annotation
    return apply


def module(name: str, **members: Any) -> ModuleType:
    """A module of that name that defines the given functions and classes itself."""
    made = ModuleType(name)
    for key, member in members.items():
        member.__module__ = name
        setattr(made, key, member)
    return made


def context() -> Any:
    return core.Context({"rev": 0, "stamps": {}}, 1)


@pytest.mark.feature("ledger-op-context-typed")
def test_a_module_whose_functions_take_the_context_by_the_protocol_passes() -> None:
    def other(doc: dict[str, Any]) -> bool:
        return bool(doc)

    ops = module("pkg.ledger_ops", apply=taking("apply", OpContext), _set=taking("_set", OpContext), other=other)
    assert check.takers(ops) == [("_set", OpContext), ("apply", OpContext)]
    assert check.judged(ops) == ["ledger_ops: 2 functions take the op context, each by the Protocol"]


@pytest.mark.feature("ledger-op-context-typed")
@pytest.mark.parametrize(
    ("annotation", "found"),
    [
        (inspect.Parameter.empty, "untyped"),
        (Any, "as Any"),
        (object, "as object"),
        ("Context", "as 'Context'"),
        (core.Context, "as Context"),
    ],
    ids=["untyped", "any", "object", "a string", "the concrete class"],
)
def test_a_function_that_takes_the_context_by_anything_but_the_protocol_fails(annotation: Any, found: str) -> None:
    ops = module("ledger_ops", apply=taking("apply", OpContext), _set=taking("_set", annotation))
    assert check.judged(ops) == [f"FAIL: ledger_ops._set takes the op context {found}"]


@pytest.mark.feature("ledger-op-context-typed")
def test_a_method_that_takes_the_context_is_held_like_a_function() -> None:
    class Gate:
        def apply(self, doc: dict[str, Any], op: dict[str, Any], ctx, apply: Any) -> bool:
            return True

    Gate.apply.__qualname__ = "Gate.apply"
    ops = module("ledger_ops", Gate=Gate, apply=taking("apply", OpContext))
    assert check.judged(ops) == ["FAIL: ledger_ops.Gate.apply takes the op context untyped"]


@pytest.mark.feature("ledger-op-context-typed")
def test_a_function_the_module_imports_from_another_is_left_out() -> None:
    ops = module("ledger_ops", apply=taking("apply", OpContext))
    ops.borrowed = taking("borrowed")
    assert check.judged(ops) == ["ledger_ops: 1 functions take the op context, each by the Protocol"]


@pytest.mark.feature("ledger-op-context-typed")
def test_a_module_with_no_function_that_takes_the_context_fails() -> None:
    def check_op(op: dict[str, Any]) -> None:
        return None

    ops = module("ledger_ops", check=check_op)
    assert check.judged(ops) == ["FAIL: ledger_ops holds no function that takes the op context"]


@pytest.mark.feature("ledger-op-context-typed")
def test_the_lines_come_in_module_name_order_and_end_with_the_concrete_context() -> None:
    later, sooner = module("ledger_b", apply=taking("apply", OpContext)), module("ledger_a", apply=taking("apply"))
    assert check.lines([later, sooner], context()) == [
        "FAIL: ledger_a.apply takes the op context untyped",
        "ledger_b: 1 functions take the op context, each by the Protocol",
        HELD,
    ]


@pytest.mark.feature("ledger-op-context-typed")
@pytest.mark.parametrize("dropped", MEMBERS)
def test_a_context_that_lacks_one_member_of_the_protocol_fails(dropped: str) -> None:
    whole = context()
    assert check.satisfied(whole) == HELD
    partial = SimpleNamespace(**{name: getattr(whole, name) for name in MEMBERS if name != dropped})
    assert check.satisfied(partial) == "FAIL: the concrete context lacks a member of the Protocol"


@pytest.mark.feature("ledger-op-context-typed")
def test_the_check_prints_every_line_and_fails_on_one_fault(capsys: pytest.CaptureFixture[str]) -> None:
    ops = [module("ledger_a", apply=taking("apply", OpContext)), module("ledger_b", apply=taking("apply", Any))]
    assert check.main(ops, context()) == 1
    assert capsys.readouterr().out.splitlines() == [
        "ledger_a: 1 functions take the op context, each by the Protocol",
        "FAIL: ledger_b.apply takes the op context as Any",
        HELD,
    ]


@pytest.mark.feature("ledger-op-context-typed")
def test_the_check_passes_when_no_line_fails(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main([module("ledger_a", apply=taking("apply", OpContext))], context()) == 0
    assert capsys.readouterr().out.splitlines() == [
        "ledger_a: 1 functions take the op context, each by the Protocol",
        HELD,
    ]


@pytest.mark.feature("ledger-op-context-typed")
def test_every_registered_op_module_takes_the_context_by_the_protocol(capsys: pytest.CaptureFixture[str]) -> None:
    passed = check.main()
    printed = capsys.readouterr().out.splitlines()
    assert [line for line in printed if line.startswith("FAIL: ")] == []
    assert passed == 0
    assert len(printed) == len(set(core.EXTENSION_OPS.values())) + 1
    assert printed[-1] == HELD


@pytest.mark.feature("ledger-op-context-typed")
def test_the_ledger_package_folder_joins_the_import_path_once(monkeypatch: pytest.MonkeyPatch) -> None:
    folder = str(Path(core.__file__).resolve().parent)
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != folder])
    check.importable()
    check.importable()
    assert sys.path.count(folder) == 1
    assert sys.path[0] == folder


@pytest.mark.feature("ledger-op-context-typed")
def test_the_rule_grades_exactly_the_registered_op_modules() -> None:
    rule: dict[str, Any] = yaml.safe_load(RULE.read_text())["rules"][0]
    registered = {f"/scripts/swarm_ledger/{Path(found.__file__).name}" for found in core.EXTENSION_OPS.values()}
    assert sorted(rule["paths"]["include"]) == sorted({*registered, FIXTURE})
