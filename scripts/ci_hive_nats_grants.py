import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

MEMBER_SOURCE = "scripts/hive/nats_grant.py"
MEMBER_KEY = "UMEMBER"


def _render(call: str) -> str:
    return (
        "import json, sys; sys.path.insert(0, sys.argv[1]); from scripts.hive import nats_grant;"
        f" print(json.dumps({call}))"
    )


RENDER_MEMBER = _render(f"nats_grant.member_claims({MEMBER_KEY!r})")
RENDER_CONTROLLER = _render("nats_grant.controller_claims()")
# The shared swarm-hive password user cannot carry a per member reply prefix, so its _INBOX.> stands for this one.
MEMBER_INBOX = f"_INBOX.{MEMBER_KEY}.>"
SHARED_INBOX = "_INBOX.>"
HIVE_VALUES = "k8s/values-overlays/agentihooks-swarm-nats/values.yaml"
SERVICE_USERS = "k8s/values-overlays/agentihooks-swarm-nats/users.yaml"
ACCOUNT = "SWARM"
HIVE_USER = "swarm-hive"
CONTROLLER_USER = "swarm-controller"
EVERYTHING = frozenset({">"})


def _subjects(value) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(value)


def _direction(rules) -> dict[str, frozenset[str]]:
    if not isinstance(rules, dict):
        rules = {"allow": rules}
    allow = _subjects(rules.get("allow"))
    return {"allow": allow or EVERYTHING, "deny": _subjects(rules.get("deny"))}


def _rendered(agentihooks: str, script: str, function: str) -> dict[str, dict[str, frozenset[str]]]:
    rendered = subprocess.run([sys.executable, "-c", script, agentihooks], capture_output=True, text=True)
    if rendered.returncode:
        raise ValueError(f"{MEMBER_SOURCE} did not render {function}: {rendered.stderr.strip()}")
    claims = json.loads(rendered.stdout)
    return {"publish": _direction(claims.get("pub")), "subscribe": _direction(claims.get("sub"))}


def member_grants(agentihooks: str) -> dict[str, dict[str, frozenset[str]]]:
    grants = _rendered(agentihooks, RENDER_MEMBER, "member_claims")
    subscribe = grants["subscribe"]
    subscribe["allow"] = frozenset(
        SHARED_INBOX if subject == MEMBER_INBOX else subject for subject in subscribe["allow"]
    )
    return grants


def controller_grants(agentihooks: str) -> dict[str, dict[str, frozenset[str]]]:
    return _rendered(agentihooks, RENDER_CONTROLLER, "controller_claims")


def user_grants(antoncore: Path, name: str) -> dict[str, dict[str, frozenset[str]]] | None:
    values = yaml.safe_load((antoncore / HIVE_VALUES).read_text())
    accounts = values["config"]["merge"].get("accounts")
    if accounts is None:
        users = yaml.safe_load((antoncore / SERVICE_USERS).read_text())["users"]
    else:
        users = accounts[ACCOUNT]["users"]
    user = next((user for user in users if user["user"] == name), None)
    if user is None and accounts is None and name == HIVE_USER:
        return None
    if user is None:
        raise ValueError(f"the {ACCOUNT} account has no {name} user")
    permissions = user.get("permissions") or {}
    return {
        "publish": _direction(permissions.get("publish")),
        "subscribe": _direction(permissions.get("subscribe")),
    }


def differences(ours: dict, theirs: dict, credential: str, user: str) -> list[str]:
    lines = []
    for direction in ("publish", "subscribe"):
        for rule in ("allow", "deny"):
            mine, other = ours[direction][rule], theirs[direction][rule]
            lines += [
                f"{direction} {rule} {subject} is granted only by the agentihooks {credential} credential"
                f" ({MEMBER_SOURCE})."
                for subject in sorted(mine - other)
            ]
            lines += [
                f"{direction} {rule} {subject} is granted only by the antoncore {user} user ({HIVE_VALUES})."
                for subject in sorted(other - mine)
            ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when hive or controller NATS grants differ from antoncore users."
    )
    parser.add_argument("--agentihooks", required=True)
    parser.add_argument("--antoncore", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        checks = (
            ("hive member", "member", HIVE_USER, member_grants(args.agentihooks)),
            ("controller", "controller", CONTROLLER_USER, controller_grants(args.agentihooks)),
        )
        compared = [(*check, user_grants(args.antoncore, check[2])) for check in checks]
    except (
        OSError,
        KeyError,
        AttributeError,
        TypeError,
        ValueError,
        yaml.YAMLError,
    ) as exc:
        print(f"::error::The swarm NATS grants cannot be read ({type(exc).__name__}: {exc}).")
        return 1
    retired = [user for _, _, user, _, theirs in compared if theirs is None]
    if retired:
        print(f"The antoncore {HIVE_USER} user is retired, so hives connect only with minted member credentials.")
    compared = [check for check in compared if check[2] not in retired]
    lines = [
        line for _, credential, user, ours, theirs in compared for line in differences(ours, theirs, credential, user)
    ]
    for line in lines:
        print(f"::error::{line}")
    if lines:
        return 1
    for label, _, _, ours, _ in compared:
        print(
            f"The {label} NATS grants match antoncore: {len(ours['publish']['allow'])} publish"
            f" and {len(ours['subscribe']['allow'])} subscribe subjects."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
