"""Who owes the operator a reaction: routes operator events to crew members.

Pure functions over a ledger's `_meta` and document; the CLI, the server and the hook share them.
"""

import re

DEFAULT_POLICY = {"nudge_after_calls": 25, "stop_after_calls": 10, "stop_blocks": 3}
MENTION_RE = re.compile(r"^@([A-Za-z][\w.@-]{0,63})")
IGNORED_KINDS = ("chat cleared", "artifact deleted", "artifact restored", "alert claimed", "alert closed")
WRITE_COMMANDS = (
    "say",
    "comment",
    "phase",
    "followup",
    "claim",
    "alert",
    "ack",
    "join",
    "edit",
    "delete",
    "scope",
    "retext",
    "task",
    "relay",
    "answer",
)
WATCH_STALE_SECONDS = 20


def policy(doc):
    given = doc.get("policy") if isinstance(doc.get("policy"), dict) else {}
    return {**DEFAULT_POLICY, **{k: v for k, v in given.items() if k in DEFAULT_POLICY and isinstance(v, int)}}


def orchestrator(members):
    return next((name for name, member in members.items() if member.get("role") == "orchestrator"), None)


def owner(event, members, tasks=()):
    boss = orchestrator(members)
    target = event.get("target", "")
    if target == "chat" or target.startswith("notes/"):
        from scripts.swarm.naming import resolve_name

        mention = MENTION_RE.match(event.get("note_text", event.get("text", "")))
        named = resolve_name(mention.group(1)) if mention else None
        return named if named in members else boss
    claimer = next((t.get("claimed_by") for t in tasks if f"tasks/{t.get('id')}" == target), None)
    if claimer in members:
        return claimer
    for name, member in members.items():
        if target in member.get("claims", []):
            return name
    return boss


def owes(event, members, name, tasks=()):
    from scripts.swarm.naming import resolve_name

    name = resolve_name(name)
    who = owner(event, members, tasks)
    return event.get("kind") == "sync requested" or who == name or who is None


def unhandled_for(meta, name, tasks=(), owners=None):
    from scripts.swarm.naming import resolve_name

    return pending_for(meta, resolve_name(name), operator_events(meta), tasks, {} if owners is None else owners)


def operator_events(meta):
    return [
        (index, event)
        for index, event in enumerate(meta.get("events", []))
        if event.get("by") == "operator" and event.get("kind") not in IGNORED_KINDS
    ]


def pending_for(meta, name, events, tasks, owners):
    members = meta.get("members", {})
    me = members.get(name)
    if me is None:
        return []
    since = me.get("handled_rev", 0)
    mine = []
    for index, event in events:
        if event.get("rev", 0) <= since:
            continue
        if index not in owners:
            owners[index] = owner(event, members, tasks)
        if event.get("kind") == "sync requested" or owners[index] in (name, None):
            mine.append(event)
    return mine


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def sync_summary(doc, meta):
    """The operator's sync order: what is pending, then what every member must do before acking."""
    since = min((m.get("handled_rev", 0) for m in meta.get("members", {}).values()), default=0)
    told = sum(
        1
        for e in meta.get("events", [])
        if e.get("rev", 0) > since
        and e.get("by") == "operator"
        and e.get("kind") not in (*IGNORED_KINDS, "sync requested")
    )
    live = lambda name: [i for i in doc.get(name, []) if not i.get("out_of_scope")]  # noqa: E731
    phases = sum(1 for i in live("phases") if not i.get("done"))
    followups = sum(1 for i in live("followups") if not i.get("done"))
    questions = sum(1 for i in live("questions") if not any(not a.get("deleted") for a in i.get("answers", [])))
    return (
        f"Operator sync. Since the crew last synced: {plural(told, 'operator message')}. Open now: "
        f"{plural(phases, 'phase')} open, {plural(followups, 'follow-up')} open, {plural(questions, 'question')} "
        "unanswered. Re-read the whole ledger, act on everything you have not handled, update every phase, "
        "follow-up, status and the time left, then ack."
    )


def crew(meta):
    from scripts.swarm.naming import resolve_names

    members = meta.get("members", {})
    names = resolve_names(list(members))
    events = operator_events(meta)
    owners = {}
    return [
        {
            "name": name,
            "role": member.get("role", "member"),
            "last_seen": member.get("last_seen", 0),
            "handled_rev": member.get("handled_rev", 0),
            "unhandled": len(pending_for(meta, names[name], events, (), owners)),
        }
        for name, member in members.items()
    ]


def closed(doc):
    items = [*doc.get("phases", []), *doc.get("followups", [])]
    return bool(doc.get("phases")) and all(item.get("done") or item.get("out_of_scope") for item in items)
