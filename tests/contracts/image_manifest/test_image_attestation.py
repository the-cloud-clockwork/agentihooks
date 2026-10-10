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
RUNTIME_METHODS = [
    "agent.get",
    "agent.list",
    "agent.prompt",
    "agent.rename",
    "pane.close",
    "pane.list",
    "pane.process_info",
    "pane.read",
    "pane.send_keys",
    "pane.send_text",
    "pane.split",
    "pane.wait_for_output",
    "server.stop",
    "tab.close",
    "tab.create",
    "tab.list",
    "workspace.close",
    "workspace.create",
    "workspace.list",
]
STATUS = {
    "running": True,
    "version": "0.9.1",
    "protocol": 22,
    "capabilities": {"detached_server_daemon": False, "endpoint_protocol_generation": 1, "health_check": True},
}
VALID = {
    "manifest": MANIFEST,
    "observed": {
        "herdr": {"version": "herdr 0.9.1", "status": STATUS, "schema": {"protocol": 22, "methods": RUNTIME_METHODS}},
        "claude": {"version": "2.1.295 (Claude Code)", "hook_registrations": 1},
        "codex": {"version": "codex-cli 0.162.0", "hook_registrations": 1},
    },
}
ORIGIN = {"commit": COMMIT, "repository": "the-cloud-clockwork/agentihooks", "run_id": "7"}
INCOMPATIBLE_REFUSALS = ["herdr protocol is not 22", "herdr server lacks health_check=True"]
PROVENANCE_ENVIRON = {
    "GITHUB_REPOSITORY": "o/r",
    "GITHUB_WORKFLOW_REF": "o/r/.github/workflows/swarm-node-image.yml@refs/heads/dev",
    "GITHUB_RUN_ID": "9",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_REF": "refs/heads/dev",
    "RUNNER_ENVIRONMENT": "github-hosted",
}


def incompatible():
    probe = copy.deepcopy(VALID)
    status = probe["observed"]["herdr"]["status"]
    status["protocol"] = 21
    status["capabilities"]["health_check"] = False
    return probe


def test_the_runtime_method_contract_is_the_accepted_list():
    assert sorted(attestation.HERDR_METHODS) == RUNTIME_METHODS


def test_the_valid_image_qualifies_every_target():
    report = attestation.qualify(MANIFEST, VALID["observed"])

    assert report == {
        "package": "SV2-IMG-05",
        "targets": {
            name: {"pinned": version, "observed": version, "qualified": True, "refusals": []}
            for name, version in TOOLS.items()
        },
        "worker_image_qualified_targets": 3,
        "promotable": True,
    }


def test_incompatible_herdr_server_capabilities_fail_only_the_herdr_target():
    report = attestation.qualify(MANIFEST, incompatible()["observed"])

    assert report["promotable"] is False
    assert report["worker_image_qualified_targets"] == 2
    assert report["targets"]["herdr"]["refusals"] == INCOMPATIBLE_REFUSALS
    assert report["targets"]["codex"]["qualified"] is True


@pytest.mark.parametrize(
    ("change", "refusal"),
    [
        (lambda h: h["status"].update(running=False), "herdr headless server did not run"),
        (lambda h: h["status"].update(running="yes"), "herdr headless server did not run"),
        (lambda h: h["schema"].update(protocol=23), "herdr protocol is not 22"),
        (lambda h: h["status"]["capabilities"].pop("health_check"), "herdr server lacks health_check=True"),
        (lambda h: h["status"]["capabilities"].update(health_check="yes"), "herdr server lacks health_check=True"),
        (lambda h: h["status"]["capabilities"].update(health_check=1), "herdr server lacks health_check=True"),
        (
            lambda h: h["status"]["capabilities"].update(endpoint_protocol_generation=2),
            "herdr server lacks endpoint_protocol_generation=1",
        ),
        (
            lambda h: h["status"]["capabilities"].update(endpoint_protocol_generation=True),
            "herdr server lacks endpoint_protocol_generation=1",
        ),
        (lambda h: h["status"].pop("capabilities"), "herdr server lacks endpoint_protocol_generation=1"),
        (
            lambda h: h["schema"].update(methods=[m for m in RUNTIME_METHODS if m not in ("pane.read", "agent.list")]),
            "herdr socket API lacks agent.list, pane.read",
        ),
        (lambda h: h.pop("schema"), "herdr protocol is not 22"),
        (lambda h: h.pop("status"), "herdr headless server did not run"),
        (lambda h: h.update(version="herdr 0.9.2"), "herdr version 'herdr 0.9.2' is not pinned 'herdr 0.9.1'"),
    ],
)
def test_each_herdr_incompatibility_names_its_refusal(change, refusal):
    observed = copy.deepcopy(VALID["observed"])
    change(observed["herdr"])

    report = attestation.qualify(MANIFEST, observed)

    assert refusal in report["targets"]["herdr"]["refusals"]
    assert report["targets"]["herdr"]["qualified"] is False
    assert report["promotable"] is False


def test_a_schema_without_methods_lacks_every_runtime_method():
    observed = copy.deepcopy(VALID["observed"])
    del observed["herdr"]["schema"]["methods"]

    refusals = attestation.qualify(MANIFEST, observed)["targets"]["herdr"]["refusals"]

    assert refusals == ["herdr socket API lacks " + ", ".join(RUNTIME_METHODS)]


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_a_harness_without_session_start_hook_delivery_is_refused(target):
    observed = copy.deepcopy(VALID["observed"])
    observed[target]["hook_registrations"] = 0

    report = attestation.qualify(MANIFEST, observed)

    assert report["targets"][target]["refusals"] == [f"{target} headless launch delivered no SessionStart hook"]
    assert report["worker_image_qualified_targets"] == 2


def test_a_harness_with_two_registrations_qualifies():
    observed = copy.deepcopy(VALID["observed"])
    observed["claude"]["hook_registrations"] = 2

    assert attestation.qualify(MANIFEST, observed)["targets"]["claude"]["qualified"] is True


def test_a_missing_target_is_refused_and_not_counted():
    observed = copy.deepcopy(VALID["observed"])
    del observed["codex"]

    report = attestation.qualify(MANIFEST, observed)

    assert report["targets"]["codex"] == {
        "pinned": "codex-cli 0.162.0",
        "observed": None,
        "qualified": False,
        "refusals": [
            "codex version None is not pinned 'codex-cli 0.162.0'",
            "codex headless launch delivered no SessionStart hook",
        ],
    }
    assert report["worker_image_qualified_targets"] == 2


def test_a_target_without_a_pin_never_qualifies():
    manifest = {"observed": {"tools": {"herdr": "herdr 0.9.1", "claude": "2.1.295 (Claude Code)"}}}
    observed = copy.deepcopy(VALID["observed"])
    observed["codex"]["version"] = ""

    report = attestation.qualify(manifest, observed)

    assert report["targets"]["codex"]["refusals"] == ["codex version '' is not pinned ''"]
    assert report["promotable"] is False


def test_the_release_artifact_names_digest_manifest_report_and_commit_provenance():
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)

    assert made == {
        "schema_version": 1,
        "package": "SV2-IMG-05",
        "image_id": IMAGE_ID,
        "digest": None,
        "tags": [],
        "promotable": True,
        "promoted": False,
        "refusals": [],
        "manifest": MANIFEST,
        "report": attestation.qualify(MANIFEST, VALID["observed"]),
        "compatibility": {
            "baseline": "local herdr 0.9.1 runtime path",
            "herdr_protocol": 22,
            "herdr_server_capabilities": {"endpoint_protocol_generation": 1, "health_check": True},
            "herdr_methods": RUNTIME_METHODS,
            "targets": TOOLS,
        },
        "provenance": ORIGIN,
    }


def test_a_manifest_from_another_commit_is_not_promotable():
    probe = copy.deepcopy(VALID)
    probe["manifest"]["source_revision"] = "b" * 40

    made = attestation.attest(probe, IMAGE_ID, ORIGIN)

    assert made["promotable"] is False
    assert made["refusals"] == ["image manifest names another commit"]


@pytest.mark.parametrize("image_id", ["1" * 64, "sha256:" + "1" * 63, "sha256:" + "G" * 64, IMAGE_ID + "0"])
def test_a_malformed_tested_image_id_is_not_promotable(image_id):
    made = attestation.attest(VALID, image_id, ORIGIN)

    assert made["refusals"] == ["tested image id is not a sha256 digest"]


def test_attest_carries_every_target_refusal():
    made = attestation.attest(incompatible(), IMAGE_ID, ORIGIN)

    assert made["promotable"] is False
    assert made["refusals"] == INCOMPATIBLE_REFUSALS


@pytest.mark.parametrize("replayed", [False, True])
def test_promotion_records_the_registry_digest_and_tags(replayed):
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)
    tags = ("ghcr.io/o/worker:sha-" + COMMIT,)

    promoted = attestation.promote(made, DIGEST, IMAGE_ID, tags, replayed)

    assert promoted == made | {
        "digest": DIGEST,
        "config": IMAGE_ID,
        "tags": list(tags),
        "promoted": not replayed,
        "replayed": replayed,
    }
    assert made["digest"] is None


def test_promotion_defaults_to_a_new_publication():
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)

    assert attestation.promote(made, DIGEST, IMAGE_ID, [])["promoted"] is True


def test_an_unqualified_image_is_never_promoted():
    made = attestation.attest(incompatible(), IMAGE_ID, ORIGIN)

    with pytest.raises(attestation.Refused, match="^unqualified image: " + "; ".join(INCOMPATIBLE_REFUSALS) + "$"):
        attestation.promote(made, DIGEST, IMAGE_ID, [])


@pytest.mark.parametrize("replayed", [False, True])
def test_a_registry_image_other_than_the_tested_one_is_refused(replayed):
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)

    with pytest.raises(attestation.Refused, match="^registry image is not the tested image$"):
        attestation.promote(made, DIGEST, "sha256:" + "3" * 64, [], replayed)


@pytest.mark.parametrize("digest", ["latest", "sha256:x", DIGEST + "0"])
def test_a_malformed_registry_digest_is_refused(digest):
    made = attestation.attest(VALID, IMAGE_ID, ORIGIN)

    with pytest.raises(attestation.Refused, match="^registry digest is not a sha256 digest$"):
        attestation.promote(made, digest, IMAGE_ID, [])


def test_provenance_names_the_commit_and_the_run():
    assert attestation.provenance(COMMIT, PROVENANCE_ENVIRON) == {
        "commit": COMMIT,
        "repository": "o/r",
        "workflow": "o/r/.github/workflows/swarm-node-image.yml@refs/heads/dev",
        "run_id": "9",
        "run_attempt": "2",
        "ref": "refs/heads/dev",
        "runner": "github-hosted",
    }
    assert attestation.provenance(COMMIT, {}) == {"commit": COMMIT} | dict.fromkeys(attestation.PROVENANCE, "")


@pytest.mark.parametrize("commit", ["abc", "A" * 40, "a" * 41, "g" * 40])
def test_provenance_needs_a_full_commit(commit):
    with pytest.raises(attestation.Refused, match="^provenance needs a full agentihooks commit$"):
        attestation.provenance(commit, {})


def _cli(tmp_path, monkeypatch, probe):
    for name, value in PROVENANCE_ENVIRON.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "probe.json").write_text(json.dumps(probe))
    output = tmp_path / "attestation.json"
    code = attestation.main(["attest", str(tmp_path / "probe.json"), IMAGE_ID, COMMIT, str(output)])
    return code, output


def test_cli_attest_writes_the_artifact_and_exits_zero_when_promotable(tmp_path, monkeypatch, capsys):
    code, output = _cli(tmp_path, monkeypatch, VALID)

    expected = attestation.attest(VALID, IMAGE_ID, attestation.provenance(COMMIT, PROVENANCE_ENVIRON))
    assert code == 0
    assert output.read_text() == json.dumps(expected, indent=2, sort_keys=True) + "\n"
    assert capsys.readouterr().out == "qualified 3 of 3 targets; promotable\n"


def test_cli_attest_writes_the_rejection_and_exits_one(tmp_path, monkeypatch, capsys):
    code, output = _cli(tmp_path, monkeypatch, incompatible())

    assert code == 1
    assert json.loads(output.read_text())["report"]["worker_image_qualified_targets"] == 2
    assert capsys.readouterr().out == "qualified 2 of 3 targets; refused: " + "; ".join(INCOMPATIBLE_REFUSALS) + "\n"


def _promote(tmp_path, monkeypatch, probe, *extra):
    _, made = _cli(tmp_path, monkeypatch, probe)
    output = tmp_path / "promoted.json"
    return attestation.main(["promote", str(made), DIGEST, IMAGE_ID, str(output), *extra]), output


def test_cli_promote_writes_the_promoted_artifact(tmp_path, monkeypatch, capsys):
    code, output = _promote(tmp_path, monkeypatch, VALID, "--tag", "x:sha-1", "--tag", "x:sha-2")

    written = json.loads(output.read_text())
    assert code == 0
    assert (written["digest"], written["config"], written["tags"]) == (DIGEST, IMAGE_ID, ["x:sha-1", "x:sha-2"])
    assert (written["promoted"], written["replayed"]) == (True, False)
    assert output.read_text().endswith("}\n") and output.read_text().startswith('{\n  "compatibility"')
    assert capsys.readouterr().out.endswith(f"SV2-IMG-05 promoted {DIGEST}\n")


def test_cli_promote_without_tags_records_none(tmp_path, monkeypatch):
    code, output = _promote(tmp_path, monkeypatch, VALID)

    assert code == 0
    assert json.loads(output.read_text())["tags"] == []


def test_cli_promote_records_a_replay(tmp_path, monkeypatch, capsys):
    code, output = _promote(tmp_path, monkeypatch, VALID, "--existing")

    written = json.loads(output.read_text())
    assert code == 0
    assert (written["promoted"], written["replayed"], written["digest"]) == (False, True, DIGEST)
    assert capsys.readouterr().out.endswith(f"SV2-IMG-05 kept accepted {DIGEST}\n")


def test_cli_promote_refuses_and_writes_nothing(tmp_path, monkeypatch, capsys):
    code, output = _promote(tmp_path, monkeypatch, incompatible())

    assert code == 1
    assert not output.exists()
    assert capsys.readouterr().err == "unqualified image: " + "; ".join(INCOMPATIBLE_REFUSALS) + "\n"


def test_cli_needs_a_command():
    with pytest.raises(SystemExit):
        attestation.main([])
