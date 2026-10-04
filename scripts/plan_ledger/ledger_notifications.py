"""Page notifications: the server derives them from what agents did; only the operator clears them."""

OPS = ("notification_clear",)
NEW_ITEM = {"questions": "New open question", "followups": "New follow-up"}
REPLY_KINDS = {"comment added": "comments", "message added": "chat"}


def check(op):
    if set(op) != {"op", "id", "target"} or not isinstance(op["target"], str) or not op["target"]:
        raise ValueError("notification_clear is the operator's and takes only id and target: a notification id or all")


def apply(doc, op, ctx):
    rows = doc.setdefault("notifications", [])
    kept = [] if op["target"] == "all" else [r for r in rows if r["id"] != op["target"]]
    if len(kept) != len(rows):
        doc["notifications"] = kept
        ctx.dirty = True
    return True


def _thread(doc, kind, target):
    if REPLY_KINDS[kind] == "chat":
        return doc.get("chat", [])
    name, _, item_id = target.partition("/")
    item = next((i for i in doc.get(name, []) if i.get("id") == item_id), None)
    return item.get("comments", []) if item else []


def _answers_operator(doc, event):
    thread = _thread(doc, event["kind"], event.get("target", ""))
    ids = [e.get("id") for e in thread]
    if event.get("id") not in ids:
        return False
    before = [e for e in thread[: ids.index(event["id"])] if not e.get("deleted")]
    return bool(before) and before[-1].get("by") == "operator"


def _from_event(doc, event):
    kind, target = event.get("kind"), event.get("target", "")
    if event.get("by") in (None, "operator"):
        return None
    if kind == "added" and target.split("/")[0] in NEW_ITEM:
        return {"item": target, "label": NEW_ITEM[target.split("/")[0]]}
    if kind in REPLY_KINDS and _answers_operator(doc, event):
        return {"item": target or "chat", "label": "Reply"}
    return None


def derive(doc, ctx):
    rows = doc.setdefault("notifications", [])
    for n, event in enumerate(ctx.events):
        found = _from_event(doc, event)
        if found:
            rows.append(
                {"id": f"nt-{ctx.rev}-{n}", **found, "text": event.get("text", ""), "by": event["by"], "at": ctx.at}
            )
