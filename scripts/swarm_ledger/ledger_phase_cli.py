import sys


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
