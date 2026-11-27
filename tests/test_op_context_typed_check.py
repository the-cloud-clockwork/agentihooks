"""The live check of the typed op context over stand-in modules, over the registry and the handed-off families of
the ledger core, and over the Semgrep rule that holds the same shape."""

import ast
import fnmatch
import importlib
import inspect
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.ci_mutation.identity import ShortNameAlias
from scripts.feature_checks import op_context_typed as check

check.importable()

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger.op_context import OpContext  # noqa: E402

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
RULE = ROOT / ".semgrep" / "features" / "ledger-op-context-typed.yml"
FIXTURE = "ledger-op-context-typed.test.py"
GLOB = "/scripts/swarm_ledger/ledger_*.py"
MEMBERS = (
    "at",
    "rev",
    "events",
    "refused",
    "dropped",
    "unstored",
    "meta",
    "slug",
    "dirty",
    "stamps",
    "author",
    "record",
    "stamp",
)
HANDED_FAMILIES = ("ledger_agent_ops", "ledger_artifacts", "ledger_comments", "ledger_stats")
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
    setattr(ops, "borrowed", taking("borrowed"))
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
def test_takers_keeps_two_functions_of_one_name_in_the_order_the_module_holds_them() -> None:
    ops = module("ledger_ops", first=taking("apply", OpContext), second=taking("apply", Any))
    assert check.takers(ops) == [("apply", OpContext), ("apply", Any)]


@pytest.mark.feature("ledger-op-context-typed")
def test_main_builds_the_concrete_context_of_a_blank_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Any] = []

    def recording_lines(modules: object, context: object) -> list[str]:
        captured.append(context)
        return [HELD]

    monkeypatch.setattr(check, "lines", recording_lines)
    assert check.main() == 0
    built = captured[0]
    assert isinstance(built, core.Context)
    assert built.rev == 1
    assert built.at == 0
    assert built.stamps == {}


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
    assert len(printed) == len(check.op_modules()) + 1
    assert printed[-1] == HELD


@pytest.mark.feature("ledger-op-context-typed")
def test_op_modules_discovers_the_registry_and_every_handed_family() -> None:
    discovered = {found.__name__.rpartition(".")[2] for found in check.op_modules()}
    registered = {found.__name__.rpartition(".")[2] for found in core.EXTENSION_OPS.values()}
    assert discovered == registered | {"ledger_core", *HANDED_FAMILIES}


@pytest.mark.feature("ledger-op-context-typed")
def test_op_modules_discovers_every_handed_family_behind_the_short_name_alias_of_a_mutation_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in HANDED_FAMILIES:
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, name)
    aliases = [ShortNameAlias(ROOT, f"scripts/swarm_ledger/{name}.py") for name in HANDED_FAMILIES]
    monkeypatch.setattr(sys, "meta_path", [*aliases, *sys.meta_path])
    discovered = {found.__name__.rpartition(".")[2] for found in check.op_modules()}
    registered = {found.__name__.rpartition(".")[2] for found in core.EXTENSION_OPS.values()}
    assert discovered == registered | {"ledger_core", *HANDED_FAMILIES}


@pytest.mark.feature("ledger-op-context-typed")
def test_the_member_set_equals_what_the_functions_use() -> None:
    assert set(MEMBERS) == {*OpContext.__annotations__, "author", "stamp"}


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_discovers_a_module_bound_by_a_bare_import_and_called_by_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plant_bare_mod.py").write_text("def apply(doc, op, ctx):\n    return True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "import plant_bare_mod\n\n\ndef apply_op(doc, op, ctx):\n    return plant_bare_mod.apply(doc, op, ctx)\n"
    )
    discovered = check.handed(tree, tmp_path)
    assert {found.__name__ for found in discovered} == {"plant_bare_mod"}


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_discovers_a_module_imported_as_a_submodule_and_called_by_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "plant_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "plant_sub.py").write_text("def apply(doc, op, ctx):\n    return True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "from plant_pkg import plant_sub\n\n\ndef apply_op(doc, op, ctx):\n    return plant_sub.apply(doc, op, ctx)\n"
    )
    discovered = check.handed(tree, package)
    assert {found.__name__ for found in discovered} == {"plant_pkg.plant_sub"}


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_discovers_a_function_called_bare_from_a_plain_from_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plant_bare_fn.py").write_text("def run(doc, op, ctx):\n    return True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "from plant_bare_fn import run\n\n\ndef apply_op(doc, op, ctx):\n    return run(doc, op, ctx=ctx)\n"
    )
    discovered = check.handed(tree, tmp_path)
    assert {found.__name__ for found in discovered} == {"plant_bare_fn"}


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_discovers_modules_through_aliased_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "plant_aliased_a.py").write_text("def apply(doc, op, ctx):\n    return True\n")
    (tmp_path / "plant_aliased_b.py").write_text("def run(doc, op, ctx):\n    return True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "import plant_aliased_a as a_mod\n"
        "from plant_aliased_b import run as b_run\n\n\n"
        "def apply_op(doc, op, ctx):\n"
        "    a_mod.apply(doc, op, ctx)\n"
        "    return b_run(doc, op, ctx)\n"
    )
    discovered = check.handed(tree, tmp_path)
    assert {found.__name__ for found in discovered} == {"plant_aliased_a", "plant_aliased_b"}


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_ignores_a_call_to_a_module_outside_the_folder(tmp_path: Path) -> None:
    tree = ast.parse("import copy\n\n\ndef apply_op(doc, op, ctx):\n    return copy.copy(ctx)\n")
    assert check.handed(tree, tmp_path) == set()


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_ignores_a_call_that_does_not_pass_the_context_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plant_unused.py").write_text("def check(op):\n    return True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse("import plant_unused\n\n\ndef check_op(op):\n    return plant_unused.check(op)\n")
    assert check.handed(tree, tmp_path) == set()


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_skips_a_call_bound_to_a_module_that_cannot_be_resolved(tmp_path: Path) -> None:
    tree = ast.parse(
        "import plant_does_not_exist_anywhere_at_all_123\n\n\n"
        "def apply_op(doc, op, ctx):\n"
        "    return plant_does_not_exist_anywhere_at_all_123.apply(doc, op, ctx)\n"
    )
    assert check.handed(tree, tmp_path) == set()


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_skips_a_name_imported_from_a_package_that_does_not_exist(tmp_path: Path) -> None:
    tree = ast.parse(
        "from plant_missing_package_123 import plant_mod\n\n\n"
        "def apply_op(doc, op, ctx):\n"
        "    return plant_mod.apply(doc, op, ctx)\n"
    )
    assert check.handed(tree, tmp_path) == set()


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_skips_a_name_imported_from_a_plain_module_even_when_a_module_of_that_name_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plant_plain_owner.py").write_text(
        "class plant_plain_tail:\n    @staticmethod\n    def apply(doc, op, ctx):\n        return True\n"
    )
    (tmp_path / "plant_plain_tail.py").write_text("raise RuntimeError('must not be imported')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "from plant_plain_owner import plant_plain_tail\n\n\n"
        "def apply_op(doc, op, ctx):\n"
        "    return plant_plain_tail.apply(doc, op, ctx)\n"
    )
    assert check.handed(tree, tmp_path) == set()


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_ignores_a_module_whose_spec_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "plant_no_spec", module("plant_no_spec"))
    tree = ast.parse(
        "import plant_no_spec\n\n\ndef apply_op(doc, op, ctx):\n    return plant_no_spec.apply(doc, op, ctx)\n"
    )
    assert check.handed(tree, tmp_path) == set()


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_discovers_a_module_in_a_subfolder_of_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = tmp_path / "plant_sub_folder"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "plant_deep.py").write_text("def apply(doc, op, ctx):\n    return True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "from plant_sub_folder import plant_deep\n\n\n"
        "def apply_op(doc, op, ctx):\n"
        "    return plant_deep.apply(doc, op, ctx)\n"
    )
    discovered = check.handed(tree, tmp_path)
    assert {found.__name__ for found in discovered} == {"plant_sub_folder.plant_deep"}


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_discovers_a_module_two_packages_deep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outer = tmp_path / "plant_outer"
    inner = outer / "plant_inner"
    inner.mkdir(parents=True)
    (outer / "__init__.py").write_text("")
    (inner / "__init__.py").write_text("")
    (inner / "plant_deepest.py").write_text("def apply(doc, op, ctx):\n    return True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "from plant_outer.plant_inner import plant_deepest\n\n\n"
        "def apply_op(doc, op, ctx):\n"
        "    return plant_deepest.apply(doc, op, ctx)\n"
    )
    discovered = check.handed(tree, tmp_path)
    assert {found.__name__ for found in discovered} == {"plant_outer.plant_inner.plant_deepest"}


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_lets_an_in_tree_import_error_propagate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "plant_raises.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "import plant_raises\n\n\ndef apply_op(doc, op, ctx):\n    return plant_raises.apply(doc, op, ctx)\n"
    )
    with pytest.raises(RuntimeError, match="boom"):
        check.handed(tree, tmp_path)


@pytest.mark.feature("ledger-op-context-typed")
def test_handed_never_imports_a_module_outside_the_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "plant_inside"
    folder.mkdir()
    (tmp_path / "plant_outside.py").write_text("raise RuntimeError('must not be imported')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    tree = ast.parse(
        "import plant_outside\n\n\ndef apply_op(doc, op, ctx):\n    return plant_outside.apply(doc, op, ctx)\n"
    )
    assert check.handed(tree, folder) == set()


@pytest.mark.feature("ledger-op-context-typed")
def test_registry_modules_keeps_the_module_values_of_a_mixed_dict() -> None:
    held = module("plant_registry_held")
    owner = module("plant_registry_owner")
    setattr(owner, "MIXED", {"op": held, "other": "not a module"})
    setattr(owner, "NAMES", {"op": "not a module"})
    setattr(owner, "EMPTY", {})
    assert check._registry_modules(owner) == {held}


@pytest.mark.feature("ledger-op-context-typed")
def test_a_module_the_core_hands_the_context_to_is_discovered_and_graded_untyped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plant_handed.py").write_text("def apply(doc, op, ctx) -> bool:\n    return True\n")
    (tmp_path / "plant_core_handed.py").write_text(
        "from scripts.swarm_ledger.op_context import OpContext\n"
        "import plant_handed\n\n\n"
        "def apply_op(doc, op, ctx: OpContext) -> bool:\n"
        "    return plant_handed.apply(doc, op, ctx)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    plant_core = importlib.import_module("plant_core_handed")
    modules = check.op_modules(plant_core)
    assert {found.__name__ for found in modules} == {"plant_core_handed", "plant_handed"}
    assert check.lines(modules, context()) == [
        "plant_core_handed: 1 functions take the op context, each by the Protocol",
        "FAIL: plant_handed.apply takes the op context untyped",
        HELD,
    ]


@pytest.mark.feature("ledger-op-context-typed")
def test_a_core_function_with_an_untyped_context_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "plant_core_untyped.py").write_text("def apply_op(doc, op, ctx) -> bool:\n    return True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    plant_core = importlib.import_module("plant_core_untyped")
    modules = check.op_modules(plant_core)
    assert [found.__name__ for found in modules] == ["plant_core_untyped"]
    assert check.lines(modules, context()) == ["FAIL: plant_core_untyped.apply_op takes the op context untyped", HELD]


@pytest.mark.feature("ledger-op-context-typed")
def test_the_ledger_package_folder_joins_the_import_path_once(monkeypatch: pytest.MonkeyPatch) -> None:
    folder = str(Path(str(core.__file__)).resolve().parent)
    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != folder])
    check.importable()
    check.importable()
    assert sys.path.count(folder) == 1
    assert sys.path[0] == folder


@pytest.mark.feature("ledger-op-context-typed")
def test_the_rule_grades_every_ledger_module_by_one_glob_with_no_hand_kept_list() -> None:
    rule: dict[str, Any] = yaml.safe_load(RULE.read_text())["rules"][0]
    assert sorted(rule["paths"]["include"]) == sorted({GLOB, FIXTURE})


@pytest.mark.feature("ledger-op-context-typed")
def test_the_rule_glob_covers_every_module_the_live_check_discovers() -> None:
    rule: dict[str, Any] = yaml.safe_load(RULE.read_text())["rules"][0]
    globs = rule["paths"]["include"]
    repo_root = ROOT.resolve()
    for found in check.op_modules():
        repo_path = "/" + str(Path(str(found.__file__)).resolve().relative_to(repo_root))
        assert any(fnmatch.fnmatch(repo_path, glob) for glob in globs)


@pytest.mark.feature("ledger-op-context-typed")
def test_rebound_names_a_function_that_renames_the_op_context() -> None:
    tree = ast.parse("def apply_op(doc, op, ctx):\n    c = ctx\n    return c\n")
    assert check.rebound(tree, "plant_core") == ["FAIL: plant_core.apply_op renames the op context to c"]


@pytest.mark.feature("ledger-op-context-typed")
def test_rebound_is_empty_when_no_function_renames_the_op_context() -> None:
    tree = ast.parse("def apply_op(doc, op, ctx):\n    return ctx.rev\n")
    assert check.rebound(tree, "plant_core") == []


@pytest.mark.feature("ledger-op-context-typed")
def test_rebound_names_an_annotated_rename_of_the_op_context() -> None:
    tree = ast.parse("def apply_op(doc, op, ctx):\n    c: object = ctx\n    return c\n")
    assert check.rebound(tree, "plant_core") == ["FAIL: plant_core.apply_op renames the op context to c"]


@pytest.mark.feature("ledger-op-context-typed")
def test_rebound_names_a_walrus_rename_of_the_op_context() -> None:
    tree = ast.parse("def apply_op(doc, op, ctx):\n    return (c := ctx).rev\n")
    assert check.rebound(tree, "plant_core") == ["FAIL: plant_core.apply_op renames the op context to c"]


@pytest.mark.feature("ledger-op-context-typed")
def test_the_real_core_never_renames_its_op_context() -> None:
    tree = ast.parse(Path(str(core.__file__)).read_text())
    assert check.rebound(tree, "ledger_core") == []
