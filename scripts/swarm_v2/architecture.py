import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from scripts.swarm_v2.records import _replayed as _replay
from scripts.swarm_v2.records import _write, digest
from scripts.swarm_v2.runtime.commands import Principal, Role

SCHEMA = "swarm-v2-architecture/1"
INVENTORY_SCHEMA = "swarm-v2-design-inventory/1"
ROLES = ("code_owner", "state_owner", "deployment_owner")
KINDS = frozenset({"dispatcher", "backlog", "service", "worker_component"})
CODING_TASKS = "coding_tasks"
SIGNED = ("proposal", "sha256", "approved_by", "revision", "reason", "key_id")
KEY_ENV = "SWARM_ARCHITECTURE_PUBLIC_KEY"
CARRIES = ("changed_content", CODING_TASKS, "none", "transcripts")
DECLARE = (
    f"a proposal must declare carries as one of {', '.join(CARRIES)}, launches_agents as true or false,"
    " and a non-empty authoritative_state"
)
RECORD = "docs/swarm-v2/architecture.json"
MARKDOWN = "docs/swarm-v2/decisions.md"


class ArchitectureError(ValueError):
    pass


_replayed = partial(_replay, error=ArchitectureError)


def _load(path: Path | str, schema: str) -> dict:
    data = json.loads(Path(path).read_text())
    if data.get("schema") != schema:
        raise ArchitectureError(f"{path} is not a {schema} document")
    return data


def load_record(path: Path | str) -> dict:
    return _load(path, SCHEMA)


def load_inventory(path: Path | str) -> dict:
    data = _load(path, INVENTORY_SCHEMA)
    ids = [p["id"] for p in data["proposals"]]
    if len(set(ids)) != len(ids):
        raise ArchitectureError("proposal ids must be unique")
    return data


def missing_owner(record: dict, component: dict) -> str | None:
    return next((role for role in ROLES if component.get(role) not in record["owners"]), None)


def dispatches(component: dict) -> bool:
    return (
        component.get("kind") == "dispatcher"
        or component.get("carries") == CODING_TASKS
        or component.get("launches_agents") is True
    )


def _signed(change: dict) -> bytes:
    return json.dumps({"schema": SCHEMA, **{name: change.get(name) for name in SIGNED}}, sort_keys=True).encode()


def key_id(key: Ed25519PublicKey) -> str:
    return hashlib.sha256(key.public_bytes(Encoding.Raw, PublicFormat.Raw)).hexdigest()[:16]


def _authentic(key: Ed25519PublicKey | None, change: dict) -> bool:
    signature = change.get("signature")
    if key is None or change.get("key_id") != key_id(key) or not isinstance(signature, str):
        return False
    try:
        key.verify(bytes.fromhex(signature), _signed(change))
    except (InvalidSignature, ValueError):
        return False
    return True


def _changes(record: dict, key: Ed25519PublicKey | None) -> list[dict]:
    return [c for c in record["operator_changes"] if _authentic(key, c)]


def approved(record: dict, proposal: dict, key: Ed25519PublicKey | None = None) -> bool:
    return any(c["proposal"] == proposal["id"] and c["sha256"] == digest(proposal) for c in _changes(record, key))


def authorities(record: dict, key: Ed25519PublicKey | None = None) -> list[str]:
    changed = {c["proposal"] for c in _changes(record, key)}
    return [c["name"] for c in record["components"] if dispatches(c) and c.get("proposal") not in changed]


def approve(
    path: Path | str,
    proposal: dict,
    reason: str,
    *,
    slug: str,
    credential: str,
    authenticate: Callable[[str, str], Principal | None],
    signer: Ed25519PrivateKey,
) -> dict:
    principal = authenticate(slug, credential)
    if (
        not isinstance(principal, Principal)
        or principal.role is not Role.OPERATOR
        or not isinstance(principal.name, str)
        or not principal.name
    ):
        raise ArchitectureError("authenticated operator required for an architecture change")
    record = load_record(path)
    sha256 = digest(proposal)
    key = signer.public_key()
    done = next((c for c in _changes(record, key) if c["proposal"] == proposal["id"] and c["sha256"] == sha256), None)
    if done:
        return done
    change = {
        "proposal": proposal["id"],
        "sha256": sha256,
        "approved_by": principal.name,
        "revision": record["revision"],
        "reason": reason,
        "key_id": key_id(key),
    }
    change["signature"] = signer.sign(_signed(change)).hex()
    record["operator_changes"].append(change)
    _write(path, record)
    return change


def _declared(proposal: dict) -> bool:
    state = proposal.get("authoritative_state")
    return (
        proposal.get("carries") in CARRIES
        and isinstance(proposal.get("launches_agents"), bool)
        and isinstance(state, str)
        and bool(state.strip())
    )


def _excluded(record: dict, component: dict) -> bool:
    excluded = {name.casefold() for name in record["worker_excluded"]}
    return component.get("kind") == "worker_component" and component["name"].casefold() in excluded


def _conflict(record: dict, proposal: dict, repeated: set[str]) -> str:
    if proposal["id"] in repeated:
        return f"conflicting proposals for {proposal['name']}"
    name, state = proposal["name"].casefold(), proposal["authoritative_state"].casefold()
    for c in record["components"]:
        if c["name"].casefold() == name:
            return f"{c['name']} is already recorded; changing it needs an operator architecture change"
        if c["authoritative_state"].casefold() == state:
            return (
                f"{c['name']} already owns this authoritative state; sharing it needs an operator architecture change"
            )
    return ""


def _verdict(record: dict, proposal: dict, repeated: set[str], key: Ed25519PublicKey | None) -> tuple[str, str]:
    kind = proposal.get("kind")
    role = missing_owner(record, proposal)
    if kind not in KINDS:
        return "rejected", f"unknown kind {kind!r}"
    if not _declared(proposal):
        return "rejected", DECLARE
    if role:
        return "rejected", f"{role} must name exactly one owner from the record"
    if _excluded(record, proposal):
        return "rejected", f"the worker image excludes {proposal['name']} (AD-06)"
    if dispatches(proposal) and not approved(record, proposal, key):
        return "rejected", f"inserts another coding-task queue beside {', '.join(authorities(record, key))} (AD-05)"
    if kind == "backlog" and not (proposal.get("bounded") is True and proposal["carries"] in record["backlog_carries"]):
        return "rejected", f"a backlog must be bounded and carry one of {', '.join(record['backlog_carries'])} (AD-05)"
    if kind == "worker_component" and proposal["name"].casefold() not in {
        n.casefold() for n in record["worker_permitted"]
    }:
        return "unresolved", f"{proposal['name']} is not a permitted worker image component (AD-06)"
    conflict = _conflict(record, proposal, repeated)
    return ("unresolved", conflict) if conflict else ("accepted", "")


def _repeated(proposals: list[dict]) -> set[str]:
    declared = [p for p in proposals if _declared(p)]
    names = Counter(p["name"].casefold() for p in declared)
    states = Counter(p["authoritative_state"].casefold() for p in declared)
    return {
        p["id"] for p in declared if names[p["name"].casefold()] > 1 or states[p["authoritative_state"].casefold()] > 1
    }


def review(record: dict, inventory: dict, key: Ed25519PublicKey | None = None) -> dict:
    repeated = _repeated(inventory["proposals"])
    result = {
        "operation": inventory["operation"],
        "revision": record["revision"],
        "accepted": [],
        "rejected": [],
        "unresolved": [],
    }
    for proposal in inventory["proposals"]:
        outcome, reason = _verdict(record, proposal, repeated, key)
        if outcome == "accepted":
            result["accepted"].append(proposal["id"])
        else:
            result[outcome].append({"id": proposal["id"], "name": proposal["name"], "reason": reason})
    unowned = [c["name"] for c in [*record["components"], *inventory["proposals"]] if missing_owner(record, c)]
    return {**result, "unowned": unowned, "measurements": {"architecture_unowned_components": len(unowned)}}


def check(record: dict, key: Ed25519PublicKey | None = None) -> list[str]:
    components = record["components"]
    errors = [
        f"{c['name']}: {missing_owner(record, c)} must name exactly one owner from the record"
        for c in components
        if missing_owner(record, c)
    ]
    names = [c["name"] for c in components]
    errors += [f"{n} is recorded more than once" for n in dict.fromkeys(n for n in names if names.count(n) > 1)]
    errors += [f"{c['name']} is excluded from the worker image" for c in components if _excluded(record, c)]
    count = len(authorities(record, key))
    if count != 1:
        errors.append(f"expected one coding-task authority, found {count}")
    return errors


def _commit(path: Path | str, record: dict, operation: str, sha256: str, result: dict) -> dict:
    revision = record["revision"] + 1
    components = list(record["components"])
    record["operations"].append(
        {"id": operation, "revision": revision, "sha256": sha256, "result": result, "components": components}
    )
    record["revision"] = revision
    _write(path, record)
    return result


def apply_inventory(path: Path | str, inventory: dict, key: Ed25519PublicKey | None = None) -> dict:
    record = load_record(path)
    operation, sha256 = inventory["operation"], digest(inventory)
    done = _replayed(record, operation, sha256)
    if done:
        return done["result"]
    if inventory["base_revision"] != record["revision"]:
        raise ArchitectureError(
            f"inventory is based on revision {inventory['base_revision']}; the record is at revision {record['revision']}"
        )
    result = review(record, inventory, key)
    revision = record["revision"] + 1
    proposals = {p["id"]: p for p in inventory["proposals"]}
    for pid in result["accepted"]:
        fields = {k: v for k, v in proposals[pid].items() if k != "id"}
        record["components"].append({**fields, "proposal": pid, "added_in": revision})
    for outcome in ("rejected", "unresolved"):
        record[outcome] += [{**item, "operation": operation, "revision": revision} for item in result[outcome]]
    return _commit(path, record, operation, sha256, result)


def _accepted_at(record: dict, revision: int) -> list[dict]:
    if revision == 1:
        return [c for c in record["components"] if "proposal" not in c]
    return list(next(o["components"] for o in record["operations"] if o["revision"] == revision))


def rollback(path: Path | str, to_revision: int, operation: str) -> dict:
    record = load_record(path)
    sha256 = digest({"rollback_to": to_revision})
    done = _replayed(record, operation, sha256)
    if done:
        return done["result"]
    if not 1 <= to_revision < record["revision"]:
        raise ArchitectureError(f"rollback target {to_revision} is not an earlier revision of {record['revision']}")
    revision = record["revision"] + 1
    current = record["components"]
    restored = _accepted_at(record, to_revision)
    removed = [c for c in current if c not in restored]
    record["components"] = restored
    record["rejected"] += [
        {
            "id": c["proposal"],
            "name": c["name"],
            "reason": f"rolled back to revision {to_revision}",
            "operation": operation,
            "revision": revision,
        }
        for c in removed
    ]
    result = {
        "operation": operation,
        "revision": record["revision"],
        "rolled_back": [c["name"] for c in removed],
        "restored": [c["name"] for c in restored if c not in current],
    }
    return _commit(path, record, operation, sha256, result)


def render(record: dict, signing: Ed25519PublicKey | None = None) -> str:
    lines = [
        "# Swarm v2 architecture decisions",
        "",
        f"Package SV2-FND-02, record revision {record['revision']}. Generated from `{RECORD}` by"
        " `python -m scripts.swarm_v2.architecture render`; edit the record, never this file.",
        "",
        "## Components",
        "",
        "| Component | Kind | Code owner | State owner | Deployment owner | Authoritative state |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {c['name']} | {c['kind']} | {c['code_owner']} | {c['state_owner']} | {c['deployment_owner']} |"
        f" {c['authoritative_state']} |"
        for c in record["components"]
    ]
    for d in record["decisions"]:
        lines += ["", f"## {d['id']}: {d['title']}", "", f"Status: {d['status']}.", "", d["decision"], ""]
        lines += [f"Why: {d['rationale']}", "", "Rejected alternatives:", ""]
        lines += [f"- {a['alternative']}: {a['reason']}" for a in d["rejected_alternatives"]]
    for title, key in (
        ("Permitted worker image components", "worker_permitted"),
        ("Worker image exclusions", "worker_excluded"),
    ):
        lines += ["", f"## {title}", "", *[f"- {name}" for name in record[key]]]
    lines.append("")
    changes = [
        f"{c['proposal']} by {c['approved_by']} at revision {c['revision']} ({c['reason']})"
        for c in _changes(record, signing)
    ]
    lines.append(f"Operator architecture changes: {', '.join(changes) or 'none'}.")
    for title, key in (("Unresolved decisions", "unresolved"), ("Rejected proposals", "rejected")):
        entries = [f"- {e['name']} (`{e['id']}`, revision {e['revision']}): {e['reason']}" for e in record[key]]
        lines += ["", f"## {title}", "", *(entries or ["None."])]
    return "\n".join(lines) + "\n"


def _run(args, key: Ed25519PublicKey | None) -> int:
    if args.command == "check":
        errors = check(load_record(args.record), key)
        print("\n".join(errors) or "ok")
        return 1 if errors else 0
    if args.command == "review":
        print(json.dumps(review(load_record(args.record), load_inventory(args.inventory), key), indent=2))
        return 0
    if args.command == "record":
        print(json.dumps(apply_inventory(args.record, load_inventory(args.inventory), key), indent=2))
    elif args.command == "rollback":
        print(json.dumps(rollback(args.record, args.to, args.operation), indent=2))
    Path(args.markdown).write_text(render(load_record(args.record), key))
    return 0


def verify_key(environ: Mapping[str, str]) -> Ed25519PublicKey | None:
    raw = environ.get(KEY_ENV)
    if not raw:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(raw))
    except ValueError:
        raise ArchitectureError(f"{KEY_ENV} must be a hex Ed25519 public key") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.architecture")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("review", "record", "rollback", "render", "check"):
        command = commands.add_parser(name)
        command.add_argument("--record", default=RECORD)
        if name in ("review", "record"):
            command.add_argument("--inventory", required=True)
        if name in ("record", "rollback", "render"):
            command.add_argument("--markdown", default=MARKDOWN)
        if name == "rollback":
            command.add_argument("--to", type=int, required=True)
            command.add_argument("--operation", required=True)
    args = parser.parse_args(argv)
    try:
        return _run(args, verify_key(os.environ))
    except ArchitectureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
