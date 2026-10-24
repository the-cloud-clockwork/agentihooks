import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import ci_hive_nats_grants as grants
from scripts.hive import nats_grant

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ["swarm.*.execution.>", "$JS.ACK.>"]
SUBSCRIBE = ["_INBOX.>"]


def _agentihooks(tmp_path: Path, pub: dict, sub: dict, body: str | None = None) -> Path:
    root = tmp_path / "agentihooks"
    source = root / grants.MEMBER_SOURCE
    source.parent.mkdir(parents=True)
    for package in (root / "scripts", source.parent):
        (package / "__init__.py").write_text("")
    source.write_text(body or f"def member_claims(user):\n    return {{'pub': {pub!r}, 'sub': {sub!r}}}\n")
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
        " (scripts/hive/nats_grant.py)."
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
    ours = "is granted only by the agentihooks member credential (scripts/hive/nats_grant.py)."
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


def _operator_antoncore(tmp_path: Path, permissions: dict, user: str = grants.HIVE_USER) -> Path:
    root = tmp_path / "operator" / "antoncore"
    values = root / grants.HIVE_VALUES
    values.parent.mkdir(parents=True)
    values.write_text(yaml.safe_dump({"config": {"merge": {"operator": "eyJ", "resolver": {"type": "full"}}}}))
    users = [
        {"user": "swarm-controller", "openbao": "k8s/c"},
        {"user": user, "openbao": "k8s/h", "permissions": permissions},
    ]
    (root / grants.SERVICE_USERS).write_text(yaml.safe_dump({"users": users}))
    return root


def test_in_operator_mode_the_hive_user_is_read_from_the_service_users_file(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _operator_antoncore(tmp_path, {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}})
    assert grants.SERVICE_USERS == "k8s/values-overlays/agentihooks-swarm-nats/users.yaml"
    assert _run(capsys, agentihooks, antoncore)[0] == 0


def test_in_operator_mode_a_differing_hive_user_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _operator_antoncore(tmp_path, {"publish": {"allow": [*PUBLISH, "x.>"]}, "subscribe": SUBSCRIBE})
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines == [
        f"::error::publish allow x.> is granted only by the antoncore swarm-hive user ({grants.HIVE_VALUES})."
    ]


def test_in_operator_mode_a_missing_hive_user_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _operator_antoncore(tmp_path, {}, user="swarm-ledger")
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines == [
        "::error::The hive member NATS grants cannot be read (ValueError: the SWARM account has no swarm-hive user)."
    ]


def test_a_member_source_that_cannot_render_its_claims_is_red(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    agentihooks = _agentihooks(tmp_path / "bare", {}, {}, body="def user_claims():\n    return {}\n")
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines[0].startswith(
        "::error::The hive member NATS grants cannot be read"
        " (ValueError: scripts/hive/nats_grant.py did not render member_claims: "
    )
    assert "has no attribute 'member_claims'" in "\n".join(lines)
    assert lines[-1].endswith(").")


def test_a_missing_antoncore_checkout_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    status, lines = _run(capsys, agentihooks, tmp_path / "absent")
    assert status == 1
    assert lines[0].startswith("::error::The hive member NATS grants cannot be read (FileNotFoundError: ")


def test_both_checkouts_are_required(capsys):
    with pytest.raises(SystemExit) as exit_info:
        grants.main([])
    assert exit_info.value.code == 2
    assert "the following arguments are required: --agentihooks, --antoncore" in capsys.readouterr().err


def test_the_help_states_what_the_check_fails_on(capsys):
    with pytest.raises(SystemExit):
        grants.main(["--help"])
    lines = capsys.readouterr().out.splitlines()
    assert "Fail when hive member NATS grants differ from the antoncore hive user." in lines


def test_the_member_grants_render_the_minted_member_claims():
    claims = nats_grant.member_claims(grants.MEMBER_KEY)
    rendered = grants.member_grants(str(ROOT))
    assert claims["sub"] == {"allow": [grants.MEMBER_INBOX]}
    publish = frozenset({*nats_grant.MEMBER_PUBLISH, "slots.*.*"})
    assert rendered["publish"] == {"allow": publish, "deny": frozenset()}
    assert rendered["subscribe"] == {"allow": frozenset({"_INBOX.>"}), "deny": frozenset()}


def test_the_member_reply_inbox_stands_for_the_shared_user_inbox(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    agentihooks = _agentihooks(tmp_path / "own", {"allow": PUBLISH}, {"allow": ["_INBOX.UMEMBER.>"]})
    assert _run(capsys, agentihooks, antoncore) == (
        0,
        ["The hive member NATS grants match antoncore: 2 publish and 1 subscribe subjects."],
    )


def test_another_member_inbox_differs_from_the_shared_user_inbox(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    agentihooks = _agentihooks(tmp_path / "other", {"allow": PUBLISH}, {"allow": ["_INBOX.UOTHER.>"]})
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines == [
        "::error::subscribe allow _INBOX.UOTHER.> is granted only by the agentihooks member credential"
        " (scripts/hive/nats_grant.py).",
        "::error::subscribe allow _INBOX.> is granted only by the antoncore swarm-hive user"
        " (k8s/values-overlays/agentihooks-swarm-nats/values.yaml).",
    ]


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


def test_lint_refuses_a_grader_without_the_grant_check(tmp_path):
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint"]["steps"]
    check = next(s for s in steps if "scripts.ci_hive_nats_grants" in s.get("run", ""))
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ci_hive_nats_grants.py").write_text(
        "from pathlib import Path\nPath('head-graded').touch()\n"
    )
    (tmp_path / "runner" / "grader").mkdir(parents=True)
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path / "runner"), GITHUB_WORKSPACE=str(tmp_path))
    result = subprocess.run(["bash", "-e", "-c", check["run"]], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert (
        "::error::The protected grader carries no scripts/ci_hive_nats_grants.py,"
        " so nothing trusted can grade the hive member grants."
    ) in result.stdout
    assert not (tmp_path / "head-graded").exists()
