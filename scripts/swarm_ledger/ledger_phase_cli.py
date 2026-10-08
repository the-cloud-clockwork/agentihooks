import sys

from scripts.swarm_ledger import ledger_phases


def operation(args) -> tuple[str, dict]:
    if args.id == "add":
        return "phase_add", {
            "phase": args.state,
            "title": " ".join(args.values),
            "description": args.description,
            "depends_on": comma_list(args.depends_on),
            "planning": args.planning,
            "release": args.release,
        }
    fields = dict(value.split("=", 1) for value in args.values if "=" in value)
    if len(fields) != len(args.values):
        sys.exit("phase set takes FIELD=VALUE pairs")
    if "depends_on" in fields:
        fields["depends_on"] = comma_list(fields["depends_on"])
    if "release" in fields:
        if fields["release"] not in ("true", "false"):
            sys.exit("release must be true or false")
        fields["release"] = fields["release"] == "true"
    return "phase_update", {"item": f"phases/{args.state}", "fields": fields}


def comma_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def append_phases(plan, taken: list[str]) -> list[dict]:
    entries = plan.get("phases") if isinstance(plan, dict) else None
    if not isinstance(entries, list) or not entries:
        sys.exit("the plan file needs a nonempty phases list")
    if not all(isinstance(entry, dict) for entry in entries):
        sys.exit("each plan phase is an object")
    used = set(taken) | {entry["id"] for entry in entries if entry.get("id")}
    start = max((int(i[1:]) for i in taken if i[:1] == "p" and i[1:].isdigit()), default=0) + 1
    free = (f"p{n}" for n in range(start, start + len(used) + len(entries)) if f"p{n}" not in used)
    ids = [entry.get("id") or next(free) for entry in entries]
    return [phase_entry(position, entry, ids) for position, entry in enumerate(entries, 1)]


def phase_entry(position: int, entry: dict, ids: list[str]) -> dict:
    values, depends_on = entry.get("depends_on", []), []
    if not isinstance(values, list):
        sys.exit(f"phase {position} depends_on must be a list")
    for value in values:
        if type(value) is int and not 1 <= value <= len(ids):
            sys.exit(f"phase {position} depends on position {value}, outside the plan")
        depends_on.append(ids[value - 1] if type(value) is int else value)
    phase = {
        "phase": ids[position - 1],
        "title": entry.get("title", ""),
        "description": entry.get("description", ""),
        "depends_on": depends_on,
        "planning": entry.get("planning", ledger_phases.PLANNING_DEFAULT),
    }
    if "release" in entry:
        phase["release"] = entry["release"]
    return phase
