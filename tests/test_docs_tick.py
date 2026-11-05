"""The rendered tick step catalogue page equals the registry."""

import os
import subprocess
import sys
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path

from scripts.tick import catalogue, registry
from scripts.tick.step import Context, Domain, Lane, Result

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class FakeStep:
    name: str
    group: str = "observe"
    lane: Lane = Lane("tick")
    budget_s: float = 2.0
    interval_s: float = 60.0
    needs: frozenset[str] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()

    def run(self, ctx: Context) -> Result:
        return Result()


def registered() -> str:
    for module in catalogue.step_modules():
        import_module(module)
    return catalogue.page(registry.steps())


def test_the_committed_catalogue_equals_the_registry() -> None:
    assert (ROOT / catalogue.PAGE).read_text() == registered()


def test_a_row_names_every_field_of_its_step() -> None:
    step = FakeStep(
        "reap", "agents", Lane("slow"), 1.5, 600.0, frozenset({"verify", "launch checks"}), frozenset({"ledger"}), "f"
    )
    assert catalogue.row(step) == "| reap | agents | slow | 1.5 | 600 | launch checks, verify | ledger | f |"


def test_a_row_says_none_for_each_empty_field() -> None:
    assert catalogue.row(FakeStep("bare")) == "| bare | observe | tick | 2 | 60 | none | none | none |"


def test_the_page_orders_rows_by_group_then_name() -> None:
    steps = [FakeStep("b", "z"), FakeStep("c", "a"), FakeStep("a", "z")]
    rows = "".join(
        f"| {name} | {group} | tick | 2 | 60 | none | none | none |\n"
        for name, group in [("c", "a"), ("a", "z"), ("b", "z")]
    )
    assert catalogue.page(steps) == catalogue.HEADER + rows


def test_render_writes_the_registry_page_under_the_working_directory(tmp_path: Path) -> None:
    (tmp_path / catalogue.PAGE).parent.mkdir(parents=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    done = subprocess.run(
        [sys.executable, "-m", "scripts.tick.catalogue", "render"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == f"wrote {catalogue.PAGE}\n"
    assert (tmp_path / catalogue.PAGE).read_text() == registered()
