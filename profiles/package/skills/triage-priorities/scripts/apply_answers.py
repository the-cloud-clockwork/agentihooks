#!/usr/bin/env python3
"""Apply the operator's triage decisions to a ledger, then clear each handled priority.

Usage: apply_answers.py SLUG PLAN.json [--as NAME]

PLAN.json is a list of entries, one per priority:
  {"priority": ID, "action": "relay", "text": T, "quote": Q}          the operator's decision, relayed as the operator's
  {"priority": ID, "action": "followup-done", "text": T, "quote": Q}  relay the decision, then close the follow up
  {"priority": ID, "action": "followup-done"[, "text": T]}            close the follow up, T as the master's status
  {"priority": ID, "action": "task", "fields": {"state": S}}          change the task's fields
  {"priority": ID, "action": "comment", "text": T}                    the master's own comment on the item
  {"priority": ID, "action": "keep"}                                  still waits on the operator; left listed

The whole plan is validated before anything runs. Prints JSON {"applied": [...]}; exits 1 when an
action failed (its priority stays listed), 2 when the plan is invalid (nothing ran).
"""

import argparse
import json
import subprocess

from list_priorities import agent_name, ledger_cmd, load, run

NEEDS = {
    "relay": ("text", "quote"),
    "comment": ("text",),
    "followup-done": (),
    "task": (),
    "keep": (),
}
LISTS = {"followup-done": "followups/", "task": "tasks/"}


def entry_problems(entry, rows):
    if not isinstance(entry, dict):
        return [f"entry {entry!r} is not an object"]
    action, priority = entry.get("action"), entry.get("priority")
    if not isinstance(priority, str) or priority not in rows:
        return [f"priority {priority!r} is not on the ledger; listed: {', '.join(rows)}"]
    if action not in NEEDS:
        return [f"{priority}: action {action!r} is not one of {', '.join(NEEDS)}"]
    found = [f"{priority}: {action} needs {f} as text" for f in NEEDS[action] if not _text(entry.get(f))]
    if action == "followup-done" and "quote" in entry and not (_text(entry.get("quote")) and _text(entry.get("text"))):
        found.append(f"{priority}: a relayed followup-done needs text and quote as text")
    if action == "task" and not (isinstance(entry.get("fields"), dict) and entry["fields"]):
        found.append(f'{priority}: task needs fields as an object, e.g. {{"state": "open"}}')
    if action in LISTS and not rows[priority]["item"].startswith(LISTS[action]):
        found.append(f"{priority}: {action} applies to {LISTS[action]} items, not {rows[priority]['item']}")
    return found


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def problems(plan, rows):
    if not isinstance(plan, list):
        return ["the plan must be a list of entries"]
    found = [p for entry in plan for p in entry_problems(entry, rows)]
    seen = [e["priority"] for e in plan if isinstance(e, dict) and isinstance(e.get("priority"), str)]
    found += [f"{p}: listed more than once" for p in sorted({p for p in seen if seen.count(p) > 1})]
    return found


def commands(entry, item):
    action, item_id = entry["action"], item.split("/", 1)[1]
    relay = ["relay", item, entry.get("text", ""), "--quote", entry.get("quote", "")]
    if action == "relay":
        return [relay]
    if action == "comment":
        return [["comment", item, entry["text"]]]
    if action == "followup-done":
        if entry.get("quote"):
            return [relay, ["followup", "done", item_id]]
        return [["followup", "done", item_id, *(["--status", entry["text"]] if entry.get("text") else [])]]
    return [["task", "set", item_id, *(f"{k}={v}" for k, v in entry["fields"].items())]]


def refusal(done):
    """Why a ledger call failed: a non zero exit, or a comment the server answered with posted false."""
    if done.returncode:
        return (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
    try:
        reply = json.loads(done.stdout)
    except ValueError:
        return ""
    return "the ledger refused it" if isinstance(reply, dict) and reply.get("posted") is False else ""


def apply(slug, name, plan, rows):
    applied = []
    for entry in plan:
        result = {"priority": entry["priority"], "action": entry["action"], "ok": True}
        if entry["action"] != "keep":
            for args in commands(entry, rows[entry["priority"]]["item"]):
                error = refusal(subprocess.run(ledger_cmd(slug, name, *args), capture_output=True, text=True))
                if error:
                    result.update(ok=False, error=f"{args[0]}: {error}")
                    break
        applied.append(result)
    return applied


def clear(slug, name, applied):
    """Clear handled priorities still listed; a relayed answer or the swarm tick may drop one first."""
    listed = {p["id"] for p in load(slug).get("priorities", [])}
    ids = [r["priority"] for r in applied if r["ok"] and r["action"] != "keep" and r["priority"] in listed]
    if ids:
        done = subprocess.run(ledger_cmd(slug, name, "priority", "clear", *ids), capture_output=True, text=True)
        left = {p["id"] for p in load(slug).get("priorities", [])} if done.returncode else set()
        for r in (r for r in applied if r["priority"] in ids and r["priority"] in left):
            r.update(ok=False, error="applied, but clearing its priority failed: " + done.stderr.strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug")
    parser.add_argument("plan")
    parser.add_argument("--as", dest="name", default="")
    args = parser.parse_args(argv)
    name = agent_name(args.name)
    try:
        with open(args.plan, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read plan {args.plan}: {exc}") from exc
    rows = {p["id"]: p for p in load(args.slug).get("priorities", [])}
    found = problems(plan, rows)
    if found:
        raise SystemExit("plan refused, nothing applied; fix each line and run again:\n" + "\n".join(found))
    applied = apply(args.slug, name, plan, rows)
    clear(args.slug, name, applied)
    print(json.dumps({"applied": applied}, indent=1))
    return 0 if all(r["ok"] for r in applied) else 1


if __name__ == "__main__":
    run(main)
