"""Check every registered tick step against the feature map and the scenario index.

``check`` loads ``feature-map.json`` and ``tests/swarm/tick_scenarios/steps.json``, imports every step module and
validates the registry with that catalogue. The index's unmapped steps may only shrink against a base tree.
"""

import argparse
import json
from importlib import import_module
from pathlib import Path

from scripts.tick import registry
from scripts.tick.registry import Catalogue

FEATURE_MAP = Path("feature-map.json")
INDEX = Path("tests/swarm/tick_scenarios/steps.json")
GOLDENS = Path("tests/swarm/tick_scenarios/goldens.json")
STEP_MODULES = tuple(
    f"scripts.tick.steps.{name}" for name in ("observe", "agents", "host", "masters", "place", "report", "tasks")
)


def scenarios(root: Path) -> frozenset[str]:
    """The recorded tick scenario names of ``root``, each golden key without its entry point."""
    return frozenset(key.split("/", 1)[-1] for key in json.loads((root / GOLDENS).read_text()))


def load(root: Path) -> Catalogue:
    """The catalogue recorded in ``root``, refusing an index that names a scenario no golden records."""
    features = frozenset(entry["id"] for entry in json.loads((root / FEATURE_MAP).read_text())["features"])
    index = json.loads((root / INDEX).read_text())
    listed = {step: frozenset(names) for step, names in index["scenarios"].items()}
    unknown = sorted(frozenset().union(*listed.values()) - scenarios(root))
    if unknown:
        raise ValueError(f"{INDEX} names unknown scenarios: {', '.join(unknown)}")
    return Catalogue(features, listed, frozenset(index["unmapped"]))


def unmapped(root: Path) -> frozenset[str] | None:
    """The unmapped steps the index of ``root`` records, or nothing when it has no index."""
    path = root / INDEX
    return frozenset(json.loads(path.read_text())["unmapped"]) if path.is_file() else None


def grown(base: frozenset[str] | None, head: frozenset[str]) -> list[str]:
    """Refuse each unmapped step of the head the base does not hold; a base without an index allows the head's."""
    if base is None:
        return []
    return [f"Unmapped step {name} is new; map it to a feature and a scenario instead" for name in sorted(head - base)]


def check(root: Path, base: Path | None = None) -> list[str]:
    """Every error of the registered steps against the catalogue of ``root`` and its unmapped steps against ``base``."""
    for module in STEP_MODULES:
        import_module(module)
    catalogue = load(root)
    errors = grown(None if base is None else unmapped(base), catalogue.unmapped)
    try:
        registry.validate(catalogue)
    except ValueError as exc:
        errors += str(exc).splitlines()
    return errors


def main(argv: list[str] | None = None) -> int:
    """Check the registered steps of a tree against its catalogue and a base tree."""
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("check")
    command.add_argument("--root", type=Path, default=Path("."))
    command.add_argument("--base", type=Path)
    args = parser.parse_args(argv)
    errors = check(args.root, args.base)
    print(f"{len(errors)} tick catalogue errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
