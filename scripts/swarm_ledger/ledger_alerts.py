"""Alerts: the server raises one from each new ledger warning and sends it to its target's inbox; anyone claims it
and closes it done with an outcome."""

OPS = ("alert_claim", "alert_close")
SIZE, SYNC = "size", "sync"
TARGETS = {SIZE: "master", SYNC: "operator"}
OPEN, CLAIMED, DONE = "open", "claimed", "done"
OPERATOR = "operator"
SENDER = "ledger"
DONE_KEPT = 200
MAX_OUTCOME = 2000
SENT_TTL_S = 30 * 24 * 3600


def check(op):
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


def derive(doc, ctx, raised, before):
    """raised is (source, text) for each warning this sync found; before is the warnings of the previous sync.

    A text already open or claimed raises nothing, and one closed done raises again only after its warning cleared.
    """
    rows = doc.setdefault("alerts", [])
    live = {a["text"] for a in rows if a["state"] != DONE}
    known = {a["text"] for a in rows}
    for n, (source, text) in enumerate(raised):
        if text in live or (text in before and text in known):
            continue
        alert = {
            "id": f"al-{ctx.rev}-{n}",
            "text": text,
            "source": source,
            "target": TARGETS[source],
            "state": OPEN,
            "at": ctx.at,
            "rev": ctx.rev,
        }
        rows.append(alert)
        live.add(text)
        ctx.dirty = True
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


def deliver(inbox, slug, alerts, rev, master):
    """Send each alert raised at rev to its target's inbox, once."""
    sent = []
    for alert in alerts:
        if alert.get("rev") != rev or alert["state"] != OPEN:
            continue
        if not inbox.redis.set(inbox.key("alert-sent", slug, alert["id"]), 1, nx=True, ex=SENT_TTL_S):
            continue
        address = master if alert["target"] == "master" else OPERATOR
        sent.append(inbox.send(SENDER, address, message(slug, alert)))
    return sent
