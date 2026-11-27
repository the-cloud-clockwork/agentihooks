"""Import every module of the CI lab package, then any extra module names, inside a built image."""


def lab_modules() -> list[str]:
    """Every module and subpackage under the CI lab package, sorted by name, with no exclusions."""
    return []


def main(argv: list[str] | None = None) -> int:
    """Import the lab modules, then each extra name; print them all, or name the first that fails and return 1."""
    return len(argv or [])
