import os
import subprocess
from pathlib import Path
from typing import Any

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
LEDGER = {"publish": {"allow": list(grants.LEDGER_PUBLISH)}, "subscribe": {"allow": list(grants.LEDGER_SUBSCRIBE)}}
LAB_USER = "swarm-ci-lab-scheduler"
LAB_STREAM = "AGENTIHOOKS-CI-LAB-HEADS"
LAB_DURABLE = "ci-lab-scheduler"
LAB_HEADS = f"{LAB_STREAM}.{LAB_DURABLE}"
LAB_SOURCES = {"scripts/events/nats.py": "LAB_STREAM", "scripts/ci_lab/scheduler.py": "DURABLE"}
LAB_FROM = "the agentihooks lab scheduler (scripts/events/nats.py LAB_STREAM, scripts/ci_lab/scheduler.py DURABLE)"


def _lab_publish(stream: str = LAB_STREAM, durable: str = LAB_DURABLE) -> list[str]:
    return [
        f"$JS.API.CONSUMER.INFO.{stream}.{durable}",
        f"$JS.API.CONSUMER.MSG.NEXT.{stream}.{durable}",
        f"$JS.ACK.{stream}.{durable}.>",
    ]


LAB = {"publish": {"allow": _lab_publish()}, "subscribe": {"allow": SUBSCRIBE}}


def _lab_sources(root: Path, stream: str = LAB_STREAM, durable: str = LAB_DURABLE) -> None:
    for (source, name), value in zip(LAB_SOURCES.items(), (stream, durable), strict=True):
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name} = {value!r}\nraise RuntimeError('the grant check ran {source}')\n")


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
    _lab_sources(root)
    return root


def _users(hive: dict, controller: dict | None, password: str | None) -> list[dict]:
    user = {"user": grants.CONTROLLER_USER}
    if password:
        user["password"] = password
    else:
        user["openbao"] = "k8s/c"
    if controller is not None:
        user["permissions"] = controller
    ledger = {"user": grants.LEDGER_USER, "openbao": "k8s/l", "permissions": LEDGER}
    return [user, hive, ledger, {"user": LAB_USER, "openbao": "k8s/s", "permissions": LAB}]


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
    "The ledger NATS grants match antoncore: 10 publish and 2 subscribe subjects.",
    "The lab scheduler NATS grants are in antoncore: 3 publish and 1 subscribe subjects.",
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
    status, lines = _run(capsys, agentihooks, _antoncore(tmp_path / "missing", {}, user="swarm-other"))
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
    antoncore = _operator_antoncore(tmp_path, {}, user="swarm-other")
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 0
    assert lines == [
        "The antoncore swarm-hive user is retired, so hives connect only with minted member credentials.",
        *MATCH[1:],
    ]


def test_in_operator_mode_a_retired_hive_user_without_a_controller_user_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    root = _operator_antoncore(tmp_path, {}, user="swarm-other")
    users = yaml.safe_load((root / grants.SERVICE_USERS).read_text())["users"]
    (root / grants.SERVICE_USERS).write_text(yaml.safe_dump({"users": users[1:]}))
    status, lines = _run(capsys, agentihooks, root)
    assert status == 1
    assert lines == [
        "::error::The swarm NATS grants cannot be read (ValueError: the SWARM account has no swarm-controller user)."
    ]


def test_in_operator_mode_a_retired_hive_user_still_grades_the_controller(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    antoncore = _operator_antoncore(tmp_path, {}, user="swarm-other", controller=None)
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
    assert "Fail when hive, controller or ledger NATS grants differ from antoncore users." in lines


@pytest.mark.feature("hub-only-execution-stop")
def test_the_member_grants_render_the_minted_member_claims() -> None:
    claims = nats_grant.member_claims(grants.MEMBER_KEY)
    rendered = grants.member_grants(str(ROOT))
    assert claims["sub"] == {"allow": [grants.MEMBER_INBOX]}
    publish = frozenset({*nats_grant.MEMBER_PUBLISH, "slots.*.*", "exits.*.*"})
    assert rendered["publish"] == {
        "allow": publish,
        "deny": frozenset(
            {
                "swarm.*.execution.stop",
                "swarm.*.execution.stop.>",
                "swarm.*.events.execution.stop",
                "swarm.*.events.execution.stop.>",
                "swarm.*.stop.>",
                "swarm.*.execution.exited",
                "swarm.*.events.execution.exited",
            }
        ),
    }
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
    checkout = next(s for s in steps if s.get("env", {}).get("REPOSITORY") == "the-cloud-clockwork/antoncore")
    assert checkout["env"]["REF"] == "dev"
    assert grants.HIVE_VALUES in checkout["env"]["SELECTED_PATHS"].split()
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
        "subscribe": {"allow": frozenset({"_INBOX.>", "swarm.*.slots", "slots.*.*", "exits.*.*"}), "deny": frozenset()},
    }


def test_the_controller_grant_covers_every_bucket_and_queue_the_hub_opens():
    from scripts.events import nats
    from scripts.hive import buckets, execution_events, presence
    from scripts.hub import hive_loss, lease, pulls, reconciler, runner, slots, timers
    from scripts.inbox import migration
    from scripts.scheduler import core

    opened = {
        *buckets.NAMES,
        *slots.BUCKETS,
        core.BUCKET,
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
    checkout = next(s for s in steps if s.get("env", {}).get("REPOSITORY") == "the-cloud-clockwork/antoncore")
    assert grants.SERVICE_USERS in checkout["env"]["SELECTED_PATHS"].split()


def test_a_ledger_subject_only_antoncore_grants_fails(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    root = _operator_antoncore(tmp_path, {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}})
    users = yaml.safe_load((root / grants.SERVICE_USERS).read_text())["users"]
    users[2]["permissions"]["publish"]["allow"].append("$KV.inbox-wake.>")
    (root / grants.SERVICE_USERS).write_text(yaml.safe_dump({"users": users}))
    status, lines = _run(capsys, agentihooks, root)
    assert status == 1
    assert lines == [
        f"::error::publish allow $KV.inbox-wake.> is granted only by the antoncore swarm-ledger user"
        f" ({grants.HIVE_VALUES})."
    ]


def test_a_missing_ledger_user_is_red(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    root = _operator_antoncore(tmp_path, {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}})
    users = yaml.safe_load((root / grants.SERVICE_USERS).read_text())["users"]
    (root / grants.SERVICE_USERS).write_text(yaml.safe_dump({"users": users[:2]}))
    status, lines = _run(capsys, agentihooks, root)
    assert status == 1
    assert lines == [
        "::error::The swarm NATS grants cannot be read (ValueError: the SWARM account has no swarm-ledger user)."
    ]


def test_a_ledger_subject_the_antoncore_user_lacks_names_the_grader_as_its_source(tmp_path, capsys):
    agentihooks, _ = _matching(tmp_path)
    root = _operator_antoncore(tmp_path, {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}})
    users = yaml.safe_load((root / grants.SERVICE_USERS).read_text())["users"]
    users[2]["permissions"]["publish"]["allow"].remove("$KV.inbox.>")
    (root / grants.SERVICE_USERS).write_text(yaml.safe_dump({"users": users}))
    status, lines = _run(capsys, agentihooks, root)
    assert status == 1
    assert lines == [
        "::error::publish allow $KV.inbox.> is granted only by the agentihooks ledger grant"
        " (scripts/ci_hive_nats_grants.py)."
    ]
    assert grants.ledger_grants() == {
        "publish": {"allow": frozenset(grants.LEDGER_PUBLISH), "deny": frozenset()},
        "subscribe": {"allow": frozenset({"swarm.>", "_INBOX.>"}), "deny": frozenset()},
    }


def test_the_ledger_grant_covers_the_inbox_bucket_the_seat_inbox_writes_and_purges() -> None:
    from scripts.inbox.bus import SeatInbox

    opened = []

    class Bus:
        def for_swarm(self, swarm: str) -> "Bus":
            return self

        def state(self, bucket):
            opened.append(bucket)

    SeatInbox(Bus(), "rig")
    assert opened == list(grants.LEDGER_BUCKETS)
    publish = set(grants.LEDGER_PUBLISH)
    assert {f"$KV.{bucket}.>" for bucket in opened} <= publish
    assert {f"$JS.API.STREAM.PURGE.KV_{bucket}" for bucket in opened} <= publish
    assert not {">", "$JS.API.>", "$KV.>", "$JS.API.STREAM.PURGE.*"} & publish


def _lab_antoncore(tmp_path: Path, permissions: dict[str, Any] | None) -> Path:
    root = _operator_antoncore(tmp_path, {"publish": {"allow": PUBLISH}, "subscribe": {"allow": SUBSCRIBE}})
    path = root / grants.SERVICE_USERS
    users = yaml.safe_load(path.read_text())["users"]
    lab: list[dict[str, Any]] = [] if permissions is None else [{**users[3], "permissions": permissions}]
    path.write_text(yaml.safe_dump({"users": [*users[:3], *lab]}))
    return root


def _unnamed(direction: str, subject: str) -> str:
    return (
        f"::error::{direction} allow {subject} is required by {LAB_FROM} and not named by the antoncore"
        f" {LAB_USER} user ({grants.SERVICE_USERS})."
    )


def _blocked(direction: str, denied: str, subject: str) -> str:
    return (
        f"::error::{direction} deny {denied} on the antoncore {LAB_USER} user ({grants.SERVICE_USERS}) blocks"
        f" {subject}, which the agentihooks lab scheduler needs."
    )


@pytest.mark.feature("lab-scheduler-nats-grants")
def test_the_lab_scheduler_grant_passes_beside_other_subjects_without_running_the_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agentihooks, _ = _matching(tmp_path)
    publish = ["ci.lab.>", *_lab_publish(durable="ci-lab-relay"), *_lab_publish()]
    antoncore = _lab_antoncore(tmp_path, {"publish": {"allow": publish}, "subscribe": {"allow": SUBSCRIBE}})
    assert _run(capsys, agentihooks, antoncore) == (0, MATCH)


@pytest.mark.feature("lab-scheduler-nats-grants")
@pytest.mark.parametrize("lacked", range(3), ids=["info", "next", "ack"])
def test_each_lab_consumer_subject_the_antoncore_user_lacks_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lacked: int
) -> None:
    agentihooks, _ = _matching(tmp_path)
    publish = _lab_publish()
    subject = publish.pop(lacked)
    antoncore = _lab_antoncore(tmp_path, {"publish": {"allow": publish}, "subscribe": {"allow": SUBSCRIBE}})
    assert _run(capsys, agentihooks, antoncore) == (1, [_unnamed("publish", subject)])


@pytest.mark.feature("lab-scheduler-nats-grants")
def test_a_lab_scheduler_user_without_the_reply_inbox_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agentihooks, _ = _matching(tmp_path)
    antoncore = _lab_antoncore(tmp_path, {"publish": {"allow": _lab_publish()}, "subscribe": {"allow": ["ci.lab.>"]}})
    assert _run(capsys, agentihooks, antoncore) == (1, [_unnamed("subscribe", "_INBOX.>")])


@pytest.mark.feature("lab-scheduler-nats-grants")
@pytest.mark.parametrize(
    "stream,durable", [("CI-LAB-HEADS-2", LAB_DURABLE), (LAB_STREAM, "ci-lab-scheduler-2")], ids=["stream", "durable"]
)
def test_a_renamed_lab_stream_or_durable_is_red_until_antoncore_grants_the_new_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], stream: str, durable: str
) -> None:
    agentihooks, _ = _matching(tmp_path)
    _lab_sources(agentihooks, stream, durable)
    renamed = _lab_publish(stream, durable)
    stale = _lab_antoncore(tmp_path / "stale", LAB)
    assert _run(capsys, agentihooks, stale) == (1, [_unnamed("publish", subject) for subject in sorted(renamed)])
    granted = _lab_antoncore(tmp_path / "granted", {"publish": {"allow": renamed}, "subscribe": {"allow": SUBSCRIBE}})
    assert _run(capsys, agentihooks, granted) == (0, MATCH)


@pytest.mark.feature("lab-scheduler-nats-grants")
def test_a_missing_lab_scheduler_user_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agentihooks, _ = _matching(tmp_path)
    assert _run(capsys, agentihooks, _lab_antoncore(tmp_path, None)) == (
        1,
        [f"::error::The swarm NATS grants cannot be read (ValueError: the SWARM account has no {LAB_USER} user)."],
    )


@pytest.mark.feature("lab-scheduler-nats-grants")
@pytest.mark.parametrize("source", list(LAB_SOURCES))
@pytest.mark.parametrize(
    "body",
    ["OTHER = 'x'\n", "{name} = 7\n", "{name} = ''\n", "{name} = 'x'\n{name} = 'x'.upper()\n", "{name}: str\n"],
    ids=["absent", "number", "empty", "computed-last", "unassigned"],
)
def test_a_lab_name_without_a_string_literal_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str, body: str
) -> None:
    agentihooks, antoncore = _matching(tmp_path)
    name = LAB_SOURCES[source]
    (agentihooks / source).write_text(body.replace("{name}", name))
    error = f"{source} does not set {name} to a non-empty string literal"
    assert _run(capsys, agentihooks, antoncore) == (
        1,
        [f"::error::The swarm NATS grants cannot be read (ValueError: {error})."],
    )


@pytest.mark.feature("lab-scheduler-nats-grants")
@pytest.mark.parametrize("source", list(LAB_SOURCES))
@pytest.mark.parametrize("body,error", [(None, "FileNotFoundError"), ("{name} = (\n", "SyntaxError")])
def test_an_unreadable_lab_source_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str, body: str | None, error: str
) -> None:
    agentihooks, antoncore = _matching(tmp_path)
    path = agentihooks / source
    if body is None:
        path.unlink()
    else:
        path.write_text(body.replace("{name}", LAB_SOURCES[source]))
    status, lines = _run(capsys, agentihooks, antoncore)
    assert status == 1
    assert len(lines) == 1
    assert lines[0].startswith(f"::error::The swarm NATS grants cannot be read ({error}: ")
    assert source in lines[0]


@pytest.mark.feature("lab-scheduler-nats-grants")
@pytest.mark.parametrize(
    "source,kept,stream,durable",
    [
        ("scripts/events/nats.py", LAB_STREAM, "renamed", LAB_DURABLE),
        ("scripts/ci_lab/scheduler.py", LAB_DURABLE, LAB_STREAM, "renamed"),
    ],
    ids=["stream", "durable"],
)
def test_the_last_assignment_of_a_lab_name_is_the_one_graded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str, kept: str, stream: str, durable: str
) -> None:
    agentihooks, antoncore = _matching(tmp_path)
    name, path = LAB_SOURCES[source], agentihooks / source
    path.write_text(f"{name} = 'renamed'\n{name}: str = {kept!r}\n")
    assert _run(capsys, agentihooks, antoncore) == (0, MATCH)
    path.write_text(f"{name}: str = {kept!r}\nOTHER = 1\n{name} = 'renamed'\n")
    expected = [_unnamed("publish", subject) for subject in sorted(_lab_publish(stream, durable))]
    assert _run(capsys, agentihooks, antoncore) == (1, expected)


@pytest.mark.feature("lab-scheduler-nats-grants")
def test_an_unrestricted_lab_scheduler_user_is_red(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    agentihooks, _ = _matching(tmp_path)
    publish = [_unnamed("publish", subject) for subject in sorted(_lab_publish())]
    assert _run(capsys, agentihooks, _lab_antoncore(tmp_path, {})) == (
        1,
        [*publish, _unnamed("subscribe", "_INBOX.>")],
    )


@pytest.mark.feature("lab-scheduler-nats-grants")
@pytest.mark.parametrize(
    "direction,denied,blocked",
    [
        ("publish", ">", sorted(_lab_publish())),
        ("publish", "$JS.ACK.>", [f"$JS.ACK.{LAB_HEADS}.>"]),
        ("publish", f"$JS.API.CONSUMER.INFO.{LAB_HEADS}", [f"$JS.API.CONSUMER.INFO.{LAB_HEADS}"]),
        ("publish", f"$JS.API.CONSUMER.*.{LAB_HEADS}", [f"$JS.API.CONSUMER.INFO.{LAB_HEADS}"]),
        ("publish", f"$JS.API.CONSUMER.*.{LAB_STREAM}.other", []),
        ("publish", f"$JS.API.CONSUMER.INFO.{LAB_STREAM}", []),
        ("publish", f"$JS.API.CONSUMER.INFO.{LAB_HEADS}.*", []),
        ("publish", f"$JS.ACK.{LAB_HEADS}", []),
        ("subscribe", "_INBOX.*.>", ["_INBOX.>"]),
        ("subscribe", "ci.lab.>", []),
    ],
    ids=[
        "everything",
        "acks",
        "exact",
        "wildcard",
        "wildcard-other",
        "shorter",
        "longer",
        "ack-prefix",
        "inboxes",
        "unrelated",
    ],
)
def test_a_deny_on_the_lab_scheduler_user_is_red_where_it_overlaps_a_lab_subject(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], direction: str, denied: str, blocked: list[str]
) -> None:
    agentihooks, _ = _matching(tmp_path)
    permissions: dict[str, Any] = {"publish": {"allow": _lab_publish()}, "subscribe": {"allow": SUBSCRIBE}}
    permissions[direction]["deny"] = [denied]
    expected = [_blocked(direction, denied, subject) for subject in blocked]
    outcome = (1, expected) if expected else (0, MATCH)
    assert _run(capsys, agentihooks, _lab_antoncore(tmp_path, permissions)) == outcome


@pytest.mark.feature("lab-scheduler-nats-grants")
def test_the_grant_check_reads_from_this_repository_the_names_the_scheduler_binds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.ci_lab import scheduler
    from scripts.events import nats

    claims = nats_grant.controller_claims()
    controller = {"publish": claims["pub"], "subscribe": claims["sub"]}
    root = _operator_antoncore(tmp_path, {}, user="swarm-other", controller=controller)
    path = root / grants.SERVICE_USERS
    users = yaml.safe_load(path.read_text())["users"]
    users[3]["permissions"] = {
        "publish": {"allow": _lab_publish(nats.LAB_STREAM, scheduler.DURABLE)},
        "subscribe": {"allow": SUBSCRIBE},
    }
    path.write_text(yaml.safe_dump({"users": users}))
    status, lines = _run(capsys, ROOT, root)
    assert status == 0
    assert lines[-1] == MATCH[-1]
