import sys
from types import ModuleType

import pytest

import scripts
from scripts.targets._common import _install_module


@pytest.mark.parametrize("identity", ["install", "scripts.install"])
def test_install_module_reuses_loaded_identity(identity: str, monkeypatch: pytest.MonkeyPatch) -> None:
    installer = ModuleType(identity)
    monkeypatch.delitem(sys.modules, "install", raising=False)
    monkeypatch.delitem(sys.modules, "scripts.install", raising=False)
    monkeypatch.setitem(sys.modules, identity, installer)

    assert _install_module() is installer


def test_install_module_imports_when_no_identity_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    installer = ModuleType("scripts.install")
    main = ModuleType("__main__")
    main.__file__ = "runner.py"
    monkeypatch.delitem(sys.modules, "install", raising=False)
    monkeypatch.delitem(sys.modules, "scripts.install", raising=False)
    monkeypatch.setitem(sys.modules, "__main__", main)
    monkeypatch.setattr(scripts, "install", installer, raising=False)

    assert _install_module() is installer


def test_install_module_reuses_the_running_script(monkeypatch: pytest.MonkeyPatch) -> None:
    installer = ModuleType("__main__")
    installer.__file__ = "scripts/install.py"
    monkeypatch.delitem(sys.modules, "install", raising=False)
    monkeypatch.delitem(sys.modules, "scripts.install", raising=False)
    monkeypatch.setitem(sys.modules, "__main__", installer)

    assert _install_module() is installer
