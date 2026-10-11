import copy
import json
from pathlib import Path

import pytest

from scripts.swarm_v2 import fleet_canary, fleet_view
from scripts.swarm_v2 import fleet_dashboard as fleet

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
CANARY = FIXTURES / "scaling-canary-sv2-cap-02.json"
OVERLAY = FIXTURES / "fleet-canary-overlay.json"
RECORD = json.loads(CANARY.read_text())
PRESSURE = json.loads(OVERLAY.read_text())
FOUND = fleet.definition()
ON: dict[str, str] = {}
SWARM = fleet.viewer({"actor": "swarm viewer", "brains": ["swarm-anton"]})
RUNS = [run["run"] for run in RECORD["runs"]]


def first_fired(trace, found=FOUND):
    fired = {}
    for sample in trace:
        for alert in fleet.evaluate(sample, found, ON)["alerts"]:
            fired.setdefault(f"{alert['rule']}:{alert['subject']}", sample["seq"])
    return fired


def test_a_stamp_reads_as_epoch_milliseconds():
    assert fleet_canary.stamp_ms("2026-10-11T16:20:31Z") == 1791735631000


def test_each_recorded_run_is_its_own_trace():
    traces = fleet_canary.record_samples(RECORD, PRESSURE)
    assert list(traces) == RUNS
    assert [len(trace) for trace in traces.values()] == [6, 6]
    assert [sample["seq"] for sample in traces[RUNS[0]]] == [1, 2, 3, 4, 5, 6]


def test_a_recorded_run_moves_from_pending_to_active_as_its_node_joins():
    trace = fleet_canary.run_samples(RECORD["runs"][0], PRESSURE)
    created = trace[0]["at_ms"]
    assert [sample["at_ms"] - created for sample in trace] == [0, 6000, 11000, 71000, 148000, 155000]
    assert [sample["attempts"][0]["phase"] for sample in trace] == ["pending"] * 5 + ["active"]
    assert [sample["pools"][0]["nodes"] for sample in trace] == [0, 0, 0, 1, 1, 1]
    assert {sample["pools"][0]["configured_maximum"] for sample in trace} == {4}
    assert trace[0]["attempts"][0] == {
        "execution_id": RUNS[0],
        "seat": "canary",
        "task": "scaling-canary",
        "phase": "pending",
        "pool": "anton-k3s-swarm",
        "permitted_pools": ["anton-k3s-swarm"],
        "pending_since_ms": created,
        "protected": False,
        "idle_since_ms": None,
        "terminal": "unknown",
        "authority": "unknown",
    }
    assert "requested" not in trace[0]["attempts"][0] and "template" not in trace[0]["pools"][0]


def test_samples_carry_the_pressure_readings_and_count_from_the_first_seq():
    trace = fleet_canary.run_samples(RECORD["runs"][1], PRESSURE, first_seq=10)
    assert trace[0]["seq"] == 10
    assert {key: trace[0][key] for key in fleet_canary.OVERLAY_KEYS} == {
        key: PRESSURE[key] for key in fleet_canary.OVERLAY_KEYS
    }
    assert "schema_version" not in trace[0]


@pytest.mark.parametrize("run", RUNS)
def test_a_recorded_scale_up_never_alerts_impossible_pending(run):
    trace = fleet_canary.record_samples(RECORD, PRESSURE)[run]
    pending_ms = trace[-1]["at_ms"] - trace[0]["at_ms"]
    assert pending_ms > fleet.rules(FOUND)[1].for_ms
    assert first_fired(trace) == {"cross_scope_denial:deny-anton-1": 1}


@pytest.mark.parametrize(("run", "seq"), [(RUNS[0], 5), (RUNS[1], 5)])
def test_the_same_recorded_run_at_its_pool_maximum_alerts_once_the_window_passes(run, seq):
    record = copy.deepcopy(RECORD)
    for recorded in record["runs"]:
        recorded["classification"]["group"]["max"] = 0
    trace = fleet_canary.record_samples(record, PRESSURE)[run]
    assert first_fired(trace)[f"impossible_pending:{run}"] == seq
    alert = next(a for a in fleet.evaluate(trace[seq - 1], FOUND, ON)["alerts"] if a["rule"] == "impossible_pending")
    assert alert["facts"]["reason"] == "permitted_pools_at_configured_maximum"


def test_a_known_shape_still_decides_the_template_fit():
    trace = fleet_canary.run_samples(RECORD["runs"][0], PRESSURE)
    sample = copy.deepcopy(trace[4])
    sample["attempts"][0]["requested"] = {"cpu_millis": 9000, "memory_mib": 1}
    sample["pools"][0]["template"] = {"cpu_millis": 8000, "memory_mib": 16384}
    alert = next(a for a in fleet.evaluate(sample, FOUND, ON)["alerts"] if a["rule"] == "impossible_pending")
    assert alert["facts"]["reason"] == "no_permitted_template_fits"
    sample["attempts"][0]["requested"] = None
    assert all(a["rule"] != "impossible_pending" for a in fleet.evaluate(sample, FOUND, ON)["alerts"])


def test_the_recorded_dashboard_shows_capacity_pressure_and_freshness():
    trace = fleet_canary.run_samples(RECORD["runs"][0], PRESSURE)
    panels = fleet.dashboard(trace[-1], SWARM, FOUND)["panels"]
    capacity = panels["capacity"][0]
    assert capacity["pool"] == "anton-k3s-swarm"
    assert [capacity["series"][key]["value"] for key in capacity["series"]] == [4, 1, 1, 0]
    assert panels["checkpoint_age"]["stale"]["value"] == 1
    assert panels["quota_wait"]["waiting_accounts"]["value"] == 1
    assert panels["knowledge"]["denials"][0]["session_title"] == fleet.WITHHELD


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def test_the_entrypoint_renders_each_recorded_run(tmp_path, capsys):
    grant = write(tmp_path, "grant.json", {"actor": "operator", "brains": ["swarm-anton"]})
    argv = ["--canary", str(CANARY), "--overlay", str(OVERLAY), "--grant", str(grant)]
    assert fleet_view.main(argv, ON) == 0
    views = json.loads(capsys.readouterr().out)
    assert list(views) == sorted(RUNS)
    view = views[RUNS[0]]
    assert [entry["alerts"] for entry in view["timeline"]] == [["cross_scope_denial:deny-anton-1"]] * 6
    assert view["dashboard"]["viewer"] == "operator"
    assert view["alert_actionability_review"]["unactionable"] == []
    assert "Private personal" not in json.dumps(views)


def test_the_entrypoint_renders_a_samples_file(tmp_path, capsys):
    samples = write(tmp_path, "trace.json", fleet_canary.run_samples(RECORD["runs"][0], PRESSURE))
    grant = write(tmp_path, "grant.json", {"brains": ["personal-operator"]})
    assert fleet_view.main(["--samples", str(samples), "--grant", str(grant)], ON) == 0
    view = json.loads(capsys.readouterr().out)["trace"]
    assert (
        view["dashboard"]["panels"]["knowledge"]["denials"][0]["session_title"] == "Private personal planning session"
    )
    assert view["timeline"][-1]["seq"] == 6


def test_the_entrypoint_honours_the_alert_switch(tmp_path, capsys):
    grant = write(tmp_path, "grant.json", {"brains": []})
    argv = ["--canary", str(CANARY), "--overlay", str(OVERLAY), "--grant", str(grant)]
    assert fleet_view.main(argv, {fleet.ALERTS_ENV: "off"}) == 0
    view = json.loads(capsys.readouterr().out)[RUNS[0]]
    assert {tuple(entry["alerts"]) for entry in view["timeline"]} == {()}
    assert view["alert_actionability_review"]["state"] == "disabled"
    assert view["dashboard"]["panels"]["attempts"]["active"]["value"] == 1


def test_the_entrypoint_refuses_a_grant_without_brains(tmp_path, capsys):
    grant = write(tmp_path, "grant.json", {"actor": "operator", "label": "operator"})
    argv = ["--canary", str(CANARY), "--overlay", str(OVERLAY), "--grant", str(grant)]
    assert fleet_view.main(argv, ON) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "fleet view refused: a viewer grant names its brains as a list of brain ids\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["--grant", "g.json"],
        ["--canary", "c.json", "--grant", "g.json"],
        ["--canary", "c.json", "--samples", "s.json", "--grant", "g.json"],
    ],
)
def test_the_entrypoint_needs_one_source_and_an_overlay_for_a_canary(argv, capsys):
    with pytest.raises(SystemExit) as exited:
        fleet_view.main(argv, ON)
    assert exited.value.code == 2
    assert "usage: python -m scripts.swarm_v2.fleet_view" in capsys.readouterr().err


def test_the_entrypoint_takes_the_definition_it_is_given(tmp_path, capsys):
    found = copy.deepcopy(FOUND)
    found["alerts"][2]["for_ms"] = 10**9
    definition = write(tmp_path, "definition.json", found)
    grant = write(tmp_path, "grant.json", {"brains": []})
    argv = ["--canary", str(CANARY), "--overlay", str(OVERLAY), "--grant", str(grant), "--definition", str(definition)]
    assert fleet_view.main(argv, ON) == 0
    view = json.loads(capsys.readouterr().out)[RUNS[0]]
    assert {tuple(entry["alerts"]) for entry in view["timeline"]} == {()}
