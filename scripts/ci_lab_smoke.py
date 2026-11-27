"""Import every module of the CI lab package, then any extra module names, inside a built image.

The swarm image smoke and the CI lab image smoke both run python -m scripts.ci_lab_smoke, so a module added to the lab
package is imported in both images with no list to edit. The first module that does not import ends the run with exit
status 1 and its name.
"""

import importlib
import pkgutil
import sys

import scripts.ci_lab


def lab_modules() -> list[str]:
    """Every module and subpackage under the CI lab package, sorted by name, with no exclusions."""
    found = pkgutil.walk_packages(scripts.ci_lab.__path__, f"{scripts.ci_lab.__name__}.")
    return sorted(module.name for module in found)


def main(argv: list[str] | None = None) -> int:
    """Import the lab modules, then each extra name; print them all, or name the first that fails and return 1."""
    names = [*lab_modules(), *(sys.argv[1:] if argv is None else argv)]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as error:
            print(f"{name} does not import: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
    print("imported:", ", ".join(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
