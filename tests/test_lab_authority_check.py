"""The live check of the lab authoritative static gates over this checkout and over damaged copies of it."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci_lab import delivery, static_exclusions
from scripts.feature_checks import lab_authority as check

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
HELD = [
    "the checkout holds exactly lint, type-gate and test-count in the lab authoritative list",
    *(
        line
        for gate in check.GATES
        for line in (
            f"the checkout holds lab authority for {gate}",
            f"the checkout holds the lab check that replaces the {gate} job",
            f"the checkout holds the lab skip clause of the {gate} job",
        )
    ),
]
DAMAGE: list[tuple[str, Callable[[Any], object], set[int]]] = [
    (delivery.DELIVERY, lambda document: document["lab"]["authoritative"].remove("test-count"), {0, 7}),
    (delivery.DELIVERY, lambda document: document["lab"]["authoritative"].append("docs"), {0}),
    (
        static_exclusions.PATH,
        lambda document: document.update({"lint/lint-check": {"needs": ["network"], "reason": "planted"}}),
        {1},
    ),
    (check.WIRING, lambda document: document["lab_gates"].pop("lab/type-gate"), {5}),
    (check.WORKFLOW, lambda document: document["jobs"]["test-count"].update({"if": "${{ !cancelled() }}"}), {9}),
    (check.WORKFLOW, lambda document: document["jobs"]["lint"]["needs"].remove("lab-verdict"), {3}),
]


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    for path in (delivery.DELIVERY, static_exclusions.PATH, check.WIRING):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, tmp_path / path)
    shutil.copytree(ROOT / delivery.WORKFLOWS, tmp_path / delivery.WORKFLOWS)
    return tmp_path


def _edit(path: Path, change: Callable[[Any], object]) -> None:
    document = yaml.safe_load(path.read_text())
    change(document)
    path.write_text(json.dumps(document) if path.suffix == ".json" else yaml.safe_dump(document))


@pytest.mark.feature("lab-authoritative-static-gates")
def test_the_checkout_holds_every_part_of_the_lab_authority_and_each_is_listed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check.ROOT == ROOT
    assert check.main() == 0
    assert capsys.readouterr().out.splitlines() == HELD


@pytest.mark.feature("lab-authoritative-static-gates")
@pytest.mark.parametrize(
    ("path", "change", "lost"),
    DAMAGE,
    ids=[
        "a listed gate removed",
        "a fourth gate listed",
        "a lint step excluded",
        "a lab check undeclared",
        "a skip clause dropped",
        "a lab verdict need dropped",
    ],
)
def test_a_part_the_checkout_lost_fails_by_name(
    copy: Path, capsys: pytest.CaptureFixture[str], path: str, change: Callable[[Any], object], lost: set[int]
) -> None:
    _edit(copy / path, change)
    expected = [
        line.replace("the checkout holds", "FAIL: the checkout lacks") if index in lost else line
        for index, line in enumerate(HELD)
    ]
    assert check.main(copy) == 1
    assert capsys.readouterr().out.splitlines() == expected
