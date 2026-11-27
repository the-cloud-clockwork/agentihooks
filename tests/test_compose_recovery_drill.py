import ast
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import yaml

from scripts.events.envelope import Envelope
from scripts.swarm import capacity, offer_quota
from scripts.swarm.store import SwarmConfig

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/packaging/compose-recovery-drill.sh"
DRILL = "DRILL=\"$(cat <<'PY'\n"


def drill():
    return (ROOT / SCRIPT).read_text()


def heredoc(script: str, opener: str) -> str:
    return script.split(opener, 1)[1].split("\nPY\n", 1)[0]


def function(body: str, name: str) -> str:
    found = next(node for node in ast.parse(body).body if isinstance(node, ast.FunctionDef) and node.name == name)
    source = ast.get_source_segment(body, found)
    assert source is not None
    return source


def spoke_reader(body: str) -> str | None:
    call = next(
        node
        for node in ast.walk(ast.parse(body))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "SpokeConfig"
    )
    return next((ast.unparse(keyword.value) for keyword in call.keywords if keyword.arg == "accounts"), None)


def assert_seat_fits(source: str) -> None:
    namespace: dict[str, object] = {"capacity": capacity}
    exec(source, namespace)
    seat = cast(Callable[..., list[capacity.Account]], namespace["seat"])
    config = SwarmConfig("rig", "/data", 1, 1, engine="events")
    offered = Envelope(
        "ready:rig:t1:0", "task.ready", "rig", "task", "t1", 0, 9, "readiness", {"lane": "eng", "quota": {}}
    )
    seated = offer_quota.Fit(config, "eng", lambda event: True, seat)
    assert seated(offered) and not seated.held
    seatless = offer_quota.Fit(config, "eng", lambda event: True, lambda *_args, **_kwargs: [])
    assert not seatless(offered) and seatless.held


def test_the_swarm_image_job_runs_the_recovery_drill_on_the_built_image():
    steps = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]["steps"]
    parallel = next(step for step in steps if "scripts/packaging/swarm-smoke-parallel.sh" in step.get("run", ""))
    assert SCRIPT in parallel["run"].split()


def test_the_recovery_drill_binds_ports_apart_from_the_other_compose_scenarios():
    script = drill()
    assert 'SWARM_PORT="${SWARM_PORT:-38765}"' in script
    assert 'SWARM_REDIS_PORT="${SWARM_REDIS_PORT:-36379}"' in script
    assert "trap cleanup EXIT" in script
    assert "docker compose down --volumes" in script


def test_the_recovery_drill_restores_the_cold_backup_before_the_hub_reconciles():
    script = drill()
    backup = script.index("volume 'tar -C /data -cf /backup/jetstream.tar .'")
    restore = script.index("volume 'find /data -mindepth 1 -delete && tar -C /data -xf /backup/jetstream.tar'")
    assert script.index("drill seed t1 t2 t3 t4 t5") < backup < script.index("drill done t1") < restore
    final_settle = script.index('"t1": {"ledger": "done", "claim": null, "launches": 1,')
    assert restore < script.index("drill beat") < script.index("drill die t2 t4") < final_settle
    assert script.index("drill die t2 t4") < script.index("state after the first hub pass") < final_settle
    assert script.count("docker compose stop nats") == 2


def test_the_recovery_drill_requires_every_task_done_with_its_recorded_claims_and_no_agent():
    script = drill()
    expected = (
        '"t1": {"ledger": "done", "claim": null, "launches": 1,',
        '$(task t2 2 \'"exited", "exited"\')',
        "$(task t3 1 '\"exited\"')",
        '$(task t4 2 \'"exited", "exited"\')',
        "$(task t5 1 '\"exited\"')",
        "$(task t6 1 '\"exited\"')",
    )
    assert all(part in script for part in expected)
    assert '"ledger": "done", "claim": null, "launches": %s, "jetstream": [%s]' in script
    assert "'\"agents\": []'" in script
    final_settle = script.index('"t1": {"ledger": "done", "claim": null, "launches": 1,')
    assert final_settle < script.index("printf 'Recovery drill passed")


def test_the_recovery_drill_runs_each_hub_pass_inside_its_state_read_with_a_short_fetch():
    script = drill()
    assert "agentihooks hub run --once" not in script
    assert 'runner.main(["run", "--once"])' in script
    assert "timeout_ms=FETCH_MS" in script
    assert 'found="$(drill pass)"' in script


@pytest.mark.feature("compose-drill-take-bound")
def test_the_recovery_drill_take_stops_on_held_polls_and_names_them() -> None:
    polls: list[int] = []

    class Held:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def poll(self, timeout_ms: int) -> str:
            polls.append(timeout_ms)
            assert len(polls) <= 50, "the take loop kept polling a spoke that only holds"
            return "held"

    namespace: dict[str, object] = {
        "sys": sys,
        "launched": lambda: [],
        "open_bus": lambda config: SimpleNamespace(close=lambda: None),
        "BusConfig": SimpleNamespace(from_environ=lambda: None),
        "Spoke": Held,
        "SpokeConfig": lambda *args: None,
        "store": SimpleNamespace(config=lambda slug: None),
        "slug": "recovery-drill",
        "HIVE": "recovery-hive",
        "StandIn": lambda: None,
        "Tasks": lambda: None,
        "view": lambda: None,
        "seat": lambda *args, **kwargs: [],
    }
    exec(function(heredoc(drill(), DRILL), "take"), namespace)
    take = cast(Callable[[int], object], namespace["take"])
    held = "['held', 'held', 'held', 'held', 'held']"
    with pytest.raises(
        SystemExit, match=rf"^the hive took \[\] of 3 offers before its polls went idle: {re.escape(held)}$"
    ):
        take(3)
    assert polls == [1_000] * 5


@pytest.mark.feature("compose-smoke-spoke-seat")
def test_the_recovery_drill_spoke_reads_a_seat_that_fits_its_offers() -> None:
    body = heredoc(drill(), DRILL)
    assert spoke_reader(function(body, "take")) == "seat"
    assert_seat_fits(function(body, "seat"))
