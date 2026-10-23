from pathlib import Path

import pytest
import yaml

from scripts import ci_hive_nats_grants as grants
from scripts.hive import nats_grant, nats_permissions

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ["swarm.*.execution.>", "$JS.ACK.>"]
SUBSCRIBE = ["_INBOX.>"]


def _agentihooks(tmp_path: Path, pub: dict, sub: dict) -> Path:
    root = tmp_path / "agentihooks"
    source = root / grants.MEMBER_SOURCE
    source.parent.mkdir(parents=True)
    source.write_text(f"MEMBER = {{'pub': {pub!r}, 'sub': {sub!r}}}\n")
    return root


def _antoncore(tmp_path: Path, permissions: dict | None, user: str = grants.HIVE_USER) -> Path:
    root = tmp_path / "antoncore"
    values = root / grants.HIVE_VALUES
    values.parent.mkdir(parents=True)
    hive = {"user": user, "password": "<< $NATS_HIVE_PASSWORD >>"}
    if permissions is not None:
        hive["permissions"] = permissions
    users = [{"user": "swarm-controller", "password": "x"}, hive]
    values.write_text(yaml.safe_dump({"config": {"merge": {"accounts": {"SWARM": {"users": users}}}}}))
    return root


def _run(capsys, agentihooks: Path, antoncore: Path) -> tuple[int, list[str]]:
    status = grants.main(["--agentihooks", str(agentihooks), "--antoncore", str(antoncore)])
    return status, capsys.readouterr().out.splitlines()


def _matching(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _agentihooks(tmp_path, {"allow": PUBLISH}, {"allow": SUBSCRIBE}),
        _antoncore(tmp_path, {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}}),
    )


def test_matching_grant_sets_pass(tmp_path, capsys):
    status, lines = _run(capsys, *_matching(tmp_path))
    assert status == 0
    assert lines == ["The hive member NATS grants match antoncore: 2 publish and 1 subscribe subjects."]


def test_a_planted_extra_subject_in_the_member_credential_fails(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    agentihooks = _agentihooks(tmp_path / "planted", {"allow": [*PUBLISH, "planted.>"]}, {"allow": SUBSCRIBE})
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines == [
        "::error::publish allow planted.> is granted only by the agentihooks member credential"
        " (scripts/hive/nats_permissions.py)."
    ]


def test_a_planted_extra_subject_in_the_antoncore_hive_user_fails(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _antoncore(
        tmp_path / "planted", {"publish": {"allow": PUBLISH}, "subscribe": {"allow": [*SUBSCRIBE, "planted"]}}
    )
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines == [
        "::error::subscribe allow planted is granted only by the antoncore swarm-hive user"
        " (k8s/values-overlays/agentihooks-swarm-nats/values.yaml)."
    ]


def test_a_deny_on_one_side_only_fails(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _antoncore(
        tmp_path / "deny", {"publish": {"allow": PUBLISH, "deny": ["dead.x"]}, "subscribe": SUBSCRIBE}
    )
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines == [
        "::error::publish deny dead.x is granted only by the antoncore swarm-hive user"
        " (k8s/values-overlays/agentihooks-swarm-nats/values.yaml)."
    ]


def test_an_unrestricted_member_credential_differs_from_a_restricted_hive_user(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    status, lines = _run(capsys, _agentihooks(tmp_path / "open", {}, {}), antoncore)
    assert status == 1
    ours = "is granted only by the agentihooks member credential (scripts/hive/nats_permissions.py)."
    theirs = (
        "is granted only by the antoncore swarm-hive user (k8s/values-overlays/agentihooks-swarm-nats/values.yaml)."
    )
    assert lines == [
        f"::error::publish allow > {ours}",
        f"::error::publish allow $JS.ACK.> {theirs}",
        f"::error::publish allow swarm.*.execution.> {theirs}",
        f"::error::subscribe allow > {ours}",
        f"::error::subscribe allow _INBOX.> {theirs}",
    ]


def test_string_and_list_shorthand_read_as_allow_lists(tmp_path, capsys):
    agentihooks = _agentihooks(tmp_path, {"allow": ["a"]}, {"allow": ["_INBOX.>"]})
    antoncore = _antoncore(tmp_path, {"publish": ["a"], "subscribe": "_INBOX.>"})
    assert _run(capsys, agentihooks, antoncore)[0] == 0


def test_a_missing_hive_user_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    status, lines = _run(capsys, agentihooks, _antoncore(tmp_path / "missing", {}, user="swarm-ledger"))
    assert status == 1
    assert lines == [
        "::error::The hive member NATS grants cannot be read (ValueError: the SWARM account has no swarm-hive user)."
    ]


def test_a_member_source_without_member_grants_is_red(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    source = tmp_path / "bare" / grants.MEMBER_SOURCE
    source.parent.mkdir(parents=True)
    source.write_text("OTHER = {}\nOTHER.MEMBER = {}\n")
    status, lines = _run(capsys, tmp_path / "bare", antoncore)
    assert status == 1
    assert lines == [
        "::error::The hive member NATS grants cannot be read"
        " (ValueError: scripts/hive/nats_permissions.py assigns no MEMBER grants)."
    ]


def test_a_missing_antoncore_checkout_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    status, lines = _run(capsys, agentihooks, tmp_path / "absent")
    assert status == 1
    assert lines[0].startswith("::error::The hive member NATS grants cannot be read (FileNotFoundError: ")


def test_the_member_checkout_defaults_to_the_working_directory(tmp_path, capsys, monkeypatch):
    agentihooks, antoncore = _matching(tmp_path)
    monkeypatch.chdir(agentihooks)
    assert grants.main(["--antoncore", str(antoncore)]) == 0


def test_the_antoncore_checkout_is_required(capsys):
    with pytest.raises(SystemExit) as exit_info:
        grants.main([])
    assert exit_info.value.code == 2
    assert "the following arguments are required: --antoncore" in capsys.readouterr().err


def test_the_help_states_what_the_check_fails_on(capsys):
    with pytest.raises(SystemExit):
        grants.main(["--help"])
    lines = capsys.readouterr().out.splitlines()
    assert "Fail when hive member NATS grants differ from the antoncore hive user." in lines


def test_minted_claims_never_share_the_member_grants():
    claims = nats_grant.user_claims()
    claims["pub"]["planted"] = True
    assert "planted" not in nats_permissions.MEMBER["pub"]


def test_the_member_grants_render_the_minted_user_claims():
    claims = nats_grant.user_claims()
    rendered = grants.member_grants(ROOT)
    assert {key: claims[key] for key in ("pub", "sub")} == nats_permissions.MEMBER
    for direction, key in (("publish", "pub"), ("subscribe", "sub")):
        assert rendered[direction]["allow"] == frozenset(claims[key].get("allow") or [">"])
        assert rendered[direction]["deny"] == frozenset(claims[key].get("deny") or [])


def test_lint_grades_the_member_grants_against_the_antoncore_dev_hive_user():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"]["lint"]["steps"]
    checkout = next(s for s in steps if s.get("with", {}).get("repository") == "the-cloud-clockwork/antoncore")
    assert checkout["with"]["ref"] == "dev"
    assert grants.HIVE_VALUES in checkout["with"]["sparse-checkout"].split()
    check = next(s for s in steps if "scripts.ci_hive_nats_grants" in s.get("run", ""))
    assert check["if"] == "${{ !cancelled() }}"
    assert steps.index(checkout) < steps.index(check)
    assert '--agentihooks "$GITHUB_WORKSPACE" --antoncore "$GITHUB_WORKSPACE/.antoncore"' in check["run"]
    assert 'cd "$RUNNER_TEMP/grader"' in check["run"]
