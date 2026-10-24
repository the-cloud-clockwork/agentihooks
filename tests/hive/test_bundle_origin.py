import json
import os
import subprocess

import pytest

import scripts.hive.bundle_origin as bundle_origin
from scripts.hive import auth, cli
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao

pytestmark = pytest.mark.unit
RECORD = "/v1/secret/data/k8s/agentihooks-swarm-workstation-hive-settings"


def bundle(tmp_path, remote="git@github.com:The-Cloud-Clockwork/agentihooks-bundle.git", branch="dev"):
    checkout = tmp_path / "bundle"
    subprocess.run(["git", "init", f"--initial-branch={branch}", str(checkout)], check=True, capture_output=True)
    if remote:
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", remote], check=True)
    return checkout


def linked(tmp_path, checkout):
    home = tmp_path / "state"
    home.mkdir()
    (home / "state.json").write_text(json.dumps({"bundle": {"path": str(checkout)}}))
    return home


def environ(home):
    return {"AGENTIHOOKS_HOME": str(home), "OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN}


def test_an_ssh_remote_becomes_an_https_address_with_the_current_branch(tmp_path):
    home = linked(tmp_path, bundle(tmp_path, branch="main"))
    assert bundle_origin.origin(home, {}) == {
        "HIVE_BUNDLE_URL": "https://github.com/the-cloud-clockwork/agentihooks-bundle.git",
        "HIVE_BUNDLE_REF": "main",
    }


def test_an_https_remote_keeps_its_address(tmp_path):
    home = linked(tmp_path, bundle(tmp_path, remote="https://github.com/the-cloud-clockwork/agentihooks-bundle"))
    assert bundle_origin.origin(home, {})["HIVE_BUNDLE_URL"] == (
        "https://github.com/the-cloud-clockwork/agentihooks-bundle.git"
    )


@pytest.mark.parametrize("state", [None, "{}", '{"bundle": {}}', "not json", "[]", '{"bundle": "path"}'])
def test_no_linked_bundle_gives_an_empty_origin(tmp_path, state):
    home = tmp_path / "state"
    home.mkdir()
    if state is not None:
        (home / "state.json").write_text(state)
    assert bundle_origin.origin(home, {}) == {"HIVE_BUNDLE_URL": "", "HIVE_BUNDLE_REF": ""}


def test_the_bundle_path_variable_names_the_bundle_when_none_is_linked(tmp_path):
    home = tmp_path / "state"
    home.mkdir()
    checkout = bundle(tmp_path, branch="main")
    assert bundle_origin.origin(home, {"AGENTIHOOKS_BUNDLE_PATH": str(checkout)}) == {
        "HIVE_BUNDLE_URL": "https://github.com/the-cloud-clockwork/agentihooks-bundle.git",
        "HIVE_BUNDLE_REF": "main",
    }


def test_the_linked_bundle_wins_over_the_bundle_path_variable(tmp_path):
    home = linked(tmp_path, bundle(tmp_path, branch="dev"))
    assert bundle_origin.origin(home, {"AGENTIHOOKS_BUNDLE_PATH": str(tmp_path / "other")})["HIVE_BUNDLE_REF"] == "dev"


def test_a_bundle_without_an_origin_remote_is_refused(tmp_path):
    checkout = bundle(tmp_path, remote="")
    with pytest.raises(auth.HiveError) as refused:
        bundle_origin.origin(linked(tmp_path, checkout), {})
    assert str(refused.value) == f"the linked bundle at {checkout} has no origin remote naming a host and repository"


def test_a_detached_bundle_is_refused(tmp_path):
    checkout = bundle(tmp_path)
    git = ["git", "-C", str(checkout), "-c", "user.name=f", "-c", "user.email=f@example.test"]
    subprocess.run([*git, "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    subprocess.run([*git, "checkout", "--detach"], check=True, capture_output=True)
    with pytest.raises(auth.HiveError) as refused:
        bundle_origin.origin(linked(tmp_path, checkout), {})
    assert str(refused.value) == f"the linked bundle at {checkout} is not on a branch"


def test_publish_merges_the_origin_into_the_settings_record_and_keeps_its_other_keys(tmp_path):
    fake = FakeBao({"GITHUB_PERSONAL_ACCESS_TOKEN": "kept", "HIVE_BUNDLE_REF": "old"}, version=4, path=RECORD)
    home = linked(tmp_path, bundle(tmp_path))
    assert bundle_origin.publish(environ(home), bundle_origin.SETTINGS_PATH, fake) == (
        "OpenBao secret/k8s/agentihooks-swarm-workstation-hive-settings: wrote HIVE_BUNDLE_REF, HIVE_BUNDLE_URL "
        "(3 keys total); bundle https://github.com/the-cloud-clockwork/agentihooks-bundle.git at dev"
    )
    assert fake.data == {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "kept",
        "HIVE_BUNDLE_URL": "https://github.com/the-cloud-clockwork/agentihooks-bundle.git",
        "HIVE_BUNDLE_REF": "dev",
    }
    assert fake.version == 5


def test_publish_with_no_bundle_writes_an_empty_origin(tmp_path):
    fake = FakeBao({"OTHER": "kept"}, path="/v1/secret/data/elsewhere")
    home = tmp_path / "state"
    home.mkdir()
    assert bundle_origin.publish(environ(home), "elsewhere", fake) == (
        "OpenBao secret/elsewhere: wrote HIVE_BUNDLE_REF, HIVE_BUNDLE_URL (3 keys total); no bundle is linked"
    )
    assert fake.data == {"OTHER": "kept", "HIVE_BUNDLE_URL": "", "HIVE_BUNDLE_REF": ""}


def test_publish_without_openbao_credentials_is_refused(tmp_path):
    with pytest.raises(auth.HiveError) as refused:
        bundle_origin.publish({"AGENTIHOOKS_HOME": str(tmp_path), "OPENBAO_URL": BAO_URL}, "elsewhere", FakeBao())
    assert str(refused.value) == "OPENBAO_URL and OPENBAO_TOKEN must both be set"


def test_a_failed_write_is_refused_with_the_openbao_status(tmp_path):
    fake = FakeBao({"OTHER": "kept"}, path=RECORD, fail_writes=True)
    home = linked(tmp_path, bundle(tmp_path))
    with pytest.raises(auth.HiveError) as refused:
        bundle_origin.publish(environ(home), bundle_origin.SETTINGS_PATH, fake)
    assert str(refused.value) == "OpenBao HTTP 503: sealed"


def test_the_cli_writes_the_named_record(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(bundle_origin, "publish", lambda env, path: calls.append((env, path)) or "written")
    assert cli.main(["bundle-origin"]) == 0
    assert cli.main(["bundle-origin", "--path", "k8s/other"]) == 0
    assert calls == [(os.environ, "k8s/agentihooks-swarm-workstation-hive-settings"), (os.environ, "k8s/other")]
    assert all(env is os.environ for env, _ in calls)
    assert capsys.readouterr().out == "written\nwritten\n"


def test_the_cli_help_names_the_record_it_writes():
    sub = next(action for action in cli._parser()._actions if action.dest == "command")
    helps = {choice.dest: choice.help for choice in sub._choices_actions}
    assert helps["bundle-origin"] == "Write the linked bundle's git address and branch into the hive settings record"
    path = next(action for action in sub.choices["bundle-origin"]._actions if action.dest == "path")
    assert path.help == "OpenBao record under the secret mount"


def test_the_cli_reports_a_refusal(monkeypatch, capsys):
    def refuse(env, path):
        raise auth.HiveError("no bundle origin")

    monkeypatch.setattr(bundle_origin, "publish", refuse)
    assert cli.main(["bundle-origin"]) == 1
    assert capsys.readouterr().err == "hive bundle-origin refused: no bundle origin\n"
