import importlib
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def tree(tmp_path, monkeypatch):
    package = tmp_path / "pkg" / "sub"
    package.mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "mod.py").write_text("RUNS = globals().get('RUNS', 0) + 1\n")
    other = tmp_path / "other"
    other.mkdir()
    (other / "mod.py").write_text("OTHER = True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    before = set(sys.modules)
    meta_path = list(sys.meta_path)
    yield tmp_path
    sys.meta_path[:] = meta_path
    for name in set(sys.modules) - before:
        del sys.modules[name]


def install(root, path):
    from scripts.ci_mutation import identity

    paths = None if path is None else [path] if isinstance(path, str) else path
    config = SimpleNamespace(known_args_namespace=SimpleNamespace(mutated_path=paths), rootpath=root, cleanups=[])
    config.add_cleanup = config.cleanups.append
    identity.pytest_load_initial_conftests(config, None, [])
    return config


def test_short_name_import_returns_the_package_module(tree, monkeypatch):
    install(tree, "pkg/sub/mod.py")
    monkeypatch.syspath_prepend(str(tree / "pkg"))
    monkeypatch.syspath_prepend(str(tree / "pkg" / "sub"))
    short = importlib.import_module("mod")
    dotted = importlib.import_module("sub.mod")
    canonical = importlib.import_module("pkg.sub.mod")
    assert short is canonical
    assert dotted is canonical
    assert sys.modules["mod"] is canonical
    assert canonical.__name__ == "pkg.sub.mod"
    assert canonical.__spec__.name == "pkg.sub.mod"
    assert canonical.RUNS == 1


def test_dotted_short_name_resolves_through_its_parent_package(tree, monkeypatch):
    install(tree, "pkg/sub/mod.py")
    monkeypatch.syspath_prepend(str(tree / "pkg"))
    assert importlib.import_module("sub.mod") is importlib.import_module("pkg.sub.mod")


def test_short_name_found_nowhere_still_fails_as_missing(tree):
    install(tree, "pkg/sub/mod.py")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("mod")


def test_reload_through_the_short_name_executes_the_module_again(tree, monkeypatch):
    install(tree, "pkg/sub/mod.py")
    monkeypatch.syspath_prepend(str(tree / "pkg" / "sub"))
    short = importlib.import_module("mod")
    assert importlib.reload(short) is sys.modules["pkg.sub.mod"]
    assert short.RUNS == 2


def test_a_different_file_with_the_same_short_name_keeps_its_own_identity(tree, monkeypatch):
    install(tree, "pkg/sub/mod.py")
    monkeypatch.syspath_prepend(str(tree / "other"))
    assert importlib.import_module("mod").OTHER
    assert "pkg.sub.mod" not in sys.modules


def test_package_init_is_aliased_under_its_package_name(tree, monkeypatch):
    install(tree, "pkg/sub/__init__.py")
    monkeypatch.syspath_prepend(str(tree / "pkg"))
    assert importlib.import_module("sub") is importlib.import_module("pkg.sub")


def test_cleanup_removes_the_alias_and_no_path_installs_nothing(tree, monkeypatch):
    meta_path = list(sys.meta_path)
    assert install(tree, None).cleanups == []
    assert sys.meta_path == meta_path
    config = install(tree, "pkg/sub/mod.py")
    assert sys.meta_path[1:] == meta_path
    for cleanup in config.cleanups:
        cleanup()
    assert sys.meta_path == meta_path
    monkeypatch.syspath_prepend(str(tree / "pkg" / "sub"))
    assert importlib.import_module("mod") is not importlib.import_module("pkg.sub.mod")


def test_option_is_registered_for_the_mutated_path():
    from scripts.ci_mutation import identity

    added = []
    parser = SimpleNamespace(addoption=lambda *args, **kwargs: added.append((args, kwargs)))
    identity.pytest_addoption(parser)
    assert added == [(("--mutated-path",), {"action": "append", "default": None})]


def test_every_mutated_path_gets_its_own_alias_and_cleanup(tree, monkeypatch):
    (tree / "other" / "extra.py").write_text("EXTRA = True\n")
    (tree / "other" / "__init__.py").write_text("")
    meta_path = list(sys.meta_path)
    config = install(tree, ["pkg/sub/mod.py", "other/extra.py"])
    monkeypatch.syspath_prepend(str(tree / "other"))
    monkeypatch.syspath_prepend(str(tree / "pkg" / "sub"))
    assert importlib.import_module("mod") is importlib.import_module("pkg.sub.mod")
    assert importlib.import_module("extra") is importlib.import_module("other.extra")
    assert sys.meta_path[2:] == meta_path
    for cleanup in config.cleanups:
        cleanup()
    assert sys.meta_path == meta_path
