import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from pathlib import Path


class ShortNameAlias(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, root: Path, path: str) -> None:
        parts = Path(path).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        self.module = ".".join(parts)
        self.origin = (root / path).resolve()

    def find_spec(self, name, path=None, target=None):
        if not self.module.endswith(f".{name}"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or spec.origin is None or Path(spec.origin).resolve() != self.origin:
            return None
        return importlib.util.spec_from_loader(name, self)

    def create_module(self, spec):
        module = importlib.import_module(self.module)
        self.spec = module.__spec__
        return module

    def exec_module(self, module) -> None:
        # The import system overwrites __spec__ with the alias spec; reload must still run the real module.
        module.__spec__ = self.spec


def pytest_addoption(parser) -> None:
    parser.addoption("--mutated-path", action="append", default=None)


def pytest_load_initial_conftests(early_config, parser, args) -> None:
    for path in early_config.known_args_namespace.mutated_path or []:
        finder = ShortNameAlias(early_config.rootpath, path)
        sys.meta_path.insert(0, finder)
        early_config.add_cleanup(lambda finder=finder: sys.meta_path.remove(finder))
