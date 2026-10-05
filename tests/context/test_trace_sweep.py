"""Trace sweep: find a corrected directive in every layer, clear the runtime ones, plan the rest, close when clean."""

import json
import subprocess
from unittest.mock import patch

import pytest

from hooks.context import conditions, injection_trace, trace_sweep
from scripts.trace_cli import main as trace

SID = "sess-sweep-1"
TEXT = "Never run the test suite on this machine, the shared runner owns it"
COND = "pre-bash-no_tests.sh"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "dev")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    return path


def _commit(repo):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")


def _store(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enforcements": list(entries)}))
    return path


def _entry(eid, message=TEXT):
    return {"id": eid, "type": "message", "message": message, "cadence": 5}


def _condition(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / COND
    path.write_text(f"#!/usr/bin/env bash\necho '{TEXT}'\n")
    return path


@pytest.fixture()
def layers(tmp_path):
    from hooks import config

    bundle = _git_repo(tmp_path / "bundle")
    bundle_store = _store(bundle / "enforcements.json", _entry("bad-1"), _entry("keep-1", "unrelated rule"))
    profile_store = _store(bundle / "profiles" / "anton" / "enforcements.json", _entry("p-bad"))
    _commit(bundle)

    runtime = _store(tmp_path / "home-store" / "enforcements.json", _entry("r-bad"), _entry("r-keep", "other"))

    dev = tmp_path / "dev"
    tracked_repo = _git_repo(dev / "tracked")
    tracked_store = _store(tracked_repo / ".agentihooks" / "enforcements.json", _entry("t-bad"))
    _commit(tracked_repo)
    loose_repo = _git_repo(dev / "loose")
    _git(loose_repo, "commit", "-q", "--allow-empty", "-m", "seed")
    loose_store = _store(loose_repo / ".agentihooks" / "enforcements.json", _entry("l-bad"))
    loose_condition = _condition(loose_repo / ".agentihooks" / "conditions")
    runtime_condition = _condition(config.AGENTIHOOKS_HOME / "conditions")

    broadcasts = tmp_path / "broadcast.json"
    stamp = {"created_at": "2026-10-05T00:00:00Z", "ttl_seconds": 3600, "expires_at": "2999-01-01T00:00:00Z"}
    broadcasts.write_text(
        json.dumps(
            [
                {"id": "bc-op", "message": TEXT, "severity": "alert", "source": "operator", **stamp},
                {"id": "bc-other", "message": "deploy freeze", "severity": "alert", "source": "operator", **stamp},
                {
                    "id": "bc-brain",
                    "message": f"[Operator Intent]\n{TEXT}",
                    "severity": "info",
                    "source": "brain-adapter",
                    "channel": "brain",
                    "origin": {"id": "operator-intent", "file": "http://brain/feed"},
                    **stamp,
                },
            ]
        )
    )

    with (
        patch("hooks.context.enforcement._store_path", return_value=runtime),
        patch("hooks.context.enforcement._get_bundle_path", return_value=bundle),
        patch("hooks.context.enforcement._get_active_profile", return_value="anton"),
        patch("hooks.context.enforcement._get_linked_profiles", return_value={}),
        patch("hooks.context.conditions.profile_chain.read_state", return_value={}),
        patch("hooks.context.broadcast._broadcast_path", return_value=broadcasts),
    ):
        yield {
            "dev": dev,
            "bundle": bundle,
            "bundle_store": bundle_store,
            "profile_store": profile_store,
            "runtime": runtime,
            "tracked_repo": tracked_repo,
            "tracked_store": tracked_store,
            "loose_store": loose_store,
            "loose_condition": loose_condition,
            "runtime_condition": runtime_condition,
            "broadcasts": broadcasts,
        }


def _correct(source="bad-1", text=TEXT, at="2026-10-05T10:00:00Z"):
    row = {
        "at": at,
        "session": SID,
        "layer": "bundle",
        "source": source,
        "locator": {},
        "text": text,
        "repo": "/repos/agentihooks",
        "reason": "qitp rule leaked here",
    }
    path = injection_trace._corrections_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def _plan(layers):
    return {(hit.layer, hit.location, hit.action) for hit in trace_sweep.find(_correct(), root=layers["dev"])}


def test_a_planted_directive_is_found_in_every_layer(layers):
    plan = _plan(layers)

    assert plan == {
        ("enforcement", str(layers["bundle_store"]), "pr"),
        ("enforcement", str(layers["profile_store"]), "pr"),
        ("enforcement", str(layers["runtime"]), "clear"),
        ("enforcement", str(layers["tracked_store"]), "pr"),
        ("enforcement", str(layers["loose_store"]), "clear"),
        ("condition", str(layers["loose_condition"]), "clear"),
        ("condition", str(layers["runtime_condition"]), "clear"),
        ("broadcast", "bc-op", "clear"),
        ("brain", "operator-intent", "followup"),
    }


def _apply(layers, filed=None):
    filed = [] if filed is None else filed
    with patch.object(trace_sweep, "_file_followup", side_effect=lambda ledger, text: filed.append(text)):
        return trace_sweep.sweep(root=layers["dev"], apply=True, session_id=SID, ledger="rig")


def _ids(path):
    return {e["id"] for e in json.loads(path.read_text())["enforcements"]}


def test_apply_clears_the_runtime_layers_and_files_a_brain_followup(layers):
    conditions.arm_gate(SID)
    _correct()
    filed = []

    _apply(layers, filed)

    assert _ids(layers["runtime"]) == {"r-keep"}
    assert _ids(layers["loose_store"]) == set()
    assert not layers["loose_condition"].exists()
    assert not layers["runtime_condition"].exists()
    remaining = {m["id"] for m in json.loads(layers["broadcasts"].read_text())}
    assert remaining == {"bc-other", "bc-brain"}
    assert len(filed) == 1 and "operator-intent" in filed[0]


def test_a_condition_stays_while_the_operator_has_not_armed_the_gate(layers):
    _correct()

    report = _apply(layers)

    assert layers["runtime_condition"].exists()
    outcomes = [outcome for hit, outcome in report["applied"] if hit.location == str(layers["runtime_condition"])]
    assert outcomes and "refused" in outcomes[0]


def test_git_layers_get_a_pull_request_plan_never_an_edit(layers):
    conditions.arm_gate(SID)
    _correct()
    before = {key: layers[key].read_bytes() for key in ("bundle_store", "profile_store", "tracked_store")}

    report = _apply(layers)

    assert {key: layers[key].read_bytes() for key in before} == before
    for repo in (layers["bundle"], layers["tracked_repo"]):
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
        assert status == ""
    planned = [outcome for hit, outcome in report["applied"] if hit.action == "pr"]
    assert len(planned) == 3 and all("pull request" in outcome for outcome in planned)
    assert any(str(layers["tracked_repo"]) in outcome for outcome in planned)


def test_a_correction_stays_open_while_a_sweep_still_finds_it(layers):
    conditions.arm_gate(SID)
    _correct()

    report = _apply(layers)

    assert report["closed"] == []
    assert len(trace_sweep.open_corrections()) == 1


def test_a_clean_resweep_closes_the_correction(layers):
    conditions.arm_gate(SID)
    _correct(source="r-bad", text="")

    report = trace_sweep.sweep(root=layers["dev"], apply=True, session_id=SID)

    assert [row["source"] for row in report["closed"]] == ["r-bad"]
    assert _ids(layers["runtime"]) == {"r-keep"}
    assert trace_sweep.open_corrections() == []
    assert trace_sweep.sweep(root=layers["dev"])["plan"] == []


def test_the_cli_prints_the_sweep_plan(layers, capsys):
    _correct()

    assert trace(["sweep", "--root", str(layers["dev"])]) == 0

    out = capsys.readouterr().out
    assert f"bad-1\tenforcement\tpr\t{layers['tracked_store']}" in out
    assert "bad-1\tbrain\tfollowup\toperator-intent" in out
    assert _ids(layers["runtime"]) == {"r-bad", "r-keep"}


def test_a_correction_records_the_directive_text():
    injection_trace.record(SID, "enforcement", "x-1", TEXT, {"store": "/s", "id": "x-1"})

    row = injection_trace.correct(SID, "x-1", "/repo", "wrong here")

    assert row["text"] == TEXT
