"""A mutation shard answers its forced fail control from recorded probe kills and runs on its own clock."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.ci_mutation import probe
from scripts.ci_mutation.runner import run_gate
from scripts.ci_mutation.stats import SharedStats
from tests.test_ci_mutation_probe import _plan, _records


@pytest.mark.feature("mutation-shard-probe-kills-own-clock")
def test_a_recorded_probe_kill_answers_the_forced_fail_control_without_running_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forced fail control passes no mutant and no tests; a probe kill already proves the tests catch a mutant."""
    calls: list[tuple[str | None, list[str]]] = []

    def execute(worker: str, *, mutant_name: str | None, tests: list[str]) -> int:
        calls.append((mutant_name, tests))
        return 0

    killed = probe.reuse_kills(execute, {"scripts.a.x_f__mutmut_1": "killed"})
    survived = probe.reuse_kills(execute, {"scripts.a.x_f__mutmut_1": "survived"})
    unprobed = probe.reuse_kills(execute, {})

    def control(cached: Callable[..., int], selector: str | None) -> int:
        with monkeypatch.context() as scoped:
            if selector is None:
                scoped.delenv("MUTANT_UNDER_TEST", raising=False)
            else:
                scoped.setenv("MUTANT_UNDER_TEST", selector)
            return cached("worker", mutant_name=None, tests=[])

    assert control(killed, "fail") == 1
    assert calls == []
    assert control(survived, "fail") == 0
    assert control(unprobed, "fail") == 0
    assert calls == [(None, []), (None, [])]
    assert control(killed, "stats") == 0
    assert control(killed, None) == 0
    assert calls == [(None, []), (None, []), (None, []), (None, [])]


@pytest.mark.feature("mutation-shard-probe-kills-own-clock")
def test_a_shard_after_a_slow_probe_keeps_its_whole_budget_and_reports_its_own_wall_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe ran in another job, so its seconds never shorten the shard's budget or its reported wall time."""
    base = {**_plan(), "costs": {"scripts.a.x_g__mutmut_1": 1}}
    plan = probe.merge_probes(base, _records(base))
    folder = tmp_path / "stats"
    folder.mkdir()
    (folder / "plan.json").write_text(json.dumps(plan))
    stats = SharedStats(folder, plan["head"])
    assert plan["probe"]["seconds"] == 9
    source = tmp_path / "hooks/sample.py"
    source.parent.mkdir(parents=True)
    source.write_text("def f():\n    return 1\n")
    (tmp_path / ".mutation-selection.json").write_text(json.dumps({"hooks/sample.py": ["tests/test_sample.py"]}))
    ran: list[list[str]] = []

    def mutate(
        root: Path, work: Path, selected: dict[str, tuple[set[int], list[str]]], *rest: object
    ) -> tuple[dict[str, list[dict[str, str]]], str]:
        ran.append(list(selected))
        return {"hooks/sample.py": []}, ""

    readings = iter([0.0])
    monkeypatch.setattr("scripts.ci_mutation.runner.time.monotonic", lambda: next(readings, 391.0))
    monkeypatch.setattr("scripts.ci_mutation.runner.mutate_files", mutate)
    report = run_gate(tmp_path, {"hooks/sample.py": {2}}, tmp_path / "output", 395, (0, 1), stats)
    assert ran == [["hooks/sample.py"]]
    assert report["not_mutated"] == []
    assert report["shard"]["wall_seconds"] == 391
    assert report["shard"]["budget_exhausted"] is False
    assert "over budget" not in capsys.readouterr().out
