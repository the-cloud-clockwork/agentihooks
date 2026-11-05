"""Check every registered tick step against the feature map and the scenario index.

``check`` loads ``feature-map.json`` and ``tests/swarm/tick_scenarios/steps.json``, imports every step module and
validates the registry with that catalogue. ``shrink`` refuses an unmapped step a base tree's index does not hold; CI
runs it from the base tree's copy of this module so a head cannot loosen it. ``render`` writes the step catalogue page
from the registry.
"""

import argparse
import json
import pkgutil
from collections.abc import Iterable
from importlib import import_module
from pathlib import Path

from scripts.tick import registry, steps
from scripts.tick.registry import Catalogue
from scripts.tick.step import Step

FEATURE_MAP = Path("feature-map.json")
INDEX = Path("tests/swarm/tick_scenarios/steps.json")
GOLDENS = Path("tests/swarm/tick_scenarios/goldens.json")
PAGE = Path("docs/reference/tick-step-catalogue.md")
HEADER = """---
title: Tick step catalogue
parent: Reference
nav_order: 73
---

# Tick step catalogue

Every registered tick step, by group and name, with the lane it runs on, its budget floor and interval in seconds, the
steps it needs, the ports its effects name and its feature map id. `python -m scripts.tick.catalogue render` writes this page
from `scripts.tick.registry.steps()`, and `tests/test_docs_tick.py` fails when it no longer matches the registry.
How a step is added is in [Tick engine steps](tick-engine-steps.md#adding-a-step).

| Step | Group | Lane | Floor | Interval | Needs | Effects | Feature |
|---|---|---|---|---|---|---|---|
"""


def step_modules() -> list[str]:
    """Every module of the step package, discovered from its path."""
    return sorted(f"{steps.__name__}.{module.name}" for module in pkgutil.iter_modules(steps.__path__))


def scenarios(root: Path) -> frozenset[str]:
    """The recorded tick scenario names of ``root``, each golden key without its entry point."""
    return frozenset(key.split("/", 1)[-1] for key in json.loads((root / GOLDENS).read_text()))


def load(root: Path) -> Catalogue:
    """The catalogue recorded in ``root``, refusing an index that names a scenario no golden records."""
    features = frozenset(entry["id"] for entry in json.loads((root / FEATURE_MAP).read_text())["features"])
    index = json.loads((root / INDEX).read_text())
    listed: dict[str, frozenset[str]] = {step: frozenset(names) for step, names in index["scenarios"].items()}
    unknown = sorted(frozenset[str]().union(*listed.values()) - scenarios(root))
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


def check(root: Path) -> list[str]:
    """Every error of the registered steps against the catalogue of ``root``."""
    for module in step_modules():
        import_module(module)
    try:
        registry.validate(load(root))
    except ValueError as exc:
        return str(exc).splitlines()
    return []


def shrink(base: Path, head: Path) -> list[str]:
    """Every unmapped step the index of ``head`` adds to the index of ``base``."""
    return grown(unmapped(base), unmapped(head) or frozenset())


def names(values: Iterable[str]) -> str:
    """The sorted ``values`` joined by commas, or none when there are none."""
    return ", ".join(sorted(values)) or "none"


def row(step: Step) -> str:
    """One catalogue table row of ``step``."""
    cells = (
        step.name,
        step.group,
        step.lane,
        f"{step.budget_s:g}",
        f"{step.interval_s:g}",
        names(step.needs),
        names(step.effects),
        step.feature or "none",
    )
    return f"| {' | '.join(cells)} |"


def page(found: Iterable[Step]) -> str:
    """The catalogue page of the ``found`` steps, one row each, ordered by group and name."""
    return HEADER + "".join(f"{row(step)}\n" for step in sorted(found, key=lambda step: (step.group, step.name)))


def render(root: Path) -> Path:
    """Write the catalogue page of every registered step under ``root`` and return its path."""
    for module in step_modules():
        import_module(module)
    path = root / PAGE
    path.write_text(page(registry.steps()))
    return path


def main(argv: list[str] | None = None) -> int:
    """Check the registered steps of a tree against its catalogue, its unmapped steps against a base tree, or render
    the step catalogue page of a tree."""
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check").add_argument("--root", type=Path, default=Path("."))
    commands.add_parser("render").add_argument("--root", type=Path, default=Path("."))
    shrinking = commands.add_parser("shrink")
    shrinking.add_argument("--base", type=Path, required=True)
    shrinking.add_argument("--head", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "render":
        print(f"wrote {render(args.root)}")
        return 0
    errors = check(args.root) if args.command == "check" else shrink(args.base, args.head)
    print(f"{len(errors)} tick catalogue errors")
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
