#!/usr/bin/env python3
"""List a ledger's priorities with their item state, open ones grouped, resolved ones apart.

Usage: list_priorities.py SLUG [--clear-resolved] [--skip ID...] [--as NAME]

Reads the ledger through `agentihooks ledger --slug SLUG show`. Prints JSON {"open": [...], "resolved": [...]}.
--clear-resolved clears every resolved priority through `agentihooks ledger priority clear`.
--skip leaves out the priorities the operator already decided this run.
"""

import argparse
import json
import os
import subprocess
import sys

# The last comments carry the item's current ask; older ones are history.
RECENT = 3
# Group labels double as AskUserQuestion headers, which take at most 12 characters.
GROUP_ORDER = ("questions", "approvals", "blocked", "follow ups", "phases", "tasks")


def load(slug):
    try:
        done = subprocess.run(["agentihooks", "ledger", "--slug", slug, "show"], capture_output=True, text=True)
        if done.returncode:
            raise ValueError((done.stderr or done.stdout).strip())
        return json.loads(done.stdout)
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read ledger {slug}: {exc}. Check the slug with: agentihooks ledger list")


def find(doc, item):
    name, _, item_id = item.partition("/")
    return name, next((i for i in doc.get(name, []) if i.get("id") == item_id), None)


def still_derived(name, item):
    if name == "tasks":
        return item.get("state") == "blocked" or (item.get("state") == "pr" and item.get("awaiting") == "approval")
    return bool(item.get("needs_operator")) if name == "followups" else True


def resolution(row, name, item):
    if item is None:
        return "item missing"
    if item.get("out_of_scope"):
        return "out of scope"
    if name == "questions":
        return "answered" if any(not a.get("deleted") for a in item.get("answers", [])) else None
    if item.get("done") or item.get("state") == "done":
        return "done"
    return "no longer waiting" if row.get("derived") and not still_derived(name, item) else None


def group(name, item):
    if name == "tasks" and item.get("state") == "pr" and item.get("awaiting") == "approval":
        return "approvals"
    if name == "tasks" and item.get("state") == "blocked":
        return "blocked"
    return {"questions": "questions", "followups": "follow ups", "phases": "phases"}.get(name, "tasks")


def recent(item):
    said = [c for c in item.get("comments", []) if not c.get("deleted") and c.get("text", "").strip()]
    return [{"by": c.get("by", ""), "text": c["text"]} for c in said[-RECENT:]]


def triage(doc, skip=()):
    found = {"open": [], "resolved": []}
    for row in doc.get("priorities", []):
        if row["id"] in skip:
            continue
        name, item = find(doc, row["item"])
        why = resolution(row, name, item)
        if why:
            found["resolved"].append({"priority": row["id"], "item": row["item"], "why": why})
            continue
        found["open"].append(
            {
                "priority": row["id"],
                "item": row["item"],
                "group": group(name, item),
                "ask": row.get("text", ""),
                "item_text": item.get("text") or item.get("title", ""),
                "state": item.get("state", ""),
                "recent": recent(item),
            }
        )
    found["open"].sort(key=lambda p: GROUP_ORDER.index(p["group"]))
    return found


def ledger_cmd(slug, name, *args):
    return ["agentihooks", "ledger", "--slug", slug, "--as", name, *args]


def agent_name(given):
    name = given or os.environ.get("AGENTIHOOKS_AGENT_NAME", "")
    if not name:
        sys.exit("no agent name: pass --as NAME or run inside an agent session that sets AGENTIHOOKS_AGENT_NAME")
    return name


def run(main):
    """Exit 2 with the message on stderr for a refusal raised as a string, else main's own code."""
    try:
        sys.exit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            sys.exit(2)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug")
    parser.add_argument("--clear-resolved", action="store_true")
    parser.add_argument("--skip", nargs="*", default=[])
    parser.add_argument("--as", dest="name", default="")
    args = parser.parse_args(argv)
    found = triage(load(args.slug), set(args.skip))
    ids = [p["priority"] for p in found["resolved"]]
    if args.clear_resolved and ids:
        cleared = ledger_cmd(args.slug, agent_name(args.name), "priority", "clear", *ids)
        done = subprocess.run(cleared, capture_output=True, text=True)
        if done.returncode:
            sys.exit(
                f"clearing resolved priorities {', '.join(ids)} failed: {(done.stderr or done.stdout).strip()}. "
                "Run the same command again; the ledger server restarts itself on the next call."
            )
    print(json.dumps(found, indent=1))
    return 0


if __name__ == "__main__":
    run(main)
