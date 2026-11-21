"""Tests for the scheduler gauges no queue state answers: the node mirror's age and the GitHub calls left."""

from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from scripts.ci_lab.checks import Checks, PostRefused
from tests.ci_lab.test_checks import LIMITED, OUTPUT, Token, answer
from tests.ci_lab.test_scheduler import HEAD_A, SLUG, Run, scrape
from tests.ci_lab.test_scheduler import lab as _lab

lab = _lab
MIRROR_AGE = "ci_lab_mirror_age_seconds"
RATE_REMAINING = "ci_lab_github_rate_remaining"


def samples(text: str, name: str) -> list[str]:
    """Return the exposition's sample lines of one unlabelled metric."""
    return [line for line in text.splitlines() if line.startswith(f"{name} ")]


@pytest.mark.feature("lab-scheduler-mirror-age-and-github-rate")
def test_the_exposition_reports_the_node_mirror_age_from_its_fetched_at_stamp(
    lab: Callable[..., Run], tmp_path: Path
) -> None:
    run = lab({})
    mirror = tmp_path / "node"
    mirror.mkdir()
    stamp = mirror / "fetched-at"
    assert samples(scrape(run, 1_030_000, mirror), MIRROR_AGE) == []
    stamp.write_text("1000\n")
    assert samples(scrape(run, 1_030_000, mirror), MIRROR_AGE) == [f"{MIRROR_AGE} 30.0"]
    assert samples(scrape(run, 1_090_500, mirror), MIRROR_AGE) == [f"{MIRROR_AGE} 90.5"]
    stamp.write_text("2000\n")
    assert samples(scrape(run, 1_090_500, mirror), MIRROR_AGE) == [f"{MIRROR_AGE} 0.0"]
    stamp.write_text("nan\n")
    assert samples(scrape(run, 1_090_500, mirror), MIRROR_AGE) == []
    stamp.write_text("1000\n")
    assert samples(scrape(run, 1_090_500, mirror), MIRROR_AGE) == [f"{MIRROR_AGE} 90.5"]
    stamp.unlink()
    assert samples(scrape(run, 1_090_500, mirror), MIRROR_AGE) == []
    assert samples(scrape(run, 1_090_500), MIRROR_AGE) == []


@pytest.mark.feature("lab-scheduler-mirror-age-and-github-rate")
def test_the_exposition_reports_the_github_calls_left_from_each_check_runs_answer(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = lab({})
    waited: list[list[str]] = []
    limited = answer(403, text=LIMITED, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1000"})
    accepted = answer(201, {"id": 7}, headers={"x-ratelimit-remaining": "4999"})
    unreported = answer(200, {"id": 7})
    refused = answer(422, headers={"x-ratelimit-remaining": "4997"})
    garbled = answer(201, {"id": 8}, headers={"x-ratelimit-remaining": "many"})
    monkeypatch.setattr(requests, "request", Mock(side_effect=[limited, accepted, unreported, refused, garbled]))

    def sleep(_seconds: float) -> None:
        waited.append(samples(scrape(run, 1000), RATE_REMAINING))

    poster = Checks(SLUG, Token(), registry=run.engine.registry, sleep=sleep, clock=lambda: 1000.0)
    assert samples(scrape(run, 1000), RATE_REMAINING) == []
    assert poster.post(HEAD_A, "lint", "queued", None, OUTPUT, False) == 7
    assert waited == [[f"{RATE_REMAINING} 0.0"]]
    assert samples(scrape(run, 1000), RATE_REMAINING) == [f"{RATE_REMAINING} 4999.0"]
    assert poster.post(HEAD_A, "lint", "in_progress", None, OUTPUT, False) == 7
    assert samples(scrape(run, 1000), RATE_REMAINING) == [f"{RATE_REMAINING} 4999.0"]
    with pytest.raises(PostRefused):
        poster.post(HEAD_A, "lint", "completed", "success", OUTPUT, False)
    assert samples(scrape(run, 1000), RATE_REMAINING) == [f"{RATE_REMAINING} 4997.0"]
    assert poster.post(HEAD_A, "x", "queued", None, OUTPUT, False) == 8
    assert samples(scrape(run, 1000), RATE_REMAINING) == [f"{RATE_REMAINING} 4997.0"]
