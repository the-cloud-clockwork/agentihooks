"""Alerts: the server raises one from each new ledger warning and sends it to its target's inbox; anyone claims it
and closes it done with an outcome."""

from scripts.inbox import outbox

OPS = ("alert_claim", "alert_close", "alert_raise")
SIZE, SYNC, PROMOTION, RELEASE = "size", "sync", "promotion", "release"
SEAT, SPAWN, MERGE = "seat", "spawn", "merge"
INBOX = "inbox"
WRITER = "writer"
TARGETS = {
    WRITER: "master",
    SIZE: "master",
    SYNC: "master",
    PROMOTION: "master",
    RELEASE: "master",
    SEAT: "master",
    SPAWN: "master",
    MERGE: "master",
    INBOX: "master",
}
RAISED = (PROMOTION, RELEASE, SEAT, SPAWN, MERGE, INBOX)
MULTI = (INBOX,)
OPEN, CLAIMED, DONE = "open", "claimed", "done"
OPERATOR = "operator"
SENDER = "ledger"
DONE_KEPT = 200
MAX_OUTCOME = 2000
SENT_TTL_S = 30 * 24 * 3600


def check_raise(op):
    by, text = op.get("by"), op.get("text")
    if (
        set(op) != {"op", "id", "by", "source", "text"}
        or op["source"] not in RAISED
        or not isinstance(by, str)
        or not by.strip()
        or not isinstance(text, str)
        or not text.strip()
        or len(text) > MAX_OUTCOME
    ):
        raise ValueError(f"alert_raise takes by, a source among {RAISED} and a text of up to {MAX_OUTCOME} characters")


def raise_source(doc, ctx, source, text):
    def matches(a):
        return a["source"] == source and (source not in MULTI or a["text"] == text)

    if not any(matches(a) and a["state"] != DONE for a in doc.setdefault("alerts", [])):
        raise_warning(doc, ctx, (source, text), [])
    return True


def check(op):
    if op["op"] == "alert_raise":
        return check_raise(op)
    allowed = {"op", "id", "target", "by", *(("outcome",) if op["op"] == "alert_close" else ())}
    if set(op) - allowed or not isinstance(op.get("target"), str) or not op["target"]:
        raise ValueError(f"{op['op']} takes target, an alert id, and an optional by")
    if "by" in op and (not isinstance(op["by"], str) or not op["by"].strip()):
        raise ValueError(f"{op['op']} by must be a name")
    if op["op"] == "alert_close":
        outcome = op.get("outcome")
        if not isinstance(outcome, str) or not outcome.strip() or len(outcome) > MAX_OUTCOME:
            raise ValueError(f"alert_close needs an outcome of up to {MAX_OUTCOME} characters")


def apply(doc, op, ctx):
    if op["op"] == "alert_raise":
        return raise_source(doc, ctx, op["source"], op["text"].strip())
    alert = next((a for a in doc.setdefault("alerts", []) if a["id"] == op["target"]), None)
    if alert is None or alert["state"] == DONE:
        return False
    who, target = op.get("by", OPERATOR), f"alerts/{alert['id']}"
    if op["op"] == "alert_claim":
        if alert["state"] == CLAIMED:
            return alert["claimed_by"] == who
        alert.update(state=CLAIMED, claimed_by=who, claimed_at=ctx.at)
        ctx.record(who, "alert claimed", target, text=alert["text"])
        return True
    outcome = op["outcome"].strip()
    alert.update(state=DONE, closed_by=who, closed_at=ctx.at, outcome=outcome)
    ctx.record(who, "alert closed", target, text=outcome)
    return True


def item(op: dict) -> str:
    if "task" in op:
        return f"tasks/{op['task']}"
    return op.get("item") or op.get("thread") or op.get("target") or op["op"]


def writer(op: dict, ctx: object) -> str | None:
    return ctx.author(op["by"]) if op.get("by") else None


def close_refusal(alert: dict, ctx: object, outcome: str) -> None:
    who = alert.get("writer") or SENDER
    alert.update(state=DONE, closed_by=who, closed_at=ctx.at, outcome=outcome)
    ctx.record(who, "alert closed", f"alerts/{alert['id']}", text=outcome)


def recovered(doc: dict, op: dict, ctx: object) -> None:
    by, target_item = writer(op, ctx), item(op)
    for alert in doc.get("alerts", []):
        if (
            alert["state"] != DONE
            and alert["source"] == SYNC
            and alert.get("writer") == by
            and alert.get("item") == target_item
        ):
            close_refusal(alert, ctx, "The writer succeeded on the same item.")


def expired(alert: dict, at: int) -> bool:
    return (
        alert.get("source") == SYNC
        and alert["state"] != DONE
        and at - alert.get("last_refused_at", alert["at"]) >= 3600000
    )


def raise_warning(doc: dict, ctx: object, warning: tuple, before: list) -> None:
    source, text, *owner = warning
    writer, target_item = owner if owner else (None, None)
    rows = doc["alerts"]
    matching = [
        a
        for a in rows
        if a["text"] == text and (source != SYNC or (a.get("writer"), a.get("item")) == (writer, target_item))
    ]
    live = next((a for a in matching if a["state"] != DONE), None)
    if live:
        if source == SYNC and owner:
            live["last_refused_at"] = ctx.at
            ctx.dirty = True
        return
    if matching and text in before and not owner:
        return
    alert = {
        "id": f"al-{ctx.rev}-{len(rows)}",
        "text": text,
        "source": source,
        "target": writer if writer and writer != OPERATOR else TARGETS[source],
        "state": OPEN,
        "at": ctx.at,
        "rev": ctx.rev,
    }
    if source == SYNC:
        alert.update(writer=writer, item=target_item, last_refused_at=ctx.at)
    rows.append(alert)
    ctx.dirty = True


def derive(doc, ctx, raised, before):
    rows = doc.setdefault("alerts", [])
    for warning in raised:
        raise_warning(doc, ctx, warning, before)
    for alert in rows:
        if expired(alert, ctx.at):
            close_refusal(alert, ctx, "No repeat refusal for one hour.")
    done = [a for a in rows if a["state"] == DONE]
    gone = {id(a) for a in done[: max(0, len(done) - DONE_KEPT)]}
    if gone:
        doc["alerts"] = [a for a in rows if id(a) not in gone]


def message(slug, alert):
    return (
        f"On ledger {slug}: alert {alert['id']} ({alert['source']}): {alert['text']}. Claim it with agentihooks "
        f"ledger --slug {slug} --as <you> alert claim {alert['id']}, then close it with alert close "
        f'{alert["id"]} "<outcome>".'
    )


def deliver(inbox, slug, alerts, rev, master, since=None):
    """Send each alert raised from since through rev to its target's inbox, once."""
    since = rev if since is None else since
    sent = []
    for alert in alerts:
        if alert["state"] != OPEN or not since <= alert.get("rev", -1) <= rev:
            continue
        mark = inbox.key("alert-sent", slug, alert["id"])
        if inbox.redis.exists(mark):
            continue
        address = master if alert["target"] in ("master", OPERATOR) else alert["target"]
        notice = outbox.Post(SENDER, address, message(slug, alert), item=outbox.stable_id("alert", slug, alert["id"]))
        item = outbox.post(inbox, notice)
        inbox.redis.set(mark, 1, ex=SENT_TTL_S)
        sent += [item] if item else []
    return sent
