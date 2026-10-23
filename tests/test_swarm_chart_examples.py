from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "agentihooks-swarm"
EXAMPLES = CHART / "examples"
NAMES = ["hub.yaml", "hive-only.yaml", "external-nats.yaml"]


def example(name: str) -> dict:
    return yaml.safe_load((EXAMPLES / name).read_text())


def undeclared(given: dict, declared: dict, prefix: str = "") -> list[str]:
    found = []
    for key, value in given.items():
        path = f"{prefix}{key}"
        if key not in declared:
            found.append(path)
        elif isinstance(value, dict) and isinstance(declared[key], dict) and declared[key]:
            found += undeclared(value, declared[key], f"{path}.")
    return found


def test_the_chart_ships_a_hub_a_hive_only_and_an_external_nats_example():
    assert sorted(path.name for path in EXAMPLES.iterdir()) == sorted(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_every_key_an_example_sets_is_a_chart_value(name):
    assert undeclared(example(name), yaml.safe_load((CHART / "values.yaml").read_text())) == []


def test_an_undeclared_key_is_named_by_its_full_path():
    declared = {"hub": {"enabled": False, "bus": {"url": ""}, "env": {}}, "hiveOnly": False}
    given = {"hub": {"bus": {"adress": "x"}, "env": {"ANY": "1"}}, "hiveonly": True}

    assert undeclared(given, declared) == ["hub.bus.adress", "hiveonly"]


def test_the_hub_example_runs_the_hub_on_the_bundled_nats():
    values = example("hub.yaml")

    assert values["hub"]["enabled"] is True
    assert values["nats"]["enabled"] is True
    assert values["nats"]["config"]["jetstream"]["enabled"] is True
    assert "url" not in values["hub"].get("bus", {})
    assert values.get("hiveOnly", False) is False


def test_the_hive_only_example_renders_one_hive_without_nats():
    values = example("hive-only.yaml")

    assert values["hiveOnly"] is True
    assert values["nats"]["enabled"] is False
    assert values["workers"]["replicas"] == 1
    assert values["workers"]["credentialSecret"]["name"]
    assert values["workers"]["credentialSecret"]["key"]


def test_the_external_nats_example_points_the_hub_at_its_own_nats():
    values = example("external-nats.yaml")

    assert values["hub"]["enabled"] is True
    assert values["nats"]["enabled"] is False
    assert values["hub"]["bus"]["url"].startswith("nats://")
    assert values.get("hiveOnly", False) is False


def test_the_kind_proof_renders_every_example_after_building_the_dependency():
    script = (CHART / "ci" / "kind-smoke.sh").read_text()
    render = script.index('python3 tests/chart_examples.py "$chart"')

    assert script.index('helm dependency build "$chart"') < render
    assert render < script.index("finish() {")
