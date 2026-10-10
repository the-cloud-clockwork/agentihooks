import copy
import json

import pytest
import scripts.swarm_v2.image_attestation as attestation

pytestmark = pytest.mark.unit
COMMIT = "a" * 40
IMAGE_ID = "sha256:" + "1" * 64
DIGEST = "sha256:" + "2" * 64
TOOLS = {"herdr": "herdr 0.9.1", "claude": "2.1.295 (Claude Code)", "codex": "codex-cli 0.162.0"}
MANIFEST = {"source_revision": COMMIT, "observed": {"tools": TOOLS}}
STATUS = {
    "running": True,
    "version": "0.9.1",
    "protocol": 22,
    "capabilities": {"live_handoff": True, "detached_server_daemon": True, "health_check": True},
}
VALID = {
    "manifest": MANIFEST,
    "observed": {
        "herdr": {
            "version": "herdr 0.9.1",
            "status": STATUS,
            "schema": {"protocol": 22, "methods": sorted(attestation.HERDR_METHODS | {"ping"})},
        },
        "claude": {"version": "2.1.295 (Claude Code)", "hook_registrations": 1},
        "codex": {"version": "codex-cli 0.162.0", "hook_registrations": 1},
    },
}
ORIGIN = {"commit": COMMIT, "repository": "the-cloud-clockwork/agentihooks", "run_id": "7"}


def incompatible():
    probe = copy.deepcopy(VALID)
    status = probe["observed"]["herdr"]["status"]
    status["protocol"] = 21
    status["capabilities"]["detached_server_daemon"] = False
    return probe


def test_the_valid_image_qualifies_every_target():
    report = attestation.qualify(MANIFEST, VALID["observed"])

    assert report["promotable"] is True
    assert report["worker_image_qualified_targets"] == 3
    assert {name: target["qualified"] for name, target in report["targets"].items()} == dict.fromkeys(
        attestation.TARGETS, True
    )
    assert report["targets"]["claude"] == {
        "pinned": "2.1.295 (Claude Code)",
        "observed": "2.1.295 (Claude Code)",
        "qualified": True,
        "refusals": [],
    }


def test_incompatible_herdr_server_capabilities_fail_only_the_herdr_target():
    report = attestation.qualify(MANIFEST, incompatible()["observed"])

    assert report["promotable"] is False
    assert report["worker_image_qualified_targets"] == 2
    assert report["targets"]["herdr"]["refusals"] == [
        "herdr protocol is not 22",
        "herdr server lacks detached_server_daemon",
    ]
    assert report["targets"]["codex"]["qualified"] is True


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda h: h["status"].update(running=False), "herdr headless server did not run"),
        (lambda h: h["schema"].update(protocol=23), "herdr protocol is not 22"),
        (lambda h: h["status"]["capabilities"].pop("health_check"), "herdr server lacks health_check"),
        (lambda h: h["status"]["capabilities"].update(health_check="yes"), "herdr server lacks health_check"),
        (
            lambda h: h["schema"].update(methods=sorted(set(h["schema"]["methods"]) - {"pane.read", "agent.list"})),
            "herdr socket API lacks agent.list, pane.read",
        ),
        (lambda h: h.pop("schema"), "herdr protocol is not 22"),
        (lambda h: h.update(version="herdr 0.9.2"), "herdr version 'herdr 0.9.2' is not pinned herdr 0.9.1"),
    ],
)
def test_each_herdr_incompatibility_names_its_refusal(change, refusal):
    observed = copy.deepcopy(VALID["observed"])
    change(observed["herdr"])

    report = attestation.qualify(MANIFEST, observed)

    assert refusal in report["targets"]["herdr"]["refusals"]
    assert report["promotable"] is False


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_harness_without_session_start_hook_delivery_is_refused(target):
    observed = copy.deepcopy(VALID["observed"])
    observed[target]["hook_registrations"] = 0

    report = attestation.qualify(MANIFEST, observed)

    assert report["targets"][target]["refusals"] == [f"{target} headless launch delivered no SessionStart hook"]
    assert report["worker_image_qualified_targets"] == 2


def test_a_missing_target_is_refused_and_not_counted():
    observed = copy.deepcopy(VALID["observed"])
    del observed["codex"]

    report = attestation.qualify(MANIFEST, observed)

    assert report["targets"]["codex"]["qualified"] is False
    assert "codex headless launch delivered no SessionStart hook" in report["targets"]["codex"]["refusals"]
    assert report["worker_image_qualified_targets"] == 2


def test_the_release_artifact_names_digest_manifest_report_and_commit_provenance():
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)

    assert made["package"] == "SV2-IMG-05"
    assert made["promotable"] is True and made["promoted"] is False
    assert made["image_id"] == IMAGE_ID and made["digest"] is None
    assert made["manifest"] == MANIFEST
    assert made["report"] == attestation.qualify(MANIFEST, VALID["observed"])
    assert made["provenance"] == ORIGIN
    assert made["compatibility"] == {
        "herdr_protocol": 22,
        "herdr_server_capabilities": ["detached_server_daemon", "health_check"],
        "herdr_methods": sorted(attestation.HERDR_METHODS),
        "targets": TOOLS,
    }
    assert made["refusals"] == []


def test_a_manifest_from_another_commit_is_not_promotable():
    probe = copy.deepcopy(VALID)
    probe["manifest"]["source_revision"] = "b" * 40

    made = attestation.attest(probe, IMAGE_ID, ORIGIN)

    assert made["promotable"] is False
    assert made["refusals"] == ["image manifest names another commit"]


def test_a_malformed_tested_image_id_is_not_promotable():
    made = attestation.attest(VALID, "1" * 64, ORIGIN)

    assert made["refusals"] == ["tested image id is not a sha256 digest"]


def test_attest_carries_every_target_refusal():
    made = attestation.attest(incompatible(), IMAGE_ID, ORIGIN)

    assert made["promotable"] is False
    assert made["refusals"] == ["herdr protocol is not 22", "herdr server lacks detached_server_daemon"]


def test_promotion_records_the_registry_digest_and_tags():
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)
    tags = ["ghcr.io/o/worker:sha-" + COMMIT]

    promoted = attestation.promote(made, DIGEST, IMAGE_ID, tags)

    assert promoted == made | {"digest": DIGEST, "tags": tags, "promoted": True, "replayed": False}
    assert made["digest"] is None


def test_an_unqualified_image_is_never_promoted():
    made = attestation.attest(incompatible(), IMAGE_ID, ORIGIN)

    with pytest.raises(attestation.Refused, match="unqualified image: herdr protocol is not 22; herdr server lacks"):
        attestation.promote(made, DIGEST, IMAGE_ID, [])


def test_a_pushed_image_other_than_the_tested_one_is_refused():
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)

    with pytest.raises(attestation.Refused, match="pushed image is not the tested image"):
        attestation.promote(made, DIGEST, "sha256:" + "3" * 64, [])


def test_a_malformed_registry_digest_is_refused():
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)

    with pytest.raises(attestation.Refused, match="registry digest is not a sha256 digest"):
        attestation.promote(made, "latest", IMAGE_ID, [])


def test_a_replay_keeps_the_accepted_digest_without_retagging():
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)

    replayed = attestation.replay(made, DIGEST, ["t"])

    assert replayed == made | {"digest": DIGEST, "tags": ["t"], "promoted": False, "replayed": True}


def test_a_replay_of_an_unqualified_build_is_still_refused():
    made = attestation.attest(incompatible(), IMAGE_ID, ORIGIN)

    with pytest.raises(attestation.Refused, match="unqualified image"):
        attestation.replay(made, DIGEST, ["t"])


def test_provenance_names_the_commit_and_the_run():
    environ = {
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_WORKFLOW_REF": "o/r/.github/workflows/swarm-node-image.yml@refs/heads/dev",
        "GITHUB_RUN_ID": "9",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_REF": "refs/heads/dev",
        "RUNNER_ENVIRONMENT": "github-hosted",
    }

    assert attestation.provenance(COMMIT, environ) == {
        "commit": COMMIT,
        "repository": "o/r",
        "workflow": "o/r/.github/workflows/swarm-node-image.yml@refs/heads/dev",
        "run_id": "9",
        "run_attempt": "2",
        "ref": "refs/heads/dev",
        "runner": "github-hosted",
    }
    assert attestation.provenance(COMMIT, {})["run_id"] == ""


@pytest.mark.parametrize("commit", ["abc", "A" * 40, "a" * 41])
def test_provenance_needs_a_full_commit(commit):
    with pytest.raises(attestation.Refused, match="provenance needs a full agentihooks commit"):
        attestation.provenance(commit, {})


def _cli(tmp_path, monkeypatch, probe):
    (tmp_path / "probe.json").write_text(json.dumps(probe))
    monkeypatch.setattr(attestation.os, "environ", {"GITHUB_RUN_ID": "5"})
    output = tmp_path / "attestation.json"
    argv = ["attest", "--probe", str(tmp_path / "probe.json"), "--image-id", IMAGE_ID, "--commit", COMMIT]
    return attestation.main([*argv, "--output", str(output)]), output


def test_cli_attest_writes_the_artifact_and_exits_zero_when_promotable(tmp_path, monkeypatch, capsys):
    code, output = _cli(tmp_path, monkeypatch, VALID)

    written = json.loads(output.read_text())
    assert code == 0
    assert written["promotable"] is True and written["provenance"]["run_id"] == "5"
    assert capsys.readouterr().out.strip() == "qualified 3 of 3 targets; promotable"


def test_cli_attest_writes_the_rejection_and_exits_one(tmp_path, monkeypatch, capsys):
    code, output = _cli(tmp_path, monkeypatch, incompatible())

    assert code == 1
    assert json.loads(output.read_text())["report"]["worker_image_qualified_targets"] == 2
    assert capsys.readouterr().out.strip() == (
        "qualified 2 of 3 targets; refused: herdr protocol is not 22; herdr server lacks detached_server_daemon"
    )


def _promote(tmp_path, monkeypatch, probe, *extra):
    _, made = _cli(tmp_path, monkeypatch, probe)
    output = tmp_path / "promoted.json"
    argv = ["promote", "--attestation", str(made), "--digest", DIGEST, "--config", IMAGE_ID, "--tag", "x:sha-1"]
    return attestation.main([*argv, "--tag", "x:sha-2", *extra, "--output", str(output)]), output


def test_cli_promote_writes_the_promoted_artifact(tmp_path, monkeypatch, capsys):
    code, output = _promote(tmp_path, monkeypatch, VALID)

    written = json.loads(output.read_text())
    assert code == 0
    assert (written["digest"], written["tags"], written["promoted"]) == (DIGEST, ["x:sha-1", "x:sha-2"], True)
    assert capsys.readouterr().out.strip().endswith(f"promoted {DIGEST}")


def test_cli_promote_records_a_replay(tmp_path, monkeypatch, capsys):
    code, output = _promote(tmp_path, monkeypatch, VALID, "--existing")

    written = json.loads(output.read_text())
    assert code == 0
    assert (written["promoted"], written["replayed"], written["digest"]) == (False, True, DIGEST)
    assert capsys.readouterr().out.strip().endswith(f"kept accepted {DIGEST}")


def test_cli_promote_refuses_and_writes_nothing(tmp_path, monkeypatch, capsys):
    code, output = _promote(tmp_path, monkeypatch, incompatible())

    assert code == 1
    assert not output.exists()
    assert "unqualified image" in capsys.readouterr().err
