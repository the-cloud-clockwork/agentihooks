import argparse
import ast
import json
import subprocess
import sys
from itertools import zip_longest
from pathlib import Path
from typing import Any, cast

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
LEDGER_USER = "swarm-ledger"
LEDGER_SOURCE = "scripts/ci_hive_nats_grants.py"
# The ledger signs in with the antoncore users file credential, so its grant lives here rather than in nats_grant.
LEDGER_BUCKETS = ("inbox",)
LEDGER_PUBLISH = (
    "swarm.>",
    "legacy.swarm.>",
    "dead.>",
    "$JS.API.INFO",
    "$JS.API.STREAM.INFO.>",
    "$JS.API.STREAM.MSG.GET.*",
    *(f"$JS.API.STREAM.PURGE.KV_{bucket}" for bucket in LEDGER_BUCKETS),
    "$JS.API.CONSUMER.>",
    "$JS.ACK.>",
    *(f"$KV.{bucket}.>" for bucket in LEDGER_BUCKETS),
)
LEDGER_SUBSCRIBE = ("swarm.>", "_INBOX.>")
EVERYTHING = frozenset({">"})
LAB_USER = "swarm-ci-lab-scheduler"
LAB_NAMES = (("scripts/events/nats.py", "LAB_STREAM"), ("scripts/ci_lab/scheduler.py", "DURABLE"))
LAB_SOURCE = ", ".join(f"{source} {name}" for source, name in LAB_NAMES)


def _subjects(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(value)


def _direction(rules: Any) -> dict[str, frozenset[str]]:
    if not isinstance(rules, dict):
        rules = {"allow": rules}
    allow = _subjects(cast("dict[str, Any]", rules).get("allow"))
    return {"allow": allow or EVERYTHING, "deny": _subjects(cast("dict[str, Any]", rules).get("deny"))}


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


def ledger_grants() -> dict[str, dict[str, frozenset[str]]]:
    return {"publish": _direction(list(LEDGER_PUBLISH)), "subscribe": _direction(list(LEDGER_SUBSCRIBE))}


def _bound(node: ast.stmt) -> tuple[list[ast.expr], ast.expr | None]:
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    return [], None


def _rebinds(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.Name):
        return node.id == name and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.alias):
        return node.name == "*" or name in (node.asname, node.name.split(".")[0])
    if isinstance(node, ast.MatchMapping):
        return node.rest == name
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return node.name == name
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and node.name == name


def source_name(agentihooks: str, source: str, name: str) -> str:
    tree = ast.parse((Path(agentihooks) / source).read_text(), source)
    values = [
        value
        for targets, value in map(_bound, tree.body)
        for target in targets
        if isinstance(target, ast.Name) and target.id == name
    ]
    names = [
        value.value
        for value in values
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value
    ]
    if not names or len(names) != len(values) or len(values) != sum(_rebinds(node, name) for node in ast.walk(tree)):
        raise ValueError(f"{source} must set {name} only by top level non-empty string literal assignments")
    return names[-1]


def lab_grants(agentihooks: str) -> dict[str, dict[str, frozenset[str]]]:
    consumer = ".".join(source_name(agentihooks, source, name) for source, name in LAB_NAMES)
    publish = [f"$JS.API.CONSUMER.INFO.{consumer}", f"$JS.API.CONSUMER.MSG.NEXT.{consumer}", f"$JS.ACK.{consumer}.>"]
    return {"publish": _direction(publish), "subscribe": _direction(SHARED_INBOX)}


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
    permissions: dict[str, Any] = user.get("permissions") or {}
    return {
        "publish": _direction(permissions.get("publish")),
        "subscribe": _direction(permissions.get("subscribe")),
    }


def differences(ours: dict[str, Any], theirs: dict[str, Any], credential: str, user: str) -> list[str]:
    lines: list[str] = []
    for direction in ("publish", "subscribe"):
        for rule in ("allow", "deny"):
            mine, other = ours[direction][rule], theirs[direction][rule]
            lines += [
                f"{direction} {rule} {subject} is granted only by the agentihooks {credential}."
                for subject in sorted(mine - other)
            ]
            lines += [
                f"{direction} {rule} {subject} is granted only by the antoncore {user} user ({HIVE_VALUES})."
                for subject in sorted(other - mine)
            ]
    return lines


def overlaps(first: str, second: str) -> bool:
    for left, right in zip_longest(first.split("."), second.split(".")):
        if ">" in (left, right):
            return None not in (left, right)
        if None in (left, right) or (left != right and "*" not in (left, right)):
            return False
    return True


def lab_problems(ours: dict[str, dict[str, frozenset[str]]], theirs: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for direction in ("publish", "subscribe"):
        needed = ours[direction]["allow"]
        lines += [
            f"{direction} allow {subject} is required by the agentihooks lab scheduler ({LAB_SOURCE})"
            f" and not named by the antoncore {LAB_USER} user ({SERVICE_USERS})."
            for subject in sorted(needed - theirs[direction]["allow"])
        ]
        lines += [
            f"{direction} deny {denied} on the antoncore {LAB_USER} user ({SERVICE_USERS}) blocks {subject},"
            " which the agentihooks lab scheduler needs."
            for denied in sorted(theirs[direction]["deny"])
            for subject in sorted(needed)
            if overlaps(denied, subject)
        ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when hive, controller or ledger NATS grants differ from antoncore users."
    )
    parser.add_argument("--agentihooks", required=True)
    parser.add_argument("--antoncore", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        checks = (
            ("hive member", f"member credential ({MEMBER_SOURCE})", HIVE_USER, member_grants(args.agentihooks)),
            (
                "controller",
                f"controller credential ({MEMBER_SOURCE})",
                CONTROLLER_USER,
                controller_grants(args.agentihooks),
            ),
            ("ledger", f"ledger grant ({LEDGER_SOURCE})", LEDGER_USER, ledger_grants()),
        )
        compared = [(*check, user_grants(args.antoncore, check[2])) for check in checks]
        lab = lab_grants(args.agentihooks)
        found = user_grants(args.antoncore, LAB_USER)
        granted = cast("dict[str, Any]", found)  # pragma: no mutate
    except (
        OSError,
        KeyError,
        AttributeError,
        TypeError,
        ValueError,
        SyntaxError,
        yaml.YAMLError,
    ) as exc:
        print(f"::error::The swarm NATS grants cannot be read ({type(exc).__name__}: {exc}).")
        return 1
    retired = [user for _, _, user, _, theirs in compared if theirs is None]
    if retired:
        print(f"The antoncore {HIVE_USER} user is retired, so hives connect only with minted member credentials.")
    active = [
        (label, credential, user, ours, theirs)
        for label, credential, user, ours, theirs in compared
        if theirs is not None
    ]
    lines = [
        line for _, credential, user, ours, theirs in active for line in differences(ours, theirs, credential, user)
    ]
    lines.extend(lab_problems(lab, granted))
    for line in lines:
        print(f"::error::{line}")
    if lines:
        return 1
    for label, _, _, ours, _ in active:
        print(
            f"The {label} NATS grants match antoncore: {len(ours['publish']['allow'])} publish"
            f" and {len(ours['subscribe']['allow'])} subscribe subjects."
        )
    print(
        f"The antoncore {LAB_USER} user grants the lab scheduler's {len(lab['publish']['allow'])} publish"
        f" and {len(lab['subscribe']['allow'])} subscribe subjects."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
