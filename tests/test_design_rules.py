import subprocess
from pathlib import Path

import pytest

from scripts import design_rules

pytestmark = pytest.mark.unit

TYPED = "def load(path: str, count: int = 1) -> str:\n    return path * count\n"
UNTYPED = "def load(path, count: int = 1):\n    return path * count\n"
CONCRETE = "class Store:\n    def put(self, key: str) -> None:\n        self.key = key\n"
KEEP = {"scripts/keep.py": "X = 1\n"}


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-f", *files], check=True)
    return root


def _grade(tmp_path: Path, base: dict[str, str], head: dict[str, str]) -> tuple[int, list[str]]:
    return design_rules.grade(_tree(tmp_path / "base", base), _tree(tmp_path / "head", head))


def _errors(tmp_path: Path, head: dict[str, str], base: dict[str, str] | None = None) -> list[str]:
    return _grade(tmp_path, base or KEEP, head)[1]


def _inherits(key: str, target: str) -> str:
    return (
        f"inheritance: {key} inherits the concrete project class {target}; "
        "inherit an abstract base or Protocol and compose the shared behaviour"
    )


def test_a_new_public_function_without_hints_is_red_naming_each_missing_hint(tmp_path):
    assert _errors(tmp_path, {"scripts/job.py": UNTYPED}) == [
        "untyped: public scripts/job.py::load lacks type hints on path, return"
    ]


def test_a_fully_typed_public_function_is_green(tmp_path):
    assert _grade(tmp_path, KEEP, {"scripts/job.py": TYPED}) == (1, [])


def test_an_unchanged_untyped_function_is_not_graded(tmp_path):
    assert _grade(tmp_path, {"scripts/job.py": UNTYPED}, {"scripts/job.py": UNTYPED + "\nY = 2\n"}) == (0, [])


def test_a_body_change_keeps_an_untyped_signature_ungraded(tmp_path):
    changed = UNTYPED.replace("path * count", "path * count * 2")
    assert _grade(tmp_path, {"scripts/job.py": UNTYPED}, {"scripts/job.py": changed}) == (0, [])


def test_a_changed_signature_is_graded(tmp_path):
    changed = UNTYPED.replace("count: int = 1", "count: int = 2")
    assert _grade(tmp_path, {"scripts/job.py": UNTYPED}, {"scripts/job.py": changed}) == (
        1,
        ["untyped: public scripts/job.py::load lacks type hints on path, return"],
    )


def test_a_redefined_function_is_graded_under_its_own_numbered_key(tmp_path):
    head = {"scripts/job.py": TYPED + "\n\n" + UNTYPED}
    assert _grade(tmp_path, {"scripts/job.py": TYPED}, head) == (
        1,
        ["untyped: public scripts/job.py::load#2 lacks type hints on path, return"],
    )


def test_private_and_nested_functions_are_not_graded_for_hints(tmp_path):
    source = (
        "def _load(path):\n    def inner(x):\n        return x\n    return inner(path)\n\n\n"
        "def load(path: str) -> str:\n    def inner(x):\n        return x\n    return inner(path)\n"
    )
    assert _grade(tmp_path, KEEP, {"scripts/job.py": source}) == (4, [])


def test_functions_under_a_statement_block_keep_their_scope(tmp_path):
    source = (
        "import sys\n\n"
        "if sys:\n    def load(path):\n        return path\n\n\n"
        "class Job(object):\n"
        "    if sys:\n        def run(self, n: int) -> int:\n            return self.n\n\n"
        "        def stop(self, n):\n            return n\n"
    )
    assert _errors(tmp_path, {"scripts/job.py": source}) == [
        "untyped: public scripts/job.py::load lacks type hints on path, return",
        "untyped: public scripts/job.py::Job.stop lacks type hints on n, return",
    ]


def test_methods_skip_the_receiver_but_grade_every_other_parameter(tmp_path):
    source = (
        "class Job:\n"
        "    def run(self, n: int) -> int:\n        return n\n"
        "    @classmethod\n    def make(cls) -> 'Job':\n        return cls()\n"
        "    @staticmethod\n    def parse(text) -> int:\n        return 1\n"
        "    def stop(self, n, m: int) -> None:\n        pass\n"
        "    def __init__(self, n):\n        self.n = n\n"
    )
    assert _errors(tmp_path, {"scripts/job.py": source}) == [
        "untyped: public scripts/job.py::Job.parse lacks type hints on text",
        "untyped: public scripts/job.py::Job.stop lacks type hints on n",
    ]


def test_methods_of_a_private_class_are_not_public(tmp_path):
    source = "class _Job:\n    def run(self, n):\n        return self.n\n"
    assert _errors(tmp_path, {"scripts/job.py": source}) == []


def test_star_args_and_keyword_only_parameters_need_hints(tmp_path):
    source = "def run(*items, flag) -> None:\n    pass\n"
    assert _errors(tmp_path, {"scripts/job.py": source}) == [
        "untyped: public scripts/job.py::run lacks type hints on items, flag"
    ]


def test_a_keyword_bag_is_red_on_the_config_rule(tmp_path):
    source = "def run(name: str, **options: object) -> None:\n    pass\n"
    assert _errors(tmp_path, {"scripts/job.py": source}) == [
        "config: public scripts/job.py::run takes **options as a plain dict; take a typed configuration object"
    ]


def test_every_plain_configuration_of_one_function_is_named(tmp_path):
    source = "def run(config: dict, **options: object) -> None:\n    pass\n"
    assert _errors(tmp_path, {"scripts/job.py": source}) == [
        "config: public scripts/job.py::run takes config, **options as a plain dict; take a typed configuration object"
    ]


@pytest.mark.parametrize(
    "annotation",
    [
        "dict",
        "dict[str, int]",
        "typing.Dict[str, int]",
        "Mapping[str, int]",
        "'dict'",
        "'dict[str, int]'",
        "dict | None",
        "None | dict",
        "typing.Optional[dict]",
        "Union[int, dict]",
    ],
)
@pytest.mark.parametrize("name", ["config", "settings", "job_options", "cfg", "params", "opts", "conf", "config_map"])
def test_a_configuration_parameter_typed_as_a_plain_dict_is_red(tmp_path, annotation, name):
    source = f"import typing\n\ndef run({name}: {annotation}) -> None:\n    pass\n"
    assert _errors(tmp_path, {"scripts/job.py": source}) == [
        f"config: public scripts/job.py::run takes {name} as a plain dict; take a typed configuration object"
    ]


@pytest.mark.parametrize(
    "parameter",
    [
        "config: Settings",
        "rows: dict[str, int]",
        "configured: dict",
        "reconfig: dict",
        "config: 'Settings'",
        "config: Settings | None",
        "config: list[dict]",
        "config: 'dict ->'",
    ],
)
def test_a_typed_configuration_or_a_plain_data_dict_is_green(tmp_path, parameter):
    source = f"class Settings:\n    pass\n\ndef run({parameter}) -> None:\n    pass\n"
    assert _errors(tmp_path, {"scripts/job.py": source}) == []


def test_a_private_function_may_take_a_keyword_bag(tmp_path):
    assert _errors(tmp_path, {"scripts/job.py": "def _run(**options) -> None:\n    pass\n"}) == []


def test_a_class_inheriting_a_concrete_project_class_in_its_module_is_red(tmp_path):
    source = CONCRETE + "\n\nclass Cache(Store):\n    pass\n\n\nclass Shelf(Store[int]):\n    pass\n"
    assert _errors(tmp_path, {"scripts/store.py": source}) == [
        _inherits("scripts/store.py::Cache", "scripts.store.Store"),
        _inherits("scripts/store.py::Shelf", "scripts.store.Store"),
    ]


@pytest.mark.parametrize(
    "imports, base",
    [
        ("from scripts.store import Store", "Store"),
        ("from scripts.store import Store as Base", "Base"),
        ("from .store import Store", "Store"),
        ("from scripts import store", "store.Store"),
        ("from . import store", "store.Store"),
        ("import scripts.store as store", "store.Store"),
        ("from scripts.reexport import Store", "Store"),
    ],
)
def test_a_concrete_project_class_imported_from_another_module_is_red(tmp_path, imports, base):
    head = {
        "scripts/__init__.py": "",
        "scripts/store.py": CONCRETE,
        "scripts/reexport.py": "from scripts.store import Store\n",
        "scripts/cache.py": f"{imports}\n\n\nclass Cache({base}):\n    pass\n",
    }
    base_tree = {name: text for name, text in head.items() if name != "scripts/cache.py"}
    assert _errors(tmp_path, head, base_tree) == [_inherits("scripts/cache.py::Cache", "scripts.store.Store")]


def test_a_package_reexport_and_a_parent_relative_import_resolve(tmp_path):
    head = {
        "scripts/__init__.py": CONCRETE,
        "scripts/pkg/__init__.py": "from .store import Store\n",
        "scripts/pkg/store.py": CONCRETE,
        "scripts/pkg/child.py": "from ..pkg.store import Store\n\nclass Child(Store):\n    pass\n",
        "scripts/cache.py": (
            "import scripts.store\nfrom scripts.pkg import Store\n\n"
            "class Cache(Store):\n    pass\n\nclass Root(scripts.Store):\n    pass\n"
        ),
    }
    base_tree = {name: text for name, text in head.items() if name not in ("scripts/cache.py", "scripts/pkg/child.py")}
    assert _errors(tmp_path, head, base_tree) == [
        _inherits("scripts/cache.py::Cache", "scripts.pkg.store.Store"),
        _inherits("scripts/cache.py::Root", "scripts.Store"),
        _inherits("scripts/pkg/child.py::Child", "scripts.pkg.store.Store"),
    ]


def test_a_dotted_attribute_base_resolves_through_its_imported_root(tmp_path):
    head = {
        "scripts/store.py": CONCRETE,
        "scripts/cache.py": (
            "import scripts\n\nclass Cache(scripts.store.Store):\n    pass\n\n"
            "class Made(make().Store):\n    pass\n\nclass Loose(unknown.Store):\n    pass\n"
        ),
    }
    assert _errors(tmp_path, head, {"scripts/store.py": CONCRETE}) == [
        _inherits("scripts/cache.py::Cache", "scripts.store.Store")
    ]


def test_a_concrete_project_class_named_like_a_root_is_red(tmp_path):
    parent = "class Enum:\n    pass\n"
    head = {
        "scripts/base.py": parent,
        "scripts/child.py": "from scripts.base import Enum\n\nclass Child(Enum):\n    pass\n",
    }
    assert _errors(tmp_path, head, {"scripts/base.py": parent}) == [
        _inherits("scripts/child.py::Child", "scripts.base.Enum")
    ]


def test_turning_a_function_async_grades_it(tmp_path):
    assert _errors(tmp_path, {"scripts/job.py": "async " + UNTYPED}, {"scripts/job.py": UNTYPED}) == [
        "untyped: public scripts/job.py::load lacks type hints on path, return"
    ]


@pytest.mark.parametrize("hops, errors", [(5, 1), (6, 0)])
def test_reexport_chains_resolve_up_to_five_hops(tmp_path, hops, errors):
    head = {f"scripts/r{n}.py": f"from scripts.r{n + 1} import Store\n" for n in range(1, hops)}
    head[f"scripts/r{hops}.py"] = CONCRETE
    head["scripts/cache.py"] = "from scripts.r1 import Store\n\nclass Cache(Store):\n    pass\n"
    base_tree = {name: text for name, text in head.items() if name != "scripts/cache.py"}
    assert len(_errors(tmp_path, head, base_tree)) == errors


def test_a_reexport_cycle_terminates_unresolved(tmp_path):
    head = {
        "scripts/a.py": "from scripts.b import Store\n",
        "scripts/b.py": "from scripts.a import Store\n",
        "scripts/cache.py": "from scripts.a import Store\n\nclass Cache(Store):\n    pass\n",
    }
    assert _errors(tmp_path, head, {"scripts/a.py": head["scripts/a.py"], "scripts/b.py": head["scripts/b.py"]}) == []


@pytest.mark.parametrize(
    "parent",
    [
        "from abc import ABC\n\nclass Base(ABC):\n    pass\n",
        "import abc\n\nclass Base(abc.ABC):\n    pass\n",
        "from abc import ABCMeta\n\nclass Base(metaclass=ABCMeta):\n    pass\n",
        "from abc import abstractmethod\n\nclass Base:\n    @abstractmethod\n    def run(self) -> None: ...\n",
        "from typing import Protocol\n\nclass Base(Protocol):\n    def run(self) -> None: ...\n",
        "from typing import Generic, TypeVar\nT = TypeVar('T')\n\nclass Base(Generic[T]):\n    pass\n",
        "class Base(ValueError):\n    pass\n",
        "class Root(Exception):\n    pass\n\nclass Base(Root):\n    pass\n",
        "from enum import Enum\n\nclass Base(Enum):\n    pass\n",
        "from typing import NamedTuple\n\nclass Base(NamedTuple):\n    x: int\n",
        "from dataclasses import dataclass\n\n@dataclass\nclass Base:\n    x: int\n",
        "import dataclasses\n\n@dataclasses.dataclass(frozen=True)\nclass Base:\n    x: int\n",
    ],
)
def test_a_class_on_an_abstract_protocol_exception_enum_tuple_or_dataclass_base_is_green(tmp_path, parent):
    head = {
        "scripts/base.py": parent,
        "scripts/child.py": "from scripts.base import Base\n\nclass Child(Base):\n    pass\n",
    }
    assert _errors(tmp_path, head, {"scripts/base.py": parent}) == []


@pytest.mark.parametrize(
    "parent",
    [
        "class Base(metaclass=type):\n    pass\n",
        "import argparse\n\nclass Base(argparse.Action):\n    pass\n",
        "class Meta(type):\n    pass\n\nclass Base(object, metaclass=Meta):\n    def run(self) -> None:\n        pass\n",
        "import functools\n\n@functools.total_ordering\nclass Base:\n    pass\n",
    ],
)
def test_a_concrete_base_with_another_metaclass_or_decorator_is_red(tmp_path, parent):
    head = {
        "scripts/base.py": parent,
        "scripts/child.py": "from scripts.base import Base\n\nclass Child(Base):\n    pass\n",
    }
    assert _errors(tmp_path, head, {"scripts/base.py": parent}) == [
        _inherits("scripts/child.py::Child", "scripts.base.Base")
    ]


def test_a_class_on_a_third_party_or_builtin_base_is_green(tmp_path):
    source = "import argparse\n\nclass Action(argparse.Action):\n    pass\n\nclass Rows(dict):\n    pass\n"
    assert _errors(tmp_path, {"scripts/job.py": source}) == []


def test_an_inheritance_cycle_is_red_and_terminates(tmp_path):
    source = "class A(B):\n    pass\n\nclass B(A):\n    pass\n"
    assert _errors(tmp_path, {"scripts/job.py": source}) == [
        _inherits("scripts/job.py::A", "scripts.job.B"),
        _inherits("scripts/job.py::B", "scripts.job.A"),
    ]


def test_an_existing_subclass_whose_body_changes_is_not_graded(tmp_path):
    before = CONCRETE + "\n\nclass Cache(Store):\n    pass\n"
    after = CONCRETE + "\n\nclass Cache(Store):\n    LIMIT = 3\n"
    assert _grade(tmp_path, {"scripts/store.py": before}, {"scripts/store.py": after}) == (0, [])


def test_tests_and_semgrep_fixtures_are_not_graded(tmp_path):
    head = {"tests/test_job.py": UNTYPED, ".semgrep/rule.test.py": UNTYPED, **KEEP}
    assert _grade(tmp_path, KEEP, head) == (0, [])


def test_a_tracked_python_script_without_the_suffix_is_skipped(tmp_path):
    head = {"scripts/tool": "#!/usr/bin/env python\ndef run(x):\n    return x\n", **KEEP}
    assert _grade(tmp_path, KEEP, head) == (0, [])


def _groups(key: str) -> str:
    return f"functions: {key} only groups functions and holds no instance state; make them module functions"


def _seam(keys: str, methods: str) -> str:
    return (
        f"protocol: {keys} share the call contract {methods} with no common Protocol or abstract base; "
        "type the seam with one"
    )


GROUPED = (
    "class Tools(object):\n"
    "    @staticmethod\n    def parse(text: str) -> int:\n        return len(text)\n"
    "    @classmethod\n    def make(cls) -> str:\n        return cls.__name__\n"
    "    def run(self, n: int) -> int:\n        return n\n"
    "    def bare() -> int:\n        return 3\n"
)


def test_a_class_that_only_groups_functions_is_red_naming_it(tmp_path):
    assert _errors(tmp_path, {"scripts/tools.py": GROUPED}) == [_groups("scripts/tools.py::Tools")]


@pytest.mark.parametrize(
    "method",
    [
        "    def keep(self, n: int) -> None:\n        self.n = n\n",
        "    def read(self) -> int:\n        return len([self])\n",
        "    def read(self) -> int:\n        return (lambda: self.n)()\n",
        "    def read(self, /) -> int:\n        return self.n\n",
        "    async def read(self) -> int:\n        return self.n\n",
    ],
)
def test_a_method_that_uses_its_receiver_keeps_the_class_green(tmp_path, method):
    assert _errors(tmp_path, {"scripts/tools.py": GROUPED + method}) == []


@pytest.mark.parametrize(
    "source",
    [
        "class Limits:\n    MAX = 3\n",
        "from abc import ABC\n\nclass Tools(ABC):\n    def run(self) -> int:\n        return 1\n",
        "class Tools(object, dict):\n    def run(self) -> int:\n        return 1\n",
        "class Tools(Exception):\n    def run(self) -> int:\n        return 1\n",
        "class Meta(type):\n    pass\n\nclass Tools(metaclass=Meta):\n    def run(self) -> int:\n        return 1\n",
        "from dataclasses import dataclass\n\n@dataclass\nclass Tools:\n    def run(self) -> int:\n        return 1\n",
    ],
)
def test_a_class_with_no_methods_a_base_or_a_declared_kind_is_not_a_function_group(tmp_path, source):
    assert _errors(tmp_path, {"scripts/tools.py": source}) == []


def test_an_existing_function_group_whose_body_changes_is_not_graded(tmp_path):
    head = {"scripts/tools.py": GROUPED + "    LIMIT = 3\n"}
    assert _grade(tmp_path, {"scripts/tools.py": GROUPED}, head) == (0, [])


def test_an_existing_class_that_gains_a_method_is_graded(tmp_path):
    head = {"scripts/tools.py": GROUPED + "    def stop(self) -> None:\n        pass\n"}
    assert _grade(tmp_path, {"scripts/tools.py": GROUPED}, head) == (1, [_groups("scripts/tools.py::Tools")])


def _impl(name: str, methods: str, bases: str = "") -> str:
    return f"class {name}{bases}:\n    def __init__(self) -> None:\n        self.n = 1\n{methods}\n\n"


FETCH_CALL = "    def fetch(self, key: str, /, *rest: str, limit: int = 1) -> str:"
FETCH = (
    f"{FETCH_CALL}\n        return key\n"
    "    @staticmethod\n    def parse(text: str) -> str:\n        return text\n"
    "    def _own(self) -> None:\n        pass\n"
)
PRIVATE = FETCH.replace("def fetch", "def _fetch").replace("def parse", "def _parse")


def test_concrete_classes_sharing_a_call_contract_without_a_seam_are_red(tmp_path):
    head = {
        "scripts/a.py": _impl("Http", FETCH, "(object)"),
        "scripts/b.py": _impl("Disk", FETCH.replace("_own", "_other"), "(object)"),
    }
    assert _errors(tmp_path, head) == [_seam("scripts/a.py::Http, scripts/b.py::Disk", "fetch/2, parse/1")]


def test_a_base_on_only_one_side_leaves_the_seam_untyped(tmp_path):
    head = {
        "scripts/a.py": f"import argparse\n\n{_impl('Http', FETCH, '(argparse.Action)')}",
        "scripts/b.py": _impl("Disk", FETCH),
    }
    assert _errors(tmp_path, head) == [_seam("scripts/a.py::Http, scripts/b.py::Disk", "fetch/2, parse/1")]


@pytest.mark.parametrize(
    "first, second",
    [
        (FETCH, FETCH.replace("limit: int = 1", "limit: int = 1, extra: int = 2")),
        (FETCH, FETCH.replace("def fetch", "def get")),
        (PRIVATE, PRIVATE),
    ],
)
def test_a_different_contract_or_no_public_methods_is_green(tmp_path, first, second):
    assert _errors(tmp_path, {"scripts/a.py": _impl("Http", first), "scripts/b.py": _impl("Disk", second)}) == []


@pytest.mark.parametrize(
    "seam, first, second",
    [
        ("from abc import ABC\n\nclass Source(ABC):\n    pass\n", "Source", "Source"),
        (
            "from abc import ABC\n\nclass Root(ABC):\n    pass\n\nclass One(Root):\n    pass\n\n"
            "class Two(Root):\n    pass\n",
            "One",
            "Two",
        ),
    ],
)
def test_a_common_abstract_base_types_the_seam(tmp_path, seam, first, second):
    head = {
        "scripts/seam.py": seam,
        "scripts/a.py": f"from scripts.seam import {first}\n\n{_impl('Http', FETCH, f'({first})')}",
        "scripts/b.py": f"from scripts.seam import {second}\n\n{_impl('Disk', FETCH, f'({second})')}",
    }
    assert _errors(tmp_path, head, {"scripts/seam.py": seam}) == []


def test_a_common_external_base_types_the_seam(tmp_path):
    head = {
        "scripts/a.py": f"import argparse\n\n{_impl('Http', FETCH, '(argparse.Action)')}",
        "scripts/b.py": f"import argparse\n\n{_impl('Disk', FETCH, '(argparse.Action)')}",
    }
    assert _errors(tmp_path, head) == []


@pytest.mark.parametrize(
    "protocol, errors",
    [
        (f"{FETCH_CALL} ...\n    @staticmethod\n    def parse(text: str) -> str: ...\n", 0),
        (f"{FETCH_CALL} ...\n", 0),
        (f"{FETCH_CALL} ...\n    def close(self) -> None: ...\n", 1),
        ("    pass\n", 1),
    ],
)
def test_a_structural_protocol_the_classes_satisfy_types_the_seam(tmp_path, protocol, errors):
    seam = f"from typing import Protocol\n\nclass Source(Protocol):\n{protocol}"
    unchanged = {"scripts/seam.py": seam, "tests/test_seam.py": "def broken(:\n"}
    head = {**unchanged, "scripts/a.py": _impl("Http", FETCH), "scripts/b.py": _impl("Disk", FETCH)}
    assert len(_errors(tmp_path, head, unchanged)) == errors


def test_only_changed_concrete_classes_are_grouped(tmp_path):
    old = _impl("Http", FETCH)
    head = {
        "scripts/a.py": old,
        "scripts/b.py": _impl("Disk", FETCH),
        "scripts/c.py": "from abc import ABC\n\nclass Shape(ABC):\n" + FETCH,
        "scripts/d.py": "class Failure(ValueError):\n" + FETCH,
        "scripts/e.py": "from dataclasses import dataclass\n\n@dataclass\nclass Row:\n" + FETCH,
    }
    assert _errors(tmp_path, head, {"scripts/a.py": old}) == []


def test_a_concrete_inheritance_cycle_sharing_a_contract_terminates(tmp_path):
    source = _impl("A", FETCH, "(B)") + _impl("B", FETCH, "(A)")
    assert _errors(tmp_path, {"scripts/job.py": source}) == [
        _inherits("scripts/job.py::A", "scripts.job.B"),
        _inherits("scripts/job.py::B", "scripts.job.A"),
    ]


def test_main_names_every_rule_and_exits_red(tmp_path, capsys):
    base = _tree(tmp_path / "base", KEEP)
    source = CONCRETE + "\n\nclass Cache(Store):\n    pass\n\n\ndef run(path, **options) -> None:\n    pass\n"
    head = _tree(tmp_path / "head", {**KEEP, "scripts/store.py": source})
    assert design_rules.main(["--base", str(base), "--head", str(head)]) == 1
    assert capsys.readouterr().out == (
        "4 new or changed units graded on the design rules, 3 errors\n"
        f"::error::{_inherits('scripts/store.py::Cache', 'scripts.store.Store')}\n"
        "::error::untyped: public scripts/store.py::run lacks type hints on path, options\n"
        "::error::config: public scripts/store.py::run takes **options as a plain dict; "
        "take a typed configuration object\n"
    )


def test_main_is_green_on_an_unchanged_tree(tmp_path, capsys):
    tree = _tree(tmp_path / "tree", {"scripts/job.py": UNTYPED})
    assert design_rules.main(["--base", str(tree), "--head", str(tree)]) == 0
    assert capsys.readouterr().out == "0 new or changed units graded on the design rules, 0 errors\n"


def test_main_grades_the_working_directory_by_default(tmp_path, capsys, monkeypatch):
    base = _tree(tmp_path / "base", KEEP)
    head = _tree(tmp_path / "head", {**KEEP, "scripts/job.py": UNTYPED})
    monkeypatch.chdir(head)
    assert design_rules.main(["--base", str(base)]) == 1
    assert "::error::untyped: public scripts/job.py::load" in capsys.readouterr().out


def test_main_requires_a_base(capsys):
    with pytest.raises(SystemExit) as exited:
        design_rules.main([])
    assert exited.value.code == 2
    assert "the following arguments are required: --base" in capsys.readouterr().err


@pytest.mark.parametrize("side", ["base", "head"])
def test_main_reports_a_syntax_error_naming_its_file(tmp_path, capsys, side):
    broken = {**KEEP, "scripts/job.py": "def broken(:\n"}
    fine = {**KEEP, "scripts/job.py": TYPED}
    trees = {"base": broken if side == "base" else fine, "head": broken if side == "head" else fine}
    base, head = _tree(tmp_path / "base", trees["base"]), _tree(tmp_path / "head", trees["head"])
    assert design_rules.main(["--base", str(base), "--head", str(head)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("::error::")
    assert "(job.py, line 1)" in out


def test_the_lint_job_runs_the_gate_from_the_protected_grader():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/test.yml").read_text()
    step = workflow.split("- name: Hold the design rules on new and changed units\n", 1)[1].split("- name:", 1)[0]
    assert 'cd "$RUNNER_TEMP/grader"' in step
    assert 'python -m scripts.design_rules --base "$RUNNER_TEMP/base" --head "$GITHUB_WORKSPACE"' in step
