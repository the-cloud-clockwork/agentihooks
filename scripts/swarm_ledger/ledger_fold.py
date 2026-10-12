"""Folds open tasks that change the same code into one grouped task, so one pull request delivers them."""

import json
import re
import sys
import uuid
from pathlib import Path

from scripts.swarm_ledger import ledger_groups, ledger_kinds, ledger_rank, ledger_stats

NUMBERS = ("One", "Two", "Three", "Four", "Five")
# Titles sharing this share of their significant words read as one change.
TITLE_OVERLAP = 0.6
# Words this short or this common say nothing about which code a task changes.
SHORT_WORD = 3
COMMON = frozenset(
    "the and for with from into that this when then than each every task tasks make makes add adds "
    "so it its on in of to a an or not no be is are by as at".split()
)
NAME_RES = (
    re.compile(r"`([^`\n]{3,80})`"),
    re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"),
    re.compile(r"\b[\w-]+\.(?:py|sh|js|ts|md|json|ya?ml|toml|css|html)\b"),
    re.compile(r"\bagentihooks [a-z][\w-]*(?: [a-z][\w-]*)?"),
)
PATH_RE = re.compile(r"\S+/\S+|\b[\w-]+\.(?:py|sh|js|ts|md|json|ya?ml|toml|css|html)\b")
DASHES = ("—", "–", " - ", "--")
FALLBACK_STATUS = "Folded into a grouped task; its agent delivers this change in the same pull request."
STATUS = "Folded into the task titled {title}; its agent delivers this change in the same pull request."
DONE = "one merged pull request delivers every numbered change above"
CARD_KEYS = ("id", "title", "description", "territory", "difficulty", "lane", "profile", "phase", "depends_on")


def pool(doc: dict) -> tuple[list[dict], dict]:
    pending = {
        t["id"]: t for t in ledger_stats.live(doc, "tasks") if not ledger_stats.finished(t) and not t.get("merged_into")
    }
    inputs = ledger_stats.tick_inputs(doc.get("_meta") or {}) or {}
    held = ledger_stats.frozen(doc, pending, inputs)
    chained = _chained(pending)
    kept, skipped = [], {"frozen": 0, "in flight": 0, "planner chain": 0, "grouped": 0, "not code": 0}
    for tid, task in pending.items():
        if reason := _left_out(task, tid in held, tid in chained):
            skipped[reason] += 1
        else:
            kept.append(task)
    return sorted(kept, key=ledger_rank.order), skipped


def _left_out(task: dict, held: bool, chained: bool) -> str:
    if held:
        return "frozen"
    if task.get("state") != "open" or task.get("claimed_by"):
        return "in flight"
    if task.get("group_members"):
        return "grouped"
    if ledger_kinds.kind(task) not in ledger_groups.GROUPED_KINDS:
        return "not code"
    return "planner chain" if chained else ""


def _chained(pending: dict) -> set:
    sliced = {tid for tid, t in pending.items() if t.get("slice") or t.get("plan_slice")}
    linked = set()
    for tid in sliced:
        if upstream := sliced & set(pending[tid].get("depends_on") or []):
            linked |= upstream | {tid}
    return linked


def names(task: dict) -> set:
    text = f"{task.get('title', '')}\n{task.get('description', '')}"
    return {m.strip().lower() for pattern in NAME_RES for m in pattern.findall(text)}


def words(title: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > SHORT_WORD and w not in COMMON}


def signals(a: dict, b: dict) -> list[str]:
    found = []
    if shared := sorted(set(a.get("territory") or []) & set(b.get("territory") or [])):
        found.append(f"territory {', '.join(shared)}")
    if shared := sorted(names(a) & names(b)):
        found.append(f"names {', '.join(shared)}")
    mine, theirs = words(a.get("title", "")), words(b.get("title", ""))
    if mine and theirs and len(mine & theirs) / min(len(mine), len(theirs)) >= TITLE_OVERLAP:
        found.append(f"titles share {', '.join(sorted(mine & theirs))}")
    return found


def candidates(doc: dict) -> dict:
    tasks, skipped = pool(doc)
    known = {t["id"]: t for t in doc["tasks"]}
    used, groups = set(), []
    for lead in tasks:
        if lead["id"] in used:
            continue
        group, links = [lead], []
        for other in tasks:
            if len(group) == ledger_groups.MAX_TASKS:
                break
            if other["id"] in used or other is lead:
                continue
            found = [(t["id"], s) for t in group for s in signals(t, other)]
            if found and not ledger_groups.refusal([*group, other], known, ledger_groups.FOLD_CEILING):
                group.append(other)
                links += [{"between": [tid, other["id"]], "signal": s} for tid, s in found]
        if len(group) > 1:
            groups.append({"tasks": [_card(t) for t in group], "signals": links})
            used.update(t["id"] for t in group)
    return {"pool": len(tasks), "skipped": skipped, "groups": groups}


def _card(task: dict) -> dict:
    return {key: task.get(key) for key in CARD_KEYS} | {"kind": ledger_kinds.kind(task)}


def problems(doc: dict, plan: dict) -> list[str]:
    groups = plan.get("groups") if isinstance(plan, dict) else None
    if not isinstance(groups, list) or not groups:
        return ['the plan needs {"groups": [{"lead", "members", "seams", "done"}]} with at least one group']
    known, seen, found = {t["id"]: t for t in doc["tasks"]}, set(), []
    for n, group in enumerate(groups, 1):
        found += [f"group {n}: {p}" for p in _group_problems(group, known, seen)]
    return found


def _group_problems(group: dict, known: dict, seen: set) -> list[str]:
    if not isinstance(group, dict):
        return ["each group is an object with a lead, members, seams and done"]
    lead, members = group.get("lead"), group.get("members")
    if not isinstance(lead, str) or not isinstance(members, list) or not members:
        return ["needs a lead id and at least one member id"]
    if not all(isinstance(m, str) for m in members):
        return ["members must be task ids"]
    ids = [lead, *members]
    if missing := [i for i in ids if i not in known]:
        return [f"task {missing[0]} is not on the ledger"]
    found = [f"task {i} sits in another group of this plan" for i in ids if i in seen]
    seen.update(ids)
    if reason := ledger_groups.refusal([known[i] for i in ids], known, ledger_groups.FOLD_CEILING):
        found.append(reason)
    seams = group.get("seams")
    if not isinstance(seams, list) or len(seams) != len(ids) or not all(isinstance(s, str) and s for s in seams):
        found.append(f"seams must list {len(ids)} plain sentences, one per folded task")
    else:
        found += [f"seam {NUMBERS[i]} {p}" for i, s in enumerate(seams) for p in plain_problems(s, known)]
    done = group.get("done", "")
    found += [f"done {p}" for p in plain_problems(done, known)] if isinstance(done, str) else ["done must be text"]
    return found


def plain_problems(text: str, known: dict) -> list[str]:
    found = []
    if ids := sorted(i for i in known if re.search(rf"(?<![\w-]){re.escape(i)}(?![\w-])", text)):
        found.append(f"names task id {ids[0]}; describe the change in plain words")
    if match := PATH_RE.search(text):
        found.append(f"names the path {match.group(0)}; name the command, hook or behaviour instead")
    if dash := next((d for d in DASHES if d in text), None):
        found.append(f"uses the dash {dash.strip()!r}; use a comma or a full stop")
    return found


def description(doc: dict, lead: dict, group: dict) -> str:
    phase = next((p for p in doc.get("phases", []) if p.get("id") == lead.get("phase")), {})
    mission = ": ".join(filter(None, (phase.get("title"), _sentence(phase.get("description", "")))))
    lines = [
        f"Parent intent: {_sentence(doc.get('overview', '')) or 'not stated on the ledger'}.",
        f"Mission: {mission or 'not stated on the ledger'}.",
        f"This task delivers {len(group['seams'])} folded changes in one pull request.",
        *(f"{NUMBERS[i]}: {seam.rstrip('.')}." for i, seam in enumerate(group["seams"])),
        f"Done when {(group.get('done') or DONE).rstrip('.')}.",
    ]
    return "\n".join(lines)


def _sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0].rstrip(".").strip() if text else ""


def territory(known: dict, ids: list[str]) -> list[str]:
    return list(dict.fromkeys(area for i in ids for area in known[i].get("territory") or []))


def status(lead: dict, known: dict) -> str:
    text = STATUS.format(title=lead.get("title", "").rstrip("."))
    return FALLBACK_STATUS if plain_problems(text, known) else text


def writes(doc: dict, group: dict) -> dict:
    known = {t["id"]: t for t in doc["tasks"]}
    lead, members = known[group["lead"]], group["members"]
    return {
        "group": {"item": f"tasks/{lead['id']}", "members": members},
        "lead": {"description": description(doc, lead, group), "territory": territory(known, [lead["id"], *members])},
        "statuses": {m: status(lead, known) for m in members},
    }


def _plan(path: str | None) -> dict:
    if not path:
        sys.exit("fold check and fold apply take the plan file: fold check PLAN")
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read the plan {path}: {exc}")


def _checked(doc: dict, plan: dict) -> None:
    if found := problems(doc, plan):
        print(json.dumps({"problems": found}, indent=2))
        sys.exit(2)


def _apply(args, doc: dict, plan: dict) -> None:
    from scripts.swarm_ledger import ledger

    for group in plan["groups"]:
        planned = writes(doc, group)
        ledger.send(args, "task_group", **planned["group"])
        ledger.send(args, "task_update", item=planned["group"]["item"], fields=planned["lead"])
        for member, text in planned["statuses"].items():
            entry = {"op": "add", "thread": f"tasks/{member}/comments", "id": f"c-{uuid.uuid4().hex[:10]}"}
            entry |= {"text": text, "by": args.name}
            ledger.refused(ledger.call(args.slug, [entry]), [entry])
        print(json.dumps({"lead": group["lead"], "folded": group["members"]}))


def run(args) -> None:
    from scripts.swarm_ledger import ledger

    doc = ledger.call(args.slug)
    if args.action == "candidates":
        print(json.dumps(candidates(doc), indent=2))
        return
    plan = _plan(args.plan)
    _checked(doc, plan)
    if args.action == "check":
        print(json.dumps({"ok": True, "groups": len(plan["groups"])}))
        return
    _apply(args, doc, plan)
