import pytest

from scripts.swarm import stage_detector
from scripts.swarm.health import standing
from tests.swarm.test_stages import agent, delivery, rows, run, stage
from tests.swarm.test_stages import store as store

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

H = 3_600_000
NOW = 1_791_744_000_000


def replay():
    ended = [1_791_736_920_350, 1_791_737_889_784, 1_791_742_500_838]
    elapsed = [708, 1104, 1107]
    tasks = []
    found = rows()
    for number, (end, seconds) in enumerate(zip(ended, elapsed), 1):
        task, branch = f"t{number}", f"b{number}"
        tasks.append({"id": task, "state": "done", "branch": branch})
        start = end - 1_200_000
        found["agent_events"].append(agent("claim", task, start))
        found["delivery_events"].append(delivery("merge", task, end))
        found["ci_runs"].append(run(number, branch, start, end, "success"))
        found["ci_stages"].append({**stage(number, "mutation-stats", seconds, 210, 0, "ubuntu"), "ts_ms": end})
    return found, tasks


def test_eleventh_replay_counts_three_overruns_in_one_standing_finding(store):
    source, tasks = replay()
    report = stage_detector.report(source, tasks, NOW, 4 * H)
    stage_detector.update_findings(store, "scratch", report, NOW)
    found = standing.read(store.redis, "scratch")
    overrun = found["ci_overrun"]
    assert overrun["fired"] == 3
    assert overrun["detector"]["seconds"] == 2289.0
    assert overrun["detector"]["labels"] == ["mutation-stats"]
    assert len([row for row in found.values() if row["stage"] == "ci_overrun"]) == 1
    stage_detector.update_findings(store, "scratch", report, NOW + 60_000)
    assert standing.read(store.redis, "scratch") == found
    assert report["top"][0]["stage"] == "ci_overrun"
    assert report["top"][0]["trend"] == "rising"
    assert len(report["top"]) <= 3
