"""Agent-side ops: crew membership, acknowledgement, claims, phase and follow-up state.

Every op carries `by`, the agent's name; the server records it so the gate can attribute work.
"""

import re

import ledger_comments

AUTHOR_RE = re.compile(r"^[A-Za-z][\w.@-]{0,63}$")
ROLES = ("orchestrator", "member")
ITEM_PATH_RE = re.compile(r"^(phases|questions|followups)/[^/]+$")
STATE_PATH_RE = re.compile(
    r"^((phases|followups)/[^/]+/done|(phases|questions|followups|tasks)/[^/]+/out_of_scope|followups/[^/]+/needs_operator)$"
)
FLAG_EVENTS = ("needs the operator", "no longer needs the operator")
TEXT_ITEM_RE = re.compile(r"^(questions|followups)/[^/]+$")
GATE_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
MAX_TEXT = 20000


def check(op, task_ids=()):
    by = op.get("by")
    if not isinstance(by, str) or not AUTHOR_RE.match(by) or by == "operator":
        raise ValueError("agent ops need `by`, an agent name other than operator")
    kind = op["op"]
    if kind == "join" and op.get("role", "member") not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if kind == "ack" and not isinstance(op.get("rev"), int):
        raise ValueError("ack needs an integer rev")
    if kind == "claim" and not ITEM_PATH_RE.match(str(op.get("item"))):
        raise ValueError("claim needs item phases/<id>, questions/<id> or followups/<id>")
    if kind == "set":
        if op.get("path") == "time_left_minutes":
            if type(op.get("value")) is not int or op["value"] < 0:
                raise ValueError("time left needs nonnegative whole minutes")
        elif not STATE_PATH_RE.match(str(op.get("path"))) or not isinstance(op.get("value"), bool):
            raise ValueError("set needs path <list>/<id>/done or <list>/<id>/out_of_scope and a boolean value")
    if kind == "add_item" and (
        op.get("list") not in ("followups", "questions") or not _text(op.get("text")) or "/" in op["id"]
    ):
        raise ValueError("add_item needs list followups|questions, text and an id without '/'")
    if (
        kind == "add_item"
        and "needs_operator" in op
        and (op["list"] != "followups" or not isinstance(op["needs_operator"], bool))
    ):
        raise ValueError("needs_operator is a boolean flag on a follow-up")
    if kind == "retext" and (not TEXT_ITEM_RE.match(str(op.get("item"))) or not _text(op.get("text"))):
        raise ValueError("retext needs item questions/<id> or followups/<id> and text")
    if kind in ("add_item", "retext"):
        op["text"] = _screened(op["text"])
        ledger_comments.check(op["text"], "item", task_ids=task_ids)
    if kind == "gate_bypass" and not isinstance(op.get("unhandled"), int):
        raise ValueError("gate_bypass needs an integer unhandled")
    if kind == "gate_lift" and not GATE_RE.match(str(op.get("gate"))):
        raise ValueError("gate_lift needs the gate's name")
    if "status" in op:
        if not _text(op["status"]):
            raise ValueError(f"status must be text up to {MAX_TEXT} characters")
        op["status"] = _screened(op["status"])
        ledger_comments.check(op["status"], "comment", task_ids=task_ids)


def _screened(text):
    from hooks.filters import check as filters

    return filters.screen("ledger_write", text)


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT


def touch(meta, by, at):
    member = meta.get("members", {}).get(by)
    if member is not None:
        member["last_seen"] = at


def _join(doc, op, ctx):
    members = ctx.meta["members"]
    known = members.get(op["by"])
    role = op.get("role", known["role"] if known else "member")
    members[op["by"]] = {
        "role": role,
        "joined_at": known["joined_at"] if known else ctx.at,
        "last_seen": ctx.at,
        "claims": known["claims"] if known else [],
        "handled_rev": known["handled_rev"] if known else ctx.rev - 1,
    }
    # Join evidence survives membership and event expiry for the ledger lifetime.
    starts = ctx.meta.setdefault("join_history", {}).setdefault(op["by"], [])
    joined = members[op["by"]]["joined_at"]
    if joined not in starts:
        starts.append(joined)
    ctx.record(op["by"], "joined", "", role=role)
    return True


def _leave(doc, op, ctx):
    if ctx.meta["members"].pop(op["by"], None) is None:
        return False
    ctx.record(op["by"], "left", "")
    return True


def _ack(doc, op, ctx):
    member = ctx.meta["members"].get(op["by"])
    if member is None:
        return False
    member["handled_rev"] = max(member["handled_rev"], min(op["rev"], ctx.rev - 1))
    if member.get("role") == "orchestrator":
        _answer_stats(ctx, op["by"], member["handled_rev"])
    ctx.dirty = True
    return True


def _answer_stats(ctx, by, rev):
    events = ctx.meta["events"]
    sent = next((e for e in reversed(events) if e.get("kind") == "stats sync requested"), None)
    if sent is None or sent["rev"] > rev:
        return
    if not any(e.get("kind") == "stats check answered" and e.get("id") == sent["id"] for e in events):
        ctx.record(by, "stats check answered", "", id=sent["id"])


def _item(doc, path):
    name, item_id = path.split("/")[:2]
    return next((i for i in doc.get(name, []) if i["id"] == item_id), None)


def _claim(doc, op, ctx):
    member = ctx.meta["members"].get(op["by"])
    if member is None or _item(doc, op["item"]) is None:
        return False
    if op["item"] not in member["claims"]:
        member["claims"].append(op["item"])
        ctx.record(op["by"], "claimed", op["item"])
    return True


def _set(doc, op, ctx):
    if op["path"] == "time_left_minutes":
        if doc.get("time_left_minutes") != op["value"]:
            doc["time_left_minutes"] = op["value"]
            ctx.stamp("time_left_minutes", op["by"])
            ctx.record(op["by"], "time left changed", "time_left_minutes", text=f"{op['value']}m")
        return True
    import ledger_core as core

    item = _item(doc, op["path"])
    if item is None:
        return False
    target, field = "/".join(op["path"].split("/")[:2]), op["path"].rsplit("/", 1)[1]
    if item.get(field, False) != op["value"]:
        if field == "needs_operator":
            item[field] = op["value"]
            kind = FLAG_EVENTS[0 if op["value"] else 1]
        else:
            core.set_state(item, field, op["value"])
            kind = core.state_event(field, op["value"])
        ctx.stamp(op["path"], op["by"])
        ctx.record(op["by"], kind, target)
    if op.get("status"):
        ledger_comments.post_status(item["comments"], op["by"], f"{op['id']}-st", op["status"], ctx, target)
    return True


def _add_item(doc, op, ctx):
    items = doc.setdefault(op["list"], [])
    if any(i["id"] == op["id"] for i in items):
        return True
    item = {"id": op["id"], "text": op["text"], "comments": []}
    item.update({"done": False} if op["list"] == "followups" else {"answers": []})
    if op.get("needs_operator"):
        item["needs_operator"] = True
    items.append(item)
    ctx.record(op["by"], "added", f"{op['list']}/{op['id']}", text=op["text"])
    return True


def _retext(doc, op, ctx):
    import ledger_core as core

    item = _item(doc, op["item"])
    if item is None:
        return False
    if item["text"] != op["text"]:
        ctx.record(op["by"], "text changed", op["item"], diff=core.text_diff(item["text"], op["text"]))
        item["text"] = op["text"]
        ctx.stamp(f"{op['item']}/text", op["by"])
    return True


def _gate_bypass(doc, op, ctx):
    ctx.record(op["by"], "gate bypassed", "", text=f"stopped with {op['unhandled']} unhandled operator events")
    return True


def _gate_lift(doc, op, ctx):
    ctx.record(
        op["by"], "gate lifted", "", text=f"the operator lifted the {op['gate']} gate for one hour", gate=op["gate"]
    )
    return True


HANDLERS = {
    "join": _join,
    "leave": _leave,
    "ack": _ack,
    "claim": _claim,
    "set": _set,
    "add_item": _add_item,
    "retext": _retext,
    "gate_bypass": _gate_bypass,
    "gate_lift": _gate_lift,
}


def apply(doc, op, ctx):
    done = HANDLERS[op["op"]](doc, op, ctx)
    if done and op["op"] != "leave":
        touch(ctx.meta, op["by"], ctx.at)
    return done
