"""Swarm tasks: claimable units of work under a phase, each in the eng or ci lane."""

import re

import ledger_comments

AUTHOR_RE = re.compile(r"^[A-Za-z][\w.-]{0,63}$")
ID_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
ITEM_RE = re.compile(r"^tasks/[^/]+$")
LANES = ("eng", "ci")
STATES = ("open", "claimed", "blocked", "pr", "done")
UPDATABLE = ("state", "claimed_by", "issue_url", "pr_url")
URL_FIELDS = ("issue_url", "pr_url")
URL_RE = re.compile(r"^https?://[^\s]+$")
OPS = ("task_add", "task_update")


def check(op):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError("task ops need `by`, an agent name other than operator")
    if op["op"] == "task_add":
        if (
            not isinstance(op.get("task"), str)
            or not ID_RE.match(op["task"])
            or not isinstance(op.get("title"), str)
            or not op["title"].strip()
        ):
            raise ValueError("task_add needs task <id> and title")
        if op.get("lane") not in LANES:
            raise ValueError(f"lane must be one of {LANES}")
        if not isinstance(op.get("phase", ""), str) or not isinstance(op.get("description", ""), str):
            raise ValueError("phase and description must be strings")
        ledger_comments.check(op["title"], "item")
        return
    fields = op.get("fields")
    if not ITEM_RE.match(str(op.get("item"))) or not isinstance(fields, dict) or not fields:
        raise ValueError("task_update needs item tasks/<id> and fields")
    if set(fields) - set(UPDATABLE) or not all(isinstance(v, str) for v in fields.values()):
        raise ValueError(f"task_update may set only {UPDATABLE}, as strings")
    if "state" in fields and fields["state"] not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    check_urls(fields)


def check_urls(fields):
    for key in URL_FIELDS:
        if fields.get(key) and not URL_RE.match(fields[key]):
            raise ValueError(f"{key} must be an http or https link")


def check_task(task):
    """Rules a task obeys however it arrives: op, seed edit or new ledger."""
    if task.get("lane", "eng") not in LANES:
        raise ValueError(f"tasks/{task.get('id')}/lane must be one of {LANES}")
    if task.get("state", "open") not in STATES:
        raise ValueError(f"tasks/{task.get('id')}/state must be one of {STATES}")
    check_urls(task)


def _add(doc, op, ctx):
    tasks = doc.setdefault("tasks", [])
    if any(t["id"] == op["task"] for t in tasks):
        return True
    task = {
        "id": op["task"],
        "title": op["title"].strip(),
        "description": op.get("description", ""),
        "phase": op.get("phase", ""),
        "lane": op["lane"],
        "state": "open",
        "claimed_by": "",
        "issue_url": "",
        "pr_url": "",
        "done": False,
        "comments": [],
    }
    tasks.append(task)
    ctx.record(op["by"], "added", f"tasks/{task['id']}", text=task["title"])
    return True


def _update(doc, op, ctx):
    task_id = op["item"].split("/")[1]
    task = next((t for t in doc.get("tasks", []) if t["id"] == task_id), None)
    if task is None:
        return False
    changed = {k: v for k, v in op["fields"].items() if task.get(k) != v}
    task.update(changed)
    if "state" in changed:
        task["done"] = changed["state"] == "done"
        ctx.record(op["by"], f"task {changed['state']}", op["item"])
    for key in changed:
        ctx.stamp(f"{op['item']}/{key}", op["by"])
    ctx.dirty = ctx.dirty or bool(changed)
    return True


def apply(doc, op, ctx):
    return _add(doc, op, ctx) if op["op"] == "task_add" else _update(doc, op, ctx)
