import importlib
import sys


class MutmutProgrammaticFailException(Exception):
    pass


def mangled_name_from_mutant_name(mutant_name: str) -> str:
    assert "__mutmut_" in mutant_name, mutant_name
    return mutant_name.partition("__mutmut_")[0]


def record_trampoline_hit(name: str) -> None:
    # The mutation run reads stats only from its own processes, which hold the real module.
    pass


def __getattr__(name: str) -> object:
    if name.startswith("__"):
        raise AttributeError(name)
    if getattr(sys.modules.get("mutmut.__main__"), "__getattr__", None) is __getattr__:
        del sys.modules["mutmut.__main__"]
    return getattr(importlib.import_module("mutmut.__main__"), name)
