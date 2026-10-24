import json
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "agentihooks-swarm"
SCRATCH = {"swarm": "proof-323133-t1199-1", "agent": "engineer@323133-3110", "task": "t1199", "repo": "github.com/o/r"}
HIVE = ["--set", "workers.replicas=1", "--set", "workers.credentialSecret.name=m"]
HIVE += ["--set", "workers.credentialSecret.key=c"]


def template(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", "template", "fx", str(CHART), *HIVE, *extra], capture_output=True, text=True)


def env(rendered: str, suffix: str, container: str) -> dict:
    documents = [item for item in yaml.safe_load_all(rendered) if item and item["kind"] == "Deployment"]
    pod = next(item for item in documents if item["metadata"]["name"].endswith(suffix))["spec"]["template"]["spec"]
    found = next(item for item in pod["containers"] if item["name"] == container)
    return {item["name"]: item.get("value") for item in found.get("env", [])}


def test_listed_scratch_swarms_reach_the_controller_as_one_json_setting():
    rendered = template("--set-json", f"controller.scratchSwarms=[{json.dumps(SCRATCH)}]")

    assert rendered.returncode == 0, rendered.stderr
    assert json.loads(env(rendered.stdout, "-controller", "controller")["AGENTIHOOKS_CONTROL_SCRATCH_SWARMS"]) == [
        SCRATCH
    ]


def test_no_scratch_swarm_sets_nothing_on_the_controller():
    rendered = template()

    assert rendered.returncode == 0, rendered.stderr
    assert "AGENTIHOOKS_CONTROL_SCRATCH_SWARMS" not in env(rendered.stdout, "-controller", "controller")


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"controller": {"scratchSwarms": [{**SCRATCH, "swarm": "rig-grade-swarm"}]}}, "proof-<code>-<task>-<n>"),
        ({"controller": {"scratchSwarms": [{**SCRATCH, "repo": ""}]}}, "needs swarm, agent, task and repo"),
        (
            {"controller": {"scratchSwarms": [SCRATCH], "hostSwarms": [SCRATCH["swarm"]]}},
            "must not be in controller.hostSwarms",
        ),
    ],
)
def test_a_scratch_swarm_the_controller_cannot_serve_is_refused_at_render(values, reason):
    rendered = subprocess.run(
        ["helm", "template", "fx", str(CHART), *HIVE, "-f", "-"],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
    )

    assert rendered.returncode != 0
    assert reason in rendered.stderr


def test_a_hive_names_the_swarms_whose_masters_it_takes():
    rendered = template("--set-json", f'workers.masterSwarms=["{SCRATCH["swarm"]}","proof-323133-t1199-2"]')

    assert rendered.returncode == 0, rendered.stderr
    hive = env(rendered.stdout, "-worker-hive", "worker-hive")
    assert hive["HIVE_MASTER_SWARMS"] == f"{SCRATCH['swarm']},proof-323133-t1199-2"
    assert hive["HIVE_EXCLUDED"] == "master"


def test_a_hive_names_no_master_swarm_by_default():
    rendered = template()

    assert rendered.returncode == 0, rendered.stderr
    assert env(rendered.stdout, "-worker-hive", "worker-hive")["HIVE_MASTER_SWARMS"] == ""


def test_the_hive_start_registers_the_charted_master_swarms():
    start = (CHART.parents[1] / "worker" / "start.sh").read_text()

    assert '"master-swarms=${HIVE_MASTER_SWARMS-}"' in start
