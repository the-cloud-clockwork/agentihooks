import json
import subprocess

import pytest

from scripts.gates import catalog, feature
from scripts.gates import log as gate_log
from scripts.swarm import cli
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm_ledger import ledger_kinds
from tests.swarm.test_cli import env, run  # noqa: F401

NAME = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/9"
MAPPED = {"features": [{"id": "f1", "tests": ["tests/test_a.py::test_a"]}, {"id": "bare", "tests": []}]}
MISSING = "this task is done only with the feature it proved: give --feature <id in feature-map.json>"
UNKNOWN = "feature nope is not in feature-map.json on dev or the merged pull request head; map it there first"
UNTESTED = "feature bare lists no test in feature-map.json; map its tests first"
UNREAD = "could not read feature-map.json from GitHub; run swarm done again when it answers"


def test_feature_is_a_proof_key_and_a_contract_flag_but_never_a_need():
    assert "feature" in ledger_kinds.PROOF_KEYS
    assert "feature" in ledger_kinds.CONTRACT_FLAGS
    assert all("feature" not in needs for needs in ledger_kinds.NEEDS.values())
    ledger_kinds.check({"contract": {"feature": "yes"}, "proof": {"feature": "f1"}})
    with pytest.raises(ValueError, match="contract feature must be yes or no"):
        ledger_kinds.check({"contract": {"feature": "maybe"}})


@pytest.mark.parametrize(
    ("task", "needed"),
    [
        ({"kind": "code"}, False),
        ({"kind": "research"}, False),
        ({"kind": "code", "contract": {"feature": "no"}}, False),
        ({"kind": "code", "contract": {"feature": "yes"}}, True),
        ({"kind": "ci", "contract": {"after": "yes"}}, True),
        ({"kind": "troubleshoot"}, True),
        ({"kind": "ops"}, True),
        ({"kind": "tune"}, True),
    ],
)
def test_the_feature_is_required_by_its_flag_a_live_after_proof_or_a_live_kind(task, needed):
    assert feature.required(task) is needed


def maps(*found):
    seen = []

    def read():
        seen.append(True)
        return list(found)

    read.seen = seen
    return read


def test_a_task_that_needs_no_feature_never_reads_the_map():
    read = maps(MAPPED)
    assert feature.refusal({"kind": "code"}, "", read) == ""
    assert read.seen == []


def test_a_required_feature_must_be_given():
    read = maps(MAPPED)
    assert feature.refusal({"kind": "ops"}, "", read) == MISSING
    assert feature.refusal({"kind": "ops"}, "  ", read) == MISSING
    assert read.seen == []


def test_an_unknown_feature_id_is_refused():
    assert feature.refusal({"kind": "ops"}, "nope", maps(MAPPED, {})) == UNKNOWN


def test_an_unread_map_is_refused_as_a_read_failure_never_as_an_unknown_id():
    assert feature.refusal({"kind": "ops"}, "nope", maps(MAPPED, None)) == UNREAD
    assert feature.refusal({"kind": "ops"}, "bare", maps(None, MAPPED)) == UNREAD
    assert feature.refusal({"kind": "ops"}, "f1", maps(None, MAPPED)) == ""


def test_an_entry_without_tests_is_refused():
    assert feature.refusal({"kind": "ops"}, "bare", maps(MAPPED)) == UNTESTED
    assert feature.refusal({"kind": "ops"}, "bare", maps({"features": [{"id": "bare"}]})) == UNTESTED


def test_a_mapped_feature_with_tests_is_accepted_from_either_map():
    assert feature.refusal({"kind": "ops"}, "f1", maps(MAPPED)) == ""
    assert feature.refusal({"kind": "ops"}, "f1", maps({}, MAPPED)) == ""
    assert feature.refusal({"kind": "ops"}, " f1 ", maps({"features": "x"}, MAPPED)) == ""


def test_the_maps_are_read_from_dev_and_the_merged_head():
    assert feature.refs("") == ["dev"]
    assert feature.refs("abc") == ["dev", "abc"]


def test_read_map_asks_github_for_the_raw_map_at_a_ref():
    calls = []

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(MAPPED), "")

    assert feature.read_map("dev", run=fake) == MAPPED
    assert calls == [
        (
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                "repos/the-cloud-clockwork/agentihooks/contents/feature-map.json?ref=dev",
            ],
            {"capture_output": True, "text": True, "timeout": 20},
        )
    ]


@pytest.mark.parametrize(
    "done",
    [
        subprocess.CompletedProcess([], 1, json.dumps(MAPPED), "not found"),
        subprocess.CompletedProcess([], 0, "not json", ""),
        subprocess.CompletedProcess([], 0, "[1]", ""),
    ],
)
def test_an_unreadable_map_reads_as_none(done):
    assert feature.read_map("dev", run=lambda argv, **kwargs: done) is None


def test_a_failed_github_call_reads_as_none():
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 20)

    assert feature.read_map("dev", run=boom) is None


def test_the_feature_gate_is_a_swarm_gate_that_defaults_to_log_only():
    assert catalog.defaults()["feature"] == "observe"
    assert cli.GATE_KEYS["feature-gate"] == "feature"


@pytest.fixture
def ops(env, monkeypatch, tmp_path):  # noqa: F811
    store, ledger, _ = env
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    read = []
    monkeypatch.setattr(feature, "read_map", lambda ref: read.append(ref) or MAPPED)
    ledger.rows["t1"].update(kind="ops", pr_url=URL)
    ledger.pulls[URL] = PullRequest("MERGED", 1, 1, False, head="h1")
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", NAME)
    return store, ledger, read, tmp_path


PROOF = ("--command", "probe", "--output", "ok")


def enforce(store):
    store.update("sw", gates={**store.config("sw").gates, "feature": "enforce"})


@pytest.mark.parametrize(
    ("given", "refused"), [((), MISSING), (("--feature", "nope"), UNKNOWN), (("--feature", "bare"), UNTESTED)]
)
def test_an_enforced_gate_refuses_done_without_a_mapped_tested_feature(ops, capsys, given, refused):
    store, ledger, _, tmp_path = ops
    enforce(store)
    before = dict(ledger.rows["t1"])
    capsys.readouterr()
    assert run("sw", "done", *PROOF, *given) == 1
    assert capsys.readouterr().err == f"swarm: {refused}\n"
    assert ledger.rows["t1"] == before
    rows = gate_log.recent("sw", None, tmp_path)
    assert [(row["gate"], row["kind"], row["agent"], row["tool"], row["reason"]) for row in rows] == [
        ("feature", "deny", NAME, "done", refused)
    ]


def test_an_enforced_gate_closes_a_task_with_a_mapped_feature(ops):
    store, ledger, read, tmp_path = ops
    enforce(store)
    assert run("sw", "done", *PROOF, "--feature", "f1") == 0
    assert ledger.rows["t1"]["state"] == "done"
    assert ledger.rows["t1"]["proof"]["feature"] == "f1"
    assert read == ["dev", "h1"]
    assert gate_log.recent("sw", None, tmp_path) == []


def test_swarm_set_switches_the_feature_gate(env):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    assert run("sw", "set", "feature-gate=enforce") == 0
    assert store.config("sw").gates["feature"] == "enforce"
    assert run("sw", "set", "feature-gate=observe") == 0
    assert store.config("sw").gates["feature"] == "observe"


def test_an_observed_gate_logs_the_refusal_and_closes_the_task(ops):
    _, ledger, _, tmp_path = ops
    assert run("sw", "done", *PROOF) == 0
    assert ledger.rows["t1"]["state"] == "done"
    rows = gate_log.recent("sw", None, tmp_path)
    assert [(row["gate"], row["kind"], row["task"], row["reason"]) for row in rows] == [
        ("feature", "observe", "t1", MISSING)
    ]


def test_a_skipped_gate_neither_logs_nor_refuses(ops):
    store, ledger, read, tmp_path = ops
    store.update("sw", gates={"feature": "off"})
    assert run("sw", "done", *PROOF, "--feature", "nope") == 0
    assert read == []
    assert ledger.rows["t1"]["state"] == "done"
    assert gate_log.recent("sw", None, tmp_path) == []


def test_a_task_without_a_pull_request_reads_only_the_dev_map(ops):
    store, ledger, read, tmp_path = ops
    enforce(store)
    ledger.rows["t1"]["pr_url"] = ""
    assert run("sw", "done", *PROOF, "--feature", "f1") == 0
    assert read == ["dev"]
    assert ledger.rows["t1"]["state"] == "done"
