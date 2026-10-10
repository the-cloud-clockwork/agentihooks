import copy
import json
import subprocess
from pathlib import Path

import pytest

from scripts.swarm_v2.herdr import capabilities
from scripts.swarm_v2.herdr.capabilities import Incompatible, Observation, Qualifier, Unreachable
from tests import sv2_hdr01_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-HDR-01"


def observed(server: dict | None = None, client: dict | None = None, methods=None) -> Observation:
    fx = cases.fixture()
    return Observation(
        client if client is not None else fx["client"],
        server if server is not None else fx["server"],
        frozenset(fx["methods"] if methods is None else methods),
    )


def test_the_pinned_pair_qualifies_every_one_window_operation():
    verdict = capabilities.qualify("t", "i", observed())
    assert verdict.compatible
    assert verdict.refusals == ()
    assert {name: verdict.matrix[name] for name in cases.ONE_WINDOW} == dict.fromkeys(cases.ONE_WINDOW, "supported")


def test_the_matrix_names_every_operation_and_every_unforwarded_command():
    table = capabilities.matrix(frozenset())
    assert table["pane_run"] == "unsupported: server lacks pane.send_input"
    assert table["update"] == "unsupported: local installation is not forwarded"
    assert table["server_replace"] == "unsupported: machine forwarding never installs, starts or restarts a server"
    assert table["session"] == "unsupported: session management is not forwarded"
    assert table["agent_attach"] == "unsupported: interactive attachment is not forwarded"
    assert table["local_fallback"] == "unsupported: a machine command never falls back to the local server"
    assert set(table) == set(capabilities.OPERATIONS) | set(capabilities.NOT_FORWARDED)


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda s, c: s["capabilities"].pop("surface_interest"), "herdr server lacks surface_interest=true"),
        (lambda s, c: s["capabilities"].update(health_check=False), "herdr server lacks health_check=true"),
        (lambda s, c: s["capabilities"].update(health_check=1), "herdr server lacks health_check=true"),
        (
            lambda s, c: s["capabilities"].update(detached_server_daemon=False),
            "herdr server lacks detached_server_daemon=true",
        ),
        (
            lambda s, c: s["capabilities"].update(endpoint_protocol_generation=2),
            "herdr server lacks endpoint_protocol_generation=1",
        ),
        (
            lambda s, c: s.update(version="0.9.2"),
            "herdr server 0.9.2 is not the client build 0.9.1, so its socket methods are unverified",
        ),
        (lambda s, c: s.update(running=False), "herdr server is not running"),
        (lambda s, c: s.pop("running"), "herdr server is not running"),
        (lambda s, c: s.update(protocol=21), "herdr protocol is not 22"),
        (lambda s, c: c.update(protocol=23), "herdr protocol is not 22"),
        (lambda s, c: c.update(remote_host_bridge=False), "herdr client lacks remote_host_bridge=true"),
        (lambda s, c: c.pop("endpoint_protocol_generation"), "herdr client lacks endpoint_protocol_generation=1"),
    ],
)
def test_each_incompatibility_names_its_refusal(change, refusal):
    fx = cases.fixture()
    server, client = copy.deepcopy(fx["server"]), copy.deepcopy(fx["client"])
    change(server, client)
    assert capabilities.refusals(observed(server, client)) == [refusal]


def test_a_server_without_capabilities_lacks_every_forwarding_capability():
    server = copy.deepcopy(cases.fixture()["server"])
    server.pop("capabilities")
    assert capabilities.refusals(observed(server)) == [
        "herdr server lacks endpoint_protocol_generation=1",
        "herdr server lacks surface_interest=true",
        "herdr server lacks health_check=true",
        "herdr server lacks detached_server_daemon=true",
    ]


def test_a_failed_probe_keeps_the_current_incarnation():
    replies = [observed()]

    def probe(target):
        if not replies:
            raise Unreachable("down")
        return replies.pop()

    gate = Qualifier(probe)
    gate.verdict("t", "one")
    with pytest.raises(Unreachable):
        gate.verdict("t", "two")
    assert gate.verdicts["t"].incarnation == "one"
    assert gate.retired == {}
    assert gate.require("t", "one", "pane_read")[:3] == ["herdr", "--machine", "t"]


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["herdr"], 30), FileNotFoundError("herdr")])
def test_a_stalled_or_missing_herdr_is_unreachable(failure):
    def run(command):
        raise failure

    with pytest.raises(Unreachable, match=rf"^herdr --machine t status: {type(failure).__name__}$"):
        capabilities.machine_probe(run)("t")


def test_a_server_without_a_method_disables_only_that_operation():
    methods = set(cases.fixture()["methods"]) - {"pane.send_input"}
    gate = Qualifier(lambda target: observed(methods=methods))
    with pytest.raises(Incompatible, match=r"^pane_run unsupported: server lacks pane\.send_input$"):
        gate.require("t", "i", "pane_run")
    assert gate.require("t", "i", "pane_read") == ["herdr", "--machine", "t", "pane", "read"]


def test_require_builds_a_machine_command_for_each_operation():
    gate = Qualifier(lambda target: observed())
    for name, (cli, _) in capabilities.OPERATIONS.items():
        assert gate.require("worker", "i", name) == ["herdr", "--machine", "worker", *cli]


@pytest.mark.parametrize(("target", "incarnation"), [("", "i"), ("t", ""), ("--session", "i")])
def test_an_unnamed_target_or_incarnation_is_refused_before_any_probe(target, incarnation):
    probes: list[str] = []
    gate = Qualifier(lambda name: probes.append(name) or observed())
    with pytest.raises(Incompatible, match="needs a target and a confirmed server incarnation"):
        gate.require(target, incarnation, "pane_read")
    assert probes == []


def test_an_unknown_operation_is_refused_and_counted():
    gate = Qualifier(lambda target: observed())
    with pytest.raises(Incompatible, match=r"^attach_tui unsupported: unknown operation$"):
        gate.require("t", "i", "attach_tui")
    assert gate.mismatches == {("t", "attach_tui unsupported: unknown operation"): 1}


def test_a_refusal_counts_each_reason_per_target():
    server = copy.deepcopy(cases.fixture()["missing_forwarding_server"])
    server["capabilities"]["health_check"] = False
    gate = Qualifier(lambda target: observed(server))
    for _ in range(2):
        with pytest.raises(Incompatible) as refused:
            gate.require("t", "i", "update")
    assert str(refused.value) == (
        "herdr server lacks surface_interest=true; herdr server lacks health_check=true; "
        "update unsupported: local installation is not forwarded"
    )
    assert gate.herdr_capability_mismatch_total() == 6
    assert gate.mismatches[("t", "herdr server lacks surface_interest=true")] == 2


def test_the_same_incarnation_reuses_its_verdict_and_a_new_one_rechecks():
    probes: list[str] = []
    gate = Qualifier(lambda target: probes.append(target) or observed())
    first = gate.verdict("t", "one")
    assert gate.verdict("t", "one") is first
    second = gate.verdict("t", "two")
    assert second is not first
    assert second.incarnation == "two"
    assert probes == ["t", "t"]


def test_a_replaced_incarnation_cannot_return():
    gate = Qualifier(lambda target: observed())
    gate.verdict("t", "one")
    gate.verdict("t", "two")
    with pytest.raises(Incompatible, match="^herdr server incarnation one on t was replaced$"):
        gate.require("t", "one", "pane_read")
    assert gate.verdicts["t"].incarnation == "two"
    assert gate.herdr_capability_mismatch_total() == 0


def test_targets_keep_separate_verdicts():
    servers = {"good": cases.fixture()["server"], "bad": cases.fixture()["missing_forwarding_server"]}
    gate = Qualifier(lambda target: observed(servers[target]))
    assert gate.require("good", "i", "pane_read")[:3] == ["herdr", "--machine", "good"]
    with pytest.raises(Incompatible):
        gate.require("bad", "i", "pane_read")
    assert gate.verdicts["good"].compatible and not gate.verdicts["bad"].compatible


def test_the_machine_probe_reads_forwarded_server_status_and_local_schema():
    runner = cases.Runner(cases.fixture()["server"])
    found = capabilities.machine_probe(runner)(cases.TARGET)
    assert runner.commands == [
        ["herdr", "--machine", cases.TARGET, "status", "server", "--json"],
        ["herdr", "status", "client", "--json"],
        ["herdr", "api", "schema", "--json"],
    ]
    assert found == observed()


@pytest.mark.parametrize("field", ["version", "protocol"])
def test_the_local_schema_is_not_trusted_for_another_server_build(field):
    server = copy.deepcopy(cases.fixture()["server"])
    server[field] = "0.9.2" if field == "version" else 21
    runner = cases.Runner(server)
    found = capabilities.machine_probe(runner)(cases.TARGET)
    assert found.methods == frozenset()
    assert ["herdr", "api", "schema", "--json"] not in runner.commands


def test_an_unreachable_machine_raises_and_caches_nothing():
    runner = cases.Runner(cases.fixture()["server"])
    runner.server = None
    gate = Qualifier(capabilities.machine_probe(runner))
    with pytest.raises(Unreachable, match=r"^herdr --machine worker-fixture status: exit 2$"):
        gate.require(cases.TARGET, "i", "pane_read")
    assert gate.verdicts == {}
    assert runner.commands == [["herdr", "--machine", cases.TARGET, "status", "server", "--json"]]


def test_an_unreachable_reply_never_repeats_remote_output():
    def run(command):
        return subprocess.CompletedProcess(command, 255, "remote stdout text", "remote stderr text")

    with pytest.raises(Unreachable) as refused:
        capabilities.machine_probe(run)("t")
    assert str(refused.value) == "herdr --machine t status: exit 255"


def test_an_unreadable_reply_is_unreachable():
    def run(command):
        return subprocess.CompletedProcess(command, 0, "not json", "")

    with pytest.raises(Unreachable, match=r"^herdr --machine t status: unreadable reply$"):
        capabilities.machine_probe(run)("t")


def test_a_non_object_reply_reads_as_empty():
    def run(command):
        return subprocess.CompletedProcess(command, 0, "[]", "")

    found = capabilities.machine_probe(run)("t")
    assert found == Observation({}, {}, frozenset())
    assert capabilities.refusals(found)[0] == "herdr server is not running"


def test_schema_methods_skip_requests_without_a_method_name():
    schema = {"schemas": {"request": {"oneOf": [{"properties": {"method": {"const": "ping"}}}, {"properties": {}}]}}}
    assert capabilities._schema_methods(schema) == frozenset({"ping"})
    assert capabilities._schema_methods({}) == frozenset()


def test_the_runner_never_reads_stdin_and_is_bounded(monkeypatch):
    seen = {}

    def fake(command, **kwargs):
        seen.update(kwargs, command=command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(capabilities.subprocess, "run", fake)
    capabilities.runner({"HOME": "/h"})(["herdr", "status"])
    assert seen == {
        "command": ["herdr", "status"],
        "env": {"HOME": "/h"},
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "timeout": capabilities.PROBE_SECONDS,
    }


def test_the_fixture_is_the_pinned_capture():
    fx = cases.fixture()
    lock = json.loads((Path(__file__).parents[1] / "docker" / "swarm-node" / "versions.lock").read_text())
    assert fx["client"]["version"] == fx["server"]["version"] == lock["tools"]["herdr"]["version"]
    assert lock["tools"]["herdr"]["sha256"] in fx["source"]
    assert fx["schema_protocol"] == capabilities.PROTOCOL
    assert "surface_interest" not in fx["missing_forwarding_server"]["capabilities"]
    assert fx["session_member_server"]["capabilities"]["detached_server_daemon"] is False


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
