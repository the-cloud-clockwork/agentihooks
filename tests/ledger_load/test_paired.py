import json

import pytest

from tests.ledger_load import paired

BASE = {"seconds": 90, "errors": 0, "write_p95_s": 1.0, "busiest_cores": 0.5}


def log(*measures: dict) -> str:
    return "\n".join(["ledger load-gate: 1000 tasks", *(json.dumps(measure) for measure in measures), "done"])


def test_a_head_within_the_margin_of_its_base_passes():
    head = {**BASE, "write_p95_s": 1.1, "busiest_cores": 0.55}
    assert paired.regressions(BASE, head) == []


@pytest.mark.parametrize(
    "change, problem",
    [
        ({"errors": 1}, "head had 1 failed requests, base 0"),
        ({"write_p95_s": 1.11}, "head write p95 1.11 exceeds base 1.0 by more than 1.1x"),
        ({"busiest_cores": 0.56}, "head busiest CPU window 0.56 exceeds base 0.5 by more than 1.1x"),
        ({"write_p95_s": None}, "write p95 was not measured on the head"),
        ({"busiest_cores": None}, "busiest CPU window was not measured on the head"),
    ],
)
def test_a_head_regression_or_missing_measure_is_named(change, problem):
    assert paired.regressions(BASE, {**BASE, **change}) == [problem]


def test_a_base_missing_its_measure_is_named():
    assert paired.regressions({**BASE, "busiest_cores": None}, BASE) == [
        "busiest CPU window was not measured on the base"
    ]
    assert paired.regressions(None, BASE) == ["the base runs printed no load measure"]
    assert paired.regressions(BASE, None) == ["the head runs printed no load measure"]


def test_each_side_is_graded_by_its_best_run_and_its_worst_errors():
    noisy = {**BASE, "errors": 2, "write_p95_s": 3.0, "busiest_cores": 0.4}
    assert paired.best([BASE, noisy]) == {"errors": 2, "write_p95_s": 1.0, "busiest_cores": 0.4}
    unmeasured = {**BASE, "busiest_cores": None}
    assert paired.best([unmeasured, BASE]) == {"errors": 0, "write_p95_s": 1.0, "busiest_cores": 0.5}
    assert paired.best([unmeasured]) == {"errors": 0, "write_p95_s": 1.0, "busiest_cores": None}
    assert paired.best([BASE, None]) is None
    assert paired.best([]) is None


def test_the_graded_measure_is_the_repeat_when_the_gate_measured_twice():
    repeat = {**BASE, "write_p95_s": 0.9}
    assert paired.graded_measure(log({**BASE, "write_p95_s": 3.0}, repeat)) == repeat
    assert paired.graded_measure("no measure here") is None


def test_the_command_fails_on_a_planted_head_regression(tmp_path, capsys):
    base, head, slow = tmp_path / "base.log", tmp_path / "head.log", tmp_path / "slow.log"
    base.write_text(log(BASE))
    head.write_text(log(BASE))
    slow.write_text(log({**BASE, "busiest_cores": 1.0}))
    assert paired.main(["--base", str(base), str(base), "--head", str(head), str(slow)]) == 0
    assert paired.main(["--base", str(base), "--head", str(slow), str(slow)]) == 1
    assert "::error::head busiest CPU window 1.0 exceeds base 0.5 by more than 1.1x" in capsys.readouterr().out
