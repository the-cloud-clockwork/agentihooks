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
CONTROLLER_PUBLISH = ["swarm.>", "$KV.hub-leases.>"]
CONTROLLER = {"publish": {"allow": CONTROLLER_PUBLISH}, "subscribe": {"allow": SUBSCRIBE}}


def _agentihooks(tmp_path: Path, pub: dict, sub: dict, body: str | None = None, controller: dict | None = None) -> Path:
    root = tmp_path / "agentihooks"
    source = root / grants.MEMBER_SOURCE
    source.parent.mkdir(parents=True)
    for package in (root / "scripts", source.parent):
        (package / "__init__.py").write_text("")
    controller = controller or {"pub": {"allow": CONTROLLER_PUBLISH}, "sub": {"allow": SUBSCRIBE}}
    source.write_text(
        body
        or f"def member_claims(user):\n    return {{'pub': {pub!r}, 'sub': {sub!r}}}\n\n\n"
        f"def controller_claims():\n    return {controller!r}\n"
    )
    return root


def _users(hive: dict, controller: dict | None, password: str | None) -> list[dict]:
    user = {"user": grants.CONTROLLER_USER}
    if password:
        user["password"] = password
    else:
        user["openbao"] = "k8s/c"
    if controller is not None:
        user["permissions"] = controller
    return [user, hive]


def _antoncore(
    tmp_path: Path, permissions: dict | None, user: str = grants.HIVE_USER, controller: dict | None = CONTROLLER
) -> Path:
    root = tmp_path / "antoncore"
    values = root / grants.HIVE_VALUES
    values.parent.mkdir(parents=True)
    hive = {"user": user, "password": "<< $NATS_HIVE_PASSWORD >>"}
    if permissions is not None:
        hive["permissions"] = permissions
    users = _users(hive, controller, "x")
    values.write_text(yaml.safe_dump({"config": {"merge": {"accounts": {"SWARM": {"users": users}}}}}))
    return root


MATCH = [
    "The hive member NATS grants match antoncore: 2 publish and 1 subscribe subjects.",
    "The controller NATS grants match antoncore: 2 publish and 1 subscribe subjects.",
]


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
    assert lines == MATCH


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
        "::error::The swarm NATS grants cannot be read (ValueError: the SWARM account has no swarm-hive user)."
    ]


def _operator_antoncore(
    tmp_path: Path, permissions: dict, user: str = grants.HIVE_USER, controller: dict | None = CONTROLLER
) -> Path:
    root = tmp_path / "operator" / "antoncore"
    values = root / grants.HIVE_VALUES
    values.parent.mkdir(parents=True)
    values.write_text(yaml.safe_dump({"config": {"merge": {"operator": "eyJ", "resolver": {"type": "full"}}}}))
    users = _users({"user": user, "openbao": "k8s/h", "permissions": permissions}, controller, None)
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


def test_in_operator_mode_a_retired_hive_user_passes(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _operator_antoncore(tmp_path, {}, user="swarm-ledger")
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 0
    assert lines == [
        "The antoncore swarm-hive user is retired, so hives connect only with minted member credentials.",
        MATCH[1],
    ]


def test_in_operator_mode_a_retired_hive_user_without_a_controller_user_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    root = _operator_antoncore(tmp_path, {}, user="swarm-ledger")
    users = yaml.safe_load((root / grants.SERVICE_USERS).read_text())["users"]
    (root / grants.SERVICE_USERS).write_text(yaml.safe_dump({"users": users[1:]}))
    status, lines = _run(capsys, agentihooks, root)
    assert status == 1
    assert lines == [
        "::error::The swarm NATS grants cannot be read (ValueError: the SWARM account has no swarm-controller user)."
    ]


def test_in_operator_mode_a_retired_hive_user_still_grades_the_controller(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _operator_antoncore(tmp_path, {}, user="swarm-ledger", controller=None)
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    ours = "is granted only by the agentihooks controller credential (scripts/hive/nats_grant.py)."
    theirs = f"is granted only by the antoncore swarm-controller user ({grants.HIVE_VALUES})."
    assert lines == [
        "The antoncore swarm-hive user is retired, so hives connect only with minted member credentials.",
        f"::error::publish allow $KV.hub-leases.> {ours}",
        f"::error::publish allow swarm.> {ours}",
        f"::error::publish allow > {theirs}",
        f"::error::subscribe allow _INBOX.> {ours}",
        f"::error::subscribe allow > {theirs}",
    ]


def test_a_member_source_that_cannot_render_its_claims_is_red(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    agentihooks = _agentihooks(tmp_path / "bare", {}, {}, body="def user_claims():\n    return {}\n")
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines[0].startswith(
        "::error::The swarm NATS grants cannot be read"
        " (ValueError: scripts/hive/nats_grant.py did not render member_claims: "
    )
    assert "has no attribute 'member_claims'" in "\n".join(lines)
    assert lines[-1].endswith(").")


def test_a_missing_antoncore_checkout_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    status, lines = _run(capsys, agentihooks, tmp_path / "absent")
    assert status == 1
    assert lines[0].startswith("::error::The swarm NATS grants cannot be read (FileNotFoundError: ")


def test_both_checkouts_are_required(capsys):
    with pytest.raises(SystemExit) as exit_info:
        grants.main([])
    assert exit_info.value.code == 2
    assert "the following arguments are required: --agentihooks, --antoncore" in capsys.readouterr().err


def test_the_help_states_what_the_check_fails_on(capsys):
    with pytest.raises(SystemExit):
        grants.main(["--help"])
    lines = capsys.readouterr().out.splitlines()
    assert "Fail when hive or controller NATS grants differ from antoncore users." in lines


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
        MATCH,
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


def test_an_unrestricted_antoncore_controller_user_fails(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _operator_antoncore(
        tmp_path, {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}}, controller=None
    )
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    ours = "is granted only by the agentihooks controller credential (scripts/hive/nats_grant.py)."
    theirs = f"is granted only by the antoncore swarm-controller user ({grants.HIVE_VALUES})."
    assert lines == [
        f"::error::publish allow $KV.hub-leases.> {ours}",
        f"::error::publish allow swarm.> {ours}",
        f"::error::publish allow > {theirs}",
        f"::error::subscribe allow _INBOX.> {ours}",
        f"::error::subscribe allow > {theirs}",
    ]


def test_a_wider_antoncore_controller_user_fails(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    wider = {"publish": {"allow": [*CONTROLLER_PUBLISH, "$JS.API.>"]}, "subscribe": {"allow": SUBSCRIBE}}
    antoncore = _antoncore(
        tmp_path / "wider", {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}}, controller=wider
    )
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines == [
        "::error::publish allow $JS.API.> is granted only by the antoncore swarm-controller user"
        f" ({grants.HIVE_VALUES})."
    ]


def test_a_controller_subject_only_agentihooks_grants_fails(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    controller = {"pub": {"allow": [*CONTROLLER_PUBLISH, "dead.>"]}, "sub": {"allow": SUBSCRIBE}}
    agentihooks = _agentihooks(tmp_path / "extra", {"allow": PUBLISH}, {"allow": SUBSCRIBE}, controller=controller)
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert lines == [
        "::error::publish allow dead.> is granted only by the agentihooks controller credential"
        " (scripts/hive/nats_grant.py)."
    ]


def test_a_missing_controller_user_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    root = _operator_antoncore(tmp_path, {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}})
    users = yaml.safe_load((root / grants.SERVICE_USERS).read_text())["users"]
    (root / grants.SERVICE_USERS).write_text(yaml.safe_dump({"users": users[1:]}))
    status, lines = _run(capsys, agentihooks, root)
    assert status == 1
    assert lines == [
        "::error::The swarm NATS grants cannot be read (ValueError: the SWARM account has no swarm-controller user)."
    ]


def test_a_source_without_controller_claims_is_red(tmp_path, capsys):
    _, antoncore = _matching(tmp_path)
    body = (
        f"def member_claims(user):\n    return {{'pub': {{'allow': {PUBLISH!r}}}, 'sub': {{'allow': {SUBSCRIBE!r}}}}}\n"
    )
    status, lines = _run(capsys, _agentihooks(tmp_path / "old", {}, {}, body=body), antoncore)
    assert status == 1
    assert lines[0].startswith(
        "::error::The swarm NATS grants cannot be read"
        " (ValueError: scripts/hive/nats_grant.py did not render controller_claims: "
    )


def test_the_controller_grants_render_the_controller_claims():
    claims = nats_grant.controller_claims()
    assert claims["type"] == "user"
    assert grants.controller_grants(str(ROOT)) == {
        "publish": {"allow": frozenset(nats_grant.CONTROLLER_PUBLISH), "deny": frozenset()},
        "subscribe": {"allow": frozenset({"_INBOX.>", "swarm.*.slots", "slots.*.*"}), "deny": frozenset()},
    }


def test_the_controller_grant_covers_every_bucket_and_queue_the_hub_opens():
    from scripts.events import nats
    from scripts.hive import buckets, execution_events, presence
    from scripts.hub import hive_loss, lease, pulls, reconciler, runner, slots, timers
    from scripts.inbox import migration

    opened = {
        *buckets.NAMES,
        *slots.BUCKETS,
        presence.BUCKET,
        execution_events.STATE,
        execution_events.STARTS,
        reconciler.CLAIMS,
        runner.VERSIONS,
        lease.BUCKET,
        hive_loss.BUCKET,
        timers.BUCKET,
        pulls.BUCKET,
        reconciler.BUCKET,
        migration.BUCKET,
        "inbox",
        "inbox-wake",
    }
    assert set(nats_grant.CONTROLLER_BUCKETS) == opened
    publish = set(nats_grant.CONTROLLER_PUBLISH)
    assert {f"$KV.{bucket}.>" for bucket in opened} <= publish
    assert {"$KV.account-slots.>", "$KV.hive-slot-accounts.>"} <= publish
    assert {"swarm.>", "dead.>", f"{nats.QUEUE_SUBJECT}.>", "$JS.ACK.>"} <= publish
    assert not {">", "$JS.API.>", "$KV.>"} & publish


def test_the_controller_grant_keeps_legacy_event_routing_and_swarm_discovery():
    from scripts.events.nats_legacy import legacy_target

    publish = {"allow": list(nats_grant.CONTROLLER_PUBLISH)}
    assert legacy_target("swarm.demo.task.claimed", publish) == "legacy.swarm.demo.task.claimed"
    assert "$JS.API.STREAM.LIST" in nats_grant.CONTROLLER_PUBLISH


def test_the_controller_grant_serves_hub_slot_requests():
    from scripts.events.subjects import slot_request

    claims = nats_grant.controller_claims()
    root, _, _ = slot_request("rig", "m1").split(".")
    assert f"{root}.*.*" in claims["sub"]["allow"]
    assert "_INBOX.>" in claims["pub"]["allow"]


def test_lint_checks_out_the_antoncore_service_users():
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint"]["steps"]
    checkout = next(s for s in steps if s.get("with", {}).get("repository") == "the-cloud-clockwork/antoncore")
    assert grants.SERVICE_USERS in checkout["with"]["sparse-checkout"].split()
