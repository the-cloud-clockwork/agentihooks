"""The compose master drill runs in the promotion swarm smoke: it retires the front door master of a scratch proof
swarm in the built swarm image and fails unless the tick's night watch forced up a claude ai login successor within
thirty minutes."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/packaging/compose-master-drill.sh"

pytestmark = pytest.mark.unit


def drill() -> str:
    """The compose master drill script."""
    return (ROOT / SCRIPT).read_text()


@pytest.mark.feature("compose-master-drill")
def test_the_swarm_image_job_runs_the_master_drill_on_the_built_image() -> None:
    steps = yaml.safe_load((ROOT / ".github/workflows/swarm-smoke.yml").read_text())["jobs"]["smoke"]["steps"]
    parallel = next(step for step in steps if "scripts/packaging/swarm-smoke-parallel.sh" in step.get("run", ""))
    assert SCRIPT in parallel["run"].split()
    assert "master drill" in parallel["name"]


@pytest.mark.feature("compose-master-drill")
def test_the_master_drill_binds_ports_apart_from_every_other_compose_scenario() -> None:
    script = drill()
    assert 'SWARM_PORT="${SWARM_PORT:-22765}"' in script
    assert 'SWARM_REDIS_PORT="${SWARM_REDIS_PORT:-22379}"' in script
    taken = {
        port
        for other in (ROOT / "scripts/packaging").glob("*.sh")
        if other.name != Path(SCRIPT).name
        for port in re.findall(r'PORT="\$\{SWARM_\w*PORT:-(\d+)\}"', other.read_text())
    }
    assert taken
    assert {"22765", "22379"}.isdisjoint(taken)
    assert "trap cleanup EXIT" in script and "docker compose down --volumes" in script


@pytest.mark.feature("compose-master-drill")
def test_the_master_drill_retires_a_live_front_door_master_through_the_tick_night_watch_with_a_login_stand_in() -> None:
    script = drill()
    assert 'SLUG="proof-ad0e11-master-1"' in script
    assert 'SwarmConfig(slug, "/data", max_eng=0, max_ci=0, max_plan=0, scratch=True)' in script
    assert 'state="working", seat=seat' in script
    assert 'route_kind="master-login")' in script
    assert (
        'master_drill.Drill(store, slug, ledger, runtime, {"tasks": ledger.tasks(slug)}, now, masters._night_watch)'
        in script
    )
    assert script.index("record = master_drill.run(drill)") < script.index('record="$(docker compose exec')


@pytest.mark.feature("compose-master-drill")
def test_the_master_drill_fails_unless_a_login_successor_held_the_seat_within_thirty_minutes() -> None:
    script = drill()
    checks = (
        'assert record["retired"] == ["master@ad0e11-0001"]',
        'assert record["successor"] in record["launched"] and record["successor"] != "master@ad0e11-0001"',
        'assert record["seated_minute"] is not None and record["seated_minute"] <= 30',
        'assert (record["route"], record["recovered"]) == ("claude ai login", True)',
    )
    assert all(check in script for check in checks)
    assert script.index(checks[-1]) < script.index("printf 'Master drill passed")
