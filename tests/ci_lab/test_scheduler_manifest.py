from collections.abc import Callable
from dataclasses import replace

import pytest

from scripts.ci_lab.objects import manifest_folder
from tests.ci_lab.test_scheduler import HEAD_A, HEAD_B, SLUG, Run, plan, unit
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab


@pytest.mark.feature("lab-worker-lease-manifest")
def test_each_enqueued_job_names_the_manifest_its_grade_keyed_its_test_units_on(lab: Callable[..., Run]) -> None:
    commit = "f" * 40
    run = lab({HEAD_A: replace(plan({"lint": (unit(1),)}), manifest=commit), HEAD_B: plan({"lint": (unit(2),)})})
    run.push(HEAD_A, lane="engineer-1")
    run.push(HEAD_B, lane="engineer-2")
    run.tick(1000)

    grants = run.lease(slots=2)

    assert sorted((grant.head, grant.manifest) for grant in grants) == [
        (HEAD_A, f"{manifest_folder(SLUG)}{commit}.json"),
        (HEAD_B, ""),
    ]
