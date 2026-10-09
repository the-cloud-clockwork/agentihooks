"""The tick step that groups small open tasks so one agent delivers them in one pull request."""

import math

from hooks.classifier import ClassifierError, YesNo, decide
from scripts.inbox.store import InboxStore
from scripts.swarm import difficulty
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import Mail
from scripts.swarm_ledger import ledger_groups, ledger_rank

PER_TICK = 3
MIN_CONFIDENCE = 0.6
APPLIES = ("delegate", "full")
PURPOSE = "task-grouping"
SEEN = "groupings"
CONFIRM = "Tasks {tasks}: is this one change surface that one pull request, one review and one browser check cover?"
TRUE = "one change surface, one review and one browser check cover every task"
FALSE = "the tasks need separate changes, reviews or browser checks"
PROPOSE = "Group {n} small tasks into one pull request led by this task."
ASK = (
    "The swarm proposes one pull request for tasks {lead} and {members}, led by {lead}. If the operator agrees, apply "
    "it with agentihooks ledger --slug {slug} --as <your name> task group {lead} {members_args}."
)
RELEASE_CLOSED = "closed without its pull request"
RELEASE_BLOCKED = "is blocked and its agent let it go"
RELEASE_REOPENED = "was reopened"
TOLD = "For your information: the swarm grouped tasks {members} under task {lead}, whose agent delivers all of them in one pull request."


def group_pass(slug, config, store, ledger, doc):
    seen = store.key(slug, SEEN)
    fresh = [g for g in candidates(doc) if not store.redis.hexists(seen, key(g))][:PER_TICK]
    if not fresh:
        return []
    verdicts = confirm(fresh, doc)
    if verdicts is None:
        return []
    mail, actions = Mail(InboxStore(store.redis), store, slug), []
    for group, yes in zip(fresh, verdicts):
        if not yes:
            store.redis.hset(seen, key(group), "declined")
            continue
        try:
            actions += (
                _apply(slug, ledger, mail, group) if config.autonomy in APPLIES else _propose(slug, ledger, mail, group)
            )
        except LedgerRefused:
            actions.append(f"skipped grouping under task {group[0]['id']}: the ledger refused its write")
            store.redis.hset(seen, key(group), "refused")
            continue
        store.redis.hset(seen, key(group), "applied" if config.autonomy in APPLIES else "proposed")
    return actions


def release_pass(slug, store, ledger, doc):
    known = {t["id"]: t for t in doc["tasks"]}
    actions = []
    for lead in [t for t in known.values() if t.get("group_members")]:
        why = _stopped(slug, store, lead, doc)
        if not why:
            continue
        try:
            ledger.ungroup_tasks(slug, lead["id"])
        except LedgerRefused:
            actions.append(f"skipped releasing the group under task {lead['id']}: the ledger refused its write")
            continue
        released = [m for m in lead.pop("group_members") if known.get(m, {}).get("merged_into") == lead["id"]]
        for member in released:
            del known[member]["merged_into"]
        actions.append(f"released tasks {', '.join(released)} from task {lead['id']}: its lead {why}")
    return actions


def _stopped(slug, store, lead, doc):
    state = lead["state"]
    if lead.get("out_of_scope") or (state == "done" and not lead.get("pr_url")):
        return RELEASE_CLOSED
    if state == "blocked" and not (lead.get("claimed_by") and store.claimant(slug, lead["id"]) == lead["claimed_by"]):
        return RELEASE_BLOCKED
    if state == "open" and _reopened(lead, doc) and not store.handoff(slug, lead["id"]):
        return RELEASE_REOPENED
    return ""


def _reopened(lead, doc):
    stamps, item = doc.get("_meta", {}).get("stamps", {}), f"tasks/{lead['id']}"
    grouped_at, changed_at = stamps.get(f"{item}/group_members"), stamps.get(f"{item}/state")
    return bool(grouped_at and changed_at) and changed_at["rev"] > grouped_at["rev"]


def candidates(doc: dict) -> list[list[dict]]:
    known = {t["id"]: t for t in doc["tasks"]}
    pool = sorted((t for t in known.values() if _eligible(t, known)), key=ledger_rank.order)
    used, groups = set(), []
    for lead in pool:
        if lead["id"] in used:
            continue
        group = [lead]
        for other in pool:
            if len(group) == ledger_groups.MAX_TASKS:
                break
            if other["id"] in used or other is lead or not _near(lead, other):
                continue
            if not ledger_groups.refusal([*group, other], known):
                group.append(other)
        if len(group) > 1:
            groups.append(group)
            used.update(t["id"] for t in group)
    return groups


def confirm(groups, doc):
    questions = {
        f"group_{i}": YesNo(CONFIRM.format(tasks=_named(g)), true=TRUE, false=FALSE) for i, g in enumerate(groups)
    }
    try:
        answers = decide(state(groups, doc), questions, purpose=PURPOSE).answers
    except ClassifierError:
        return None
    return [_yes(answers[f"group_{i}"].noul) for i in range(len(groups))]


def state(groups, doc):
    return {
        "overview": doc["overview"],
        "groups": [
            [
                {
                    "id": t["id"],
                    "title": t["title"],
                    "description": t["description"],
                    "difficulty": t["difficulty"],
                    "territory": list(t["territory"]),
                }
                for t in group
            ]
            for group in groups
        ],
    }


def key(group):
    return ",".join(sorted(t["id"] for t in group))


def _apply(slug, ledger, mail, group):
    lead, members = group[0]["id"], [t["id"] for t in group[1:]]
    ledger.group_tasks(slug, lead, members)
    group[0]["group_members"] = members
    for member in group[1:]:
        member["merged_into"] = lead
    mail.send(f"grouped:{lead}", mail.master, TOLD.format(lead=lead, members=", ".join(members)), fyi=True)
    return [f"grouped tasks {', '.join(members)} under {lead}"]


def _propose(slug, ledger, mail, group):
    lead, members = group[0]["id"], [t["id"] for t in group[1:]]
    text = ASK.format(lead=lead, members=", ".join(members), slug=slug, members_args=" ".join(members))
    mail.send(f"group-proposed:{key(group)}", mail.master, text)
    if ledger.priority(slug, f"tasks/{lead}", PROPOSE.format(n=len(group))) is False:
        raise LedgerRefused(f"the proposal under task {lead} was dropped")
    return [f"proposed grouping tasks {', '.join(members)} under {lead}"]


def _eligible(task, known):
    return not task.get("out_of_scope") and not ledger_groups.refusal([task], known)


def _near(lead, other):
    from scripts.swarm.tick import _overlaps

    mine, theirs = lead.get("territory") or [], other.get("territory") or []
    if not mine and not theirs:
        return lead.get("phase") and lead.get("phase") == other.get("phase")
    return _overlaps(mine, theirs) or (_on_page(mine) and _on_page(theirs))


def _on_page(territory):
    return bool(territory) and all(difficulty.on_page(str(area)) for area in territory)


def _named(group):
    return ", ".join(f"{t['id']} titled {t['title']}" for t in group)


def _yes(noul):
    return (
        isinstance(noul, (int, float))
        and not isinstance(noul, bool)
        and not math.isnan(noul)
        and noul >= MIN_CONFIDENCE
    )
