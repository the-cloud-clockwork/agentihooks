import argparse
import ast
import sys
from pathlib import Path

import yaml

MEMBER_SOURCE = "scripts/hive/nats_permissions.py"
HIVE_VALUES = "k8s/values-overlays/agentihooks-swarm-nats/values.yaml"
ACCOUNT = "SWARM"
HIVE_USER = "swarm-hive"
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


def member_grants(agentihooks: Path) -> dict[str, dict[str, frozenset[str]]]:
    tree = ast.parse((agentihooks / MEMBER_SOURCE).read_text())
    member = next(
        (
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "MEMBER" for target in node.targets)
        ),
        None,
    )
    if member is None:
        raise ValueError(f"{MEMBER_SOURCE} assigns no MEMBER grants")
    claims = ast.literal_eval(member)
    return {"publish": _direction(claims.get("pub")), "subscribe": _direction(claims.get("sub"))}


def hive_grants(antoncore: Path) -> dict[str, dict[str, frozenset[str]]]:
    values = yaml.safe_load((antoncore / HIVE_VALUES).read_text())
    users = values["config"]["merge"]["accounts"][ACCOUNT]["users"]
    hive = next((user for user in users if user["user"] == HIVE_USER), None)
    if hive is None:
        raise ValueError(f"the {ACCOUNT} account has no {HIVE_USER} user")
    permissions = hive.get("permissions") or {}
    return {
        "publish": _direction(permissions.get("publish")),
        "subscribe": _direction(permissions.get("subscribe")),
    }


def differences(member: dict, hive: dict) -> list[str]:
    lines = []
    for direction in ("publish", "subscribe"):
        for rule in ("allow", "deny"):
            ours, theirs = member[direction][rule], hive[direction][rule]
            lines += [
                f"{direction} {rule} {subject} is granted only by the agentihooks member credential ({MEMBER_SOURCE})."
                for subject in sorted(ours - theirs)
            ]
            lines += [
                f"{direction} {rule} {subject} is granted only by the antoncore {HIVE_USER} user ({HIVE_VALUES})."
                for subject in sorted(theirs - ours)
            ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when hive member NATS grants differ from the antoncore hive user."
    )
    parser.add_argument("--agentihooks", type=Path, default=Path.cwd())
    parser.add_argument("--antoncore", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        member, hive = member_grants(args.agentihooks), hive_grants(args.antoncore)
    except (
        OSError,
        KeyError,
        AttributeError,
        TypeError,
        ValueError,
        SyntaxError,
        yaml.YAMLError,
    ) as exc:
        print(f"::error::The hive member NATS grants cannot be read ({type(exc).__name__}: {exc}).")
        return 1
    lines = differences(member, hive)
    for line in lines:
        print(f"::error::{line}")
    if lines:
        return 1
    print(
        f"The hive member NATS grants match antoncore: {len(member['publish']['allow'])} publish"
        f" and {len(member['subscribe']['allow'])} subscribe subjects."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
