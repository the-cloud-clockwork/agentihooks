#!/usr/bin/env python3
"""Agent CLI for a ledger: join the crew, talk in chat, record progress, acknowledge operator events.

Usage: ledger.py --slug SLUG --as NAME <command> [args]

  join [--role orchestrator|member]   enter the crew (the gate binds this session)
  leave                               leave the crew
  status                              crew, my unhandled operator events
  events                              my unhandled operator events, one line each
  ack [--rev N]                       mark operator events up to N (default: latest) as handled
  say TEXT [--long]                   chat message (TEXT "-" reads stdin); --long only when the operator asked to expand
  comment ITEM TEXT [--image PATH]    attach an image (repeatable) to your status on phases/<id>, questions/<id> or followups/<id>; amends your last one
  artifact PATH TITLE [--task ID] [--request ENTRY]
                                      publish a markdown, JSON, SVG or image file the operator asked for; its task
                                      must be marked artifact requested, or ENTRY names his message that asked
                                      (task defaults to your swarm task). Proofs go on the task proof and the PR
  artifact-purge                      delete every artifact and trash row of the ledger with their files
  publish-plan PATH --phase IDS [--title T] [--repo OWNER/NAME]
                                      publish an accepted plan: a GitHub issue where the repo has issues, else a
                                      ledger artifact; links it on each phase, comments the phase, and every task
                                      added to those phases carries the link
  plan phases PATH                    append a plan's phases (JSON, the init-swarm content phases shape) to the
                                      ledger, planned automatically unless a phase names manual, which waits in
                                      review; a taken phase id refuses them all
  phase ID done|open [--status T]     set a phase state, T becomes your status comment
  followup add TEXT | done|open ID    add a follow-up, close one, or reopen one
  followup add TEXT --needs-operator  add a follow-up that waits on the operator's decision; it shows in Priorities
  followup flag|unflag ID             mark a follow-up as waiting on the operator's decision, or no longer
  question add TEXT                   ask a question on the ledger; the master answers it or raises it to the operator
  scope ITEM in|out [--status T]      mark an item out of scope (or back in); T says why
  retext ITEM TEXT                    rewrite the text of a follow-up or question
  edit chat|ITEM ENTRY TEXT           rewrite an entry (yours; the orchestrator: any agent's)
  delete chat|ITEM ENTRY...           delete entries (yours; the orchestrator: any agent's)
  audit                               list every agent text the filter refuses, the cleanup worklist
  show                                print the whole ledger as JSON: every task, note, answer and comment (no --as needed)
  priority add ITEM TEXT              ask the operator: only what blocks on his answer, at most 20 words; ITEM may
                                      be phases/<id>, questions/<id>, followups/<id> or tasks/<id>. Unanswered
                                      questions, blocked tasks, merge approvals and flagged follow-ups show on their own
  priority clear ID... | --all        clear priorities once answered
  alert list | claim ID | close ID OUTCOME
                                      list open alerts, claim one, or close it done saying what was done
  relay ITEM TEXT --quote WORDS       post the operator's decision from this pane as his answer to questions/<id>
                                      or his comment on another item; WORDS, of any length, must be in an operator
                                      prompt or AskUserQuestion answer any master or planner of this swarm recorded
  answer ITEM TEXT                    the master answers questions/<id> as itself at delegate or full autonomy,
                                      which clears it from Priorities; members cannot answer
  time-left DURATION                 record remaining time, e.g. "3h 20m"
  claim ITEM                          take ownership of an item's operator events
  task add ID TITLE --lane eng|ci [--phase P] [--description D] [--depends-on IDS] [--territory AREAS] [--gain N] [--profile NAME]
           [--kind K] [--must M --check C --judge J] [--scaffold] [--artifact] [--rank R] [--difficulty D]
                                      add a swarm task; IDS and AREAS are comma separated; K is code (default), ci,
                                      ops, troubleshoot, tune or research; M, C, J form its proof contract;
                                      --scaffold creates its work folder (steering, progress, proof) in the same call;
                                      --artifact marks a file the operator asked for, so the task may publish it;
                                      R is the queue rank, urgent, high, normal (default) or low, next meaning
                                      urgent: the swarm claims eligible tasks highest rank first, then by
                                      critical path; only the master, a planner or the operator sets it;
                                      D is the task size, S, M or L, recorded as the operator's choice
  task set ID FIELD=VALUE...          set state, claimed_by, issue_url, pr_url, depends_on, territory, kind, rank,
                                      difficulty (S, M or L), artifact (yes or no) or plan_slice of a task;
                                      phase moves it to another phase, from the master or a planner;
                                      plan_slice computes its plan lines from the published plan;
                                      proof.KEY=VALUE and contract.KEY=VALUE pairs form one object, e.g.
                                      proof.command=C proof.output=O
  plan-backfill                       compute missing plan lines for linked unfinished tasks; list missing slices
  prompt                              print the join paragraph for a launch prompt
  url                                 print the ledger page link for the operator (no --as needed)

Agent text is for the operator: plain words, what was done or why it was skipped. The server refuses
clock times, dates, hashes, run ids, file names, code identifiers, capital labels, dashes, arrows,
AI phrasing, more than one parenthesis or semicolon, and comments over 50 words (chat 100, items 40).

Env: LEDGER_DIR, LEDGER_HOST (127.0.0.1), LEDGER_PORT (8765),
LEDGER_AUTOSTART=0 (never start a server on a failed request).
"""

import argparse
import collections
import functools
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger_authority as authority  # noqa: E402
import ledger_comments  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402
import ledger_kinds  # noqa: E402
import ledger_link  # noqa: E402
import ledger_publish  # noqa: E402
import ledger_tasks  # noqa: E402
import ledger_workspace  # noqa: E402
import watch_ledger  # noqa: E402

from scripts.gates.base import Who
from scripts.swarm_ledger import ledger_phases, ledger_task_duplicates
from scripts.swarm_ledger.repository import repository

BASE = "" if ledger_link.remote() else ledger_link.base()
REMOTE_READ_ATTEMPTS = 3
REMOTE_READ_PAUSE = 0.5
REQUEST_TIMEOUT = 10
CREDENTIAL_REFUSED = '"Missing or wrong ledger credential"'
AUTH_FAILURES = collections.Counter()
SHOW_JSON = functools.partial(json.dumps, indent=1, ensure_ascii=False)
OBJECT_FORMS = {
    "proof": (ledger_kinds.PROOF_KEYS, "proof.evidence=E proof.output=O"),
    "contract": (ledger_kinds.CONTRACT_KEYS, "contract.must=M contract.check=C"),
}


def base():
    return BASE or ledger_link.base()


def credentials(slug, service=False):
    who = Who.from_env()
    if ledger_link.remote():
        controller = os.environ.get("AGENTIHOOKS_CONTROLLER_CREDENTIAL")
        if controller and not who.pinned:
            return {"X-Controller-Credential": controller}
        if service:
            sys.exit("a remote ledger client cannot make service writes; the operator credential stays on its host")
        if not who.pinned:
            raise unauthenticated(
                "a remote ledger client needs a pinned agent identity; the operator credential stays on its host"
            )
        token = os.environ.get("AGENTIHOOKS_LEDGER_AGENT_TOKEN") or launch_token(slug, who.name)
        return {"X-Ledger-Token": token, "X-Ledger-Agent": who.name}
    token = repository.token(slug) or ""
    if service or not who.pinned:
        return {"X-Ledger-Token": token}
    return {"X-Ledger-Token": authority.agent_token(token, slug, who.name), "X-Ledger-Agent": who.name}


def launch_token(slug, name):
    from scripts.swarm_ledger.api.client import ResourceClient

    credential = os.environ.get("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL")
    if not credential:
        raise unauthenticated(
            "a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential "
            "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL from agentihooks hive join"
        )
    client = ResourceClient(base(), {"X-Hive-Credential": credential, "X-Ledger-Agent": name}, REQUEST_TIMEOUT)
    try:
        return client.request(slug, "agent-token", {})["data"]["token"]
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        raise unauthenticated(f"the ledger server refused the hive credential: {exc.code}") from None


def request(slug, ops=None, service=False, timeout=REQUEST_TIMEOUT):
    from scripts.swarm_ledger.api.client import ResourceClient

    client = ResourceClient(base(), credentials(slug, service), timeout)
    return client.snapshot(slug) if ops is None else client.mutate(slug, ops)


def resource(slug: str, path: str, service: bool = False, collection: bool = False) -> dict | list:
    from scripts.swarm_ledger.api.client import ResourceClient

    def read():
        client = ResourceClient(base(), credentials(slug, service), REQUEST_TIMEOUT)
        return client.collection(slug, path) if collection else client.request(slug, path)["data"]

    return retried(read, REMOTE_READ_ATTEMPTS) if ledger_link.remote() else read()


def export(slug: str, service: bool = False) -> dict:
    from scripts.swarm_ledger.api.client import ResourceClient

    def read():
        return ResourceClient(base(), credentials(slug, service), REQUEST_TIMEOUT).request(slug, "export", {})["data"]

    return retried(read, REMOTE_READ_ATTEMPTS) if ledger_link.remote() else read()


class Missing(SystemExit):
    pass


class Unauthenticated(SystemExit):
    pass


def unauthenticated(reason):
    AUTH_FAILURES[reason] += 1
    return Unauthenticated(f"unauthenticated: {reason}")


def ledger_remote_auth_failures_total() -> int:
    return sum(AUTH_FAILURES.values())


def call(slug, ops=None, service=False):
    if ledger_link.remote():
        return remote_call(slug, ops, service)
    try:
        return request(slug, ops, service)
    except urllib.error.HTTPError as exc:
        sys.exit(f"server refused: {exc.code} {exc.read().decode(errors='replace')}")
    except OSError:
        if not repository.exists(slug):
            raise Missing(f"ledger {slug} does not exist") from None
        if os.environ.get("LEDGER_AUTOSTART") != "0":
            subprocess.run(
                [sys.executable, str(HERE / "ledger_server.py"), "--ensure"], check=False, capture_output=True
            )
    try:
        return request(slug, ops, service)
    except urllib.error.HTTPError as exc:
        sys.exit(f"server refused: {exc.code} {exc.read().decode(errors='replace')}")
    except OSError as exc:
        sys.exit(f"ledger server not answering on {base()}: {exc}")


def remote_call(slug, ops, service):
    deliver = functools.partial(request, slug, ops, service)
    try:
        return retried(deliver, REMOTE_READ_ATTEMPTS if ops is None else 1)
    except urllib.error.HTTPError as exc:
        sys.exit(f"server refused: {exc.code} {refusal_text(exc)}")
    except OSError as exc:
        sys.exit(f"ledger server not answering on {base()}: {exc}")


def retried(read, attempts):
    for attempt in range(1, attempts + 1):
        try:
            return read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and CREDENTIAL_REFUSED in refusal_text(exc):
                raise unauthenticated("the ledger server refused the credential") from None
            if exc.code < 500 or attempt == attempts:
                raise
        except OSError:
            if attempt == attempts:
                raise
        time.sleep(REMOTE_READ_PAUSE * attempt)


def refusal_text(exc):
    if not hasattr(exc, "ledger_text"):
        exc.ledger_text = exc.read().decode(errors="replace")
    return exc.ledger_text


def op(kind, args, /, **fields):
    return {"op": kind, "id": f"{kind}-{uuid.uuid4().hex[:10]}", "by": args.name, **fields}


def refused(state, ops=(), fallback=""):
    if rejected := state.get("rejected"):
        reasons = [w for w in state.get("_meta", {}).get("warnings", []) if not core.size_warning(w)] or [
            unexplained(o) for o in ops if o["id"] in rejected
        ]
        sys.exit("; ".join(reasons) or fallback or f"rejected: {rejected}")


def unexplained(sent):
    where = next((sent[key] for key in ("item", "target", "path", "thread", "list") if sent.get(key)), "the ledger")
    return (
        f"{sent['op']} on {where} refused without a reason from the server: "
        "check that the entry exists, that you may change it and that its text is not empty"
    )


def posted(state, ops):
    print(json.dumps({"posted": not state.get("rejected")}))
    refused(state, ops)


def send(args, kind, /, **fields):
    ops = [op(kind, args, **fields)]
    state = call(args.slug, ops)
    refused(state, ops)
    for warning in state.get("_meta", {}).get("warnings", []):
        if warning.startswith(ledger_task_duplicates.UNCHECKED_PREFIX):
            print(warning, file=sys.stderr)
    return state


def mine(state, name):
    return gate.unhandled_for(state["_meta"], name, state.get("tasks", []))


def cmd_join(args):
    send(args, "join", role=args.role)
    print(json.dumps({"joined": args.name, "slug": args.slug, "role": args.role}))


def cmd_leave(args):
    send(args, "leave")
    print(json.dumps({"left": args.name}))


def cmd_events(args):
    for event in mine(call(args.slug), args.name):
        print(watch_ledger.line(event))


def cmd_show(args):
    print(SHOW_JSON(call(args.slug)))


def cmd_status(args):
    state = call(args.slug)
    print(
        json.dumps(
            {
                "crew": state["_meta"].get("crew", []),
                "unhandled": len(mine(state, args.name)),
                "rev": state["_meta"]["rev"],
                "time_left_minutes": state.get("time_left_minutes"),
            },
            indent=2,
        )
    )


def cmd_ack(args):
    rev = args.rev if args.rev is not None else call(args.slug)["_meta"]["rev"]
    send(args, "ack", rev=rev)
    print(json.dumps({"acked": rev}))


def cmd_say(args):
    text = (sys.stdin.read() if args.text == "-" else args.text).strip()
    op = {
        "op": "add",
        "thread": "chat",
        "id": f"m-{uuid.uuid4().hex[:10]}",
        "text": text,
        "by": args.name,
        "to": "operator",
    }
    if args.long:
        op["long"] = True
    posted(call(args.slug, [op]), [op])


def upload_image(slug: str, name: str, path: str) -> dict:
    return upload(slug, name, path, "media", {})


def upload_artifact(slug: str, name: str, path: str, request: dict) -> dict:
    return upload(
        slug,
        name,
        path,
        "artifacts",
        {"X-Artifact-Name": Path(path).name, "X-Artifact-Request": json.dumps(request)},
    )


def upload(slug: str, name: str, path: str, route: str, extra: dict) -> dict:
    req = urllib.request.Request(
        f"{base()}/api/v1/ledgers/{slug}/uploads/{route}",
        data=Path(path).read_bytes(),
        headers={
            **credentials(slug),
            "X-Ledger-Agent": name,
            "Content-Type": "application/octet-stream",
            **extra,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"server refused the file: {exc.code} {exc.read().decode(errors='replace')}")


def cmd_comment(args):
    thread = f"{args.item}/comments"
    attachments = [upload_image(args.slug, args.name, path) for path in getattr(args, "image", [])]
    entry = {"op": "add", "thread": thread, "id": f"c-{uuid.uuid4().hex[:10]}", "text": args.text, "by": args.name}
    if attachments:
        entry["attachments"] = attachments
    posted(call(args.slug, [entry]), [entry])


def cmd_artifact(args):
    task = args.task if args.task is not None else os.environ.get("AGENTIHOOKS_SWARM_TASK", "")
    request = {"request": args.request} if args.request else {}
    file = upload_artifact(args.slug, args.name, args.path, {"task": task, "title": args.title, **request})
    state = call(args.slug, [op("artifact_add", args, task=task, title=args.title, file=file, **request)])
    print(json.dumps({"published": not state.get("rejected")}))
    refused(state, fallback="rejected: join the ledger first and name a task it holds")


def cmd_publish_plan(args):
    from scripts.swarm_ledger import plan_ranges

    phases = comma_list(args.phase)
    if not phases:
        sys.exit("publish-plan needs --phase with the ids of the phases the plan fills")
    text = Path(args.path).read_text(encoding="utf-8")
    doc = call(args.slug)
    selected = [phase for phase in doc["phases"] if phase["id"] in phases]
    if {phase["id"] for phase in selected} != set(phases):
        sys.exit("publish-plan names an unknown phase")
    try:
        ranges = plan_ranges.phase_lines(text, selected)
    except ValueError as exc:
        sys.exit(str(exc))
    title = args.title or ledger_publish.title_of(text, phases)
    stored = {}

    def artifact(path, title):
        task = os.environ.get("AGENTIHOOKS_SWARM_TASK", "")
        file = upload_artifact(args.slug, args.name, path, {"task": task, "title": title, "plan": True})
        send(args, "artifact_add", task=task, title=title, file=file, plan=True)
        stored["url"] = f"{base()}/artifacts/{args.slug}/{file['id']}"
        return stored["url"]

    try:
        issue_title = ", ".join(phase["title"] for phase in selected)
        url, where = ledger_publish.publish(args.path, title, args.repo, artifact, issue_title=issue_title)
    except ledger_publish.PublishError as exc:
        sys.exit(str(exc))
    ops = []
    for phase in phases:
        fields = {"plan_url": url, "plan_ref": {"artifact": stored["url"], "lines": ranges[phase]}}
        ops.append(op("phase_update", args, item=f"phases/{phase}", fields=fields))
        text = (
            f"Plan published as a GitHub issue: {url}" if where == "issue" else f"Plan published on the ledger: {url}"
        )
        ops.append(
            {
                "op": "add",
                "thread": f"phases/{phase}/comments",
                "id": f"c-{uuid.uuid4().hex[:10]}",
                "text": text,
                "by": args.name,
            }
        )
    refused(call(args.slug, ops), ops)
    print(json.dumps({"plan_url": url, "published_to": where, "phases": phases}))


def cmd_plan(args):
    from scripts.swarm_ledger import ledger_phase_cli

    plan = json.loads(Path(args.path).read_bytes())
    phases = ledger_phase_cli.append_phases(
        plan, [phase["id"] for phase in resource(args.slug, "phases", collection=True)]
    )
    send(args, "phase_append", phases=phases)
    print(
        json.dumps(
            {"appended": [phase["phase"] for phase in phases], "planning": {p["phase"]: p["planning"] for p in phases}}
        )
    )


def cmd_artifact_purge(args):
    send(args, "artifact_purge")
    events = resource(args.slug, "events", collection=True)
    event = next(e for e in reversed(events) if e["kind"] == "artifacts purged")
    print(json.dumps({"purged": event["count"], "artifacts": resource(args.slug, "counts")["artifacts"]}))


def cmd_phase(args):
    if args.id in ("add", "set") and args.values:
        from scripts.swarm_ledger import ledger_phase_cli

        kind, fields = ledger_phase_cli.operation(args)
        send(args, kind, **fields)
        print(json.dumps(fields))
        return
    if args.state not in ("done", "open") or args.values:
        sys.exit("phase takes ID done|open, add ID TITLE, or set ID FIELD=VALUE")
    send(args, "set", **with_status(args, path=f"phases/{args.id}/done", value=args.state == "done"))
    print(json.dumps({"phase": args.id, "state": args.state}))


def with_status(args, **fields):
    return {**fields, "status": args.status} if args.status else fields


def cmd_followup(args):
    if args.action == "add":
        send(
            args,
            "add_item",
            list="followups",
            text=args.value,
            **({"needs_operator": True} if args.needs_operator else {}),
        )
    elif args.action in ("flag", "unflag"):
        send(args, "set", path=f"followups/{args.value}/needs_operator", value=args.action == "flag")
    else:
        send(args, "set", **with_status(args, path=f"followups/{args.value}/done", value=args.action == "done"))
    print(json.dumps({"followup": args.action}))


def cmd_question(args):
    send(args, "add_item", list="questions", text=args.text)
    print(json.dumps({"question": args.action}))


def cmd_priority(args):
    if args.action == "add":
        if len(args.values) != 2:
            sys.exit('priority add needs ITEM and "TEXT"')
        send(args, "priority", item=args.values[0], text=args.values[1])
        print(json.dumps({"priority": args.values[0]}))
        return
    targets = ["all"] if args.all else args.values
    ops = [op("priority_clear", args, target=t) for t in targets]
    state = call(args.slug, ops)
    rejected = state.get("rejected", [])
    cleared = [o["target"] for o in ops if o["id"] not in rejected]
    print(json.dumps({"cleared": cleared, "rejected": rejected}))
    refused(state, ops)


def cmd_alert(args):
    if args.action == "list":
        print(json.dumps([a for a in call(args.slug)["alerts"] if a["state"] != "done"], indent=2))
        return
    if not args.id or (args.action == "close") != bool(args.outcome):
        sys.exit('alert claim needs ID; alert close needs ID and "OUTCOME"')
    send(args, f"alert_{args.action}", target=args.id, **({"outcome": args.outcome} if args.outcome else {}))
    print(json.dumps({"alert": args.id, "action": args.action}))


def cmd_relay(args):
    import ledger_relay

    if not ledger_relay.verified(args.name, args.quote):
        sys.exit(
            "relay refused: the quote is not in an operator prompt or answer any master or planner of this swarm recorded"
        )
    send(args, "relay", item=args.item, text=args.text, quote=args.quote)
    print(json.dumps({"relayed": True, "item": args.item}))


def swarm_autonomy(slug):
    from scripts.swarm.store import SwarmError, connect

    try:
        return connect().config(slug).autonomy
    except SwarmError:
        return ""


def cmd_answer(args):
    import ledger_answer

    reason = ledger_answer.refusal(swarm_autonomy(args.slug))
    if reason:
        sys.exit(reason)
    send(args, "answer", item=args.item, text=args.text)
    print(json.dumps({"answered": True, "item": args.item}))


def cmd_scope(args):
    send(args, "set", **with_status(args, path=f"{args.item}/out_of_scope", value=args.state == "out"))
    print(json.dumps({"item": args.item, "scope": args.state}))


def cmd_retext(args):
    send(args, "retext", item=args.item, text=args.text)
    print(json.dumps({"retext": args.item}))


def thread_of(target):
    return "chat" if target == "chat" else f"{target}/comments"


def cmd_edit(args):
    ops = [{"op": "edit", "thread": thread_of(args.target), "id": args.entry, "text": args.text, "by": args.name}]
    refused(call(args.slug, ops), ops)
    print(json.dumps({"edited": args.entry}))


def cmd_delete(args):
    ops = [{"op": "delete", "thread": thread_of(args.target), "id": entry, "by": args.name} for entry in args.entries]
    state = call(args.slug, ops)
    print(
        json.dumps(
            {
                "deleted": [e for e in args.entries if e not in state.get("rejected", [])],
                "rejected": state.get("rejected", []),
            }
        )
    )
    refused(state, ops)


def cmd_audit(args):
    rows = ledger_comments.audit(export(args.slug))
    for where, entry, by, reasons in rows:
        print(f"{where} {entry} {by or '-'}: {'; '.join(reasons)}")
    print(f"{len(rows)} to clean")


def _duration(value):
    minutes = core.time_left_value(value)
    if minutes is None:
        raise argparse.ArgumentTypeError("time left must be a duration such as 3h 20m")
    return minutes


def cmd_time_left(args):
    send(args, "set", path="time_left_minutes", value=args.minutes)
    print(json.dumps({"time_left_minutes": args.minutes}))


def cmd_claim(args):
    send(args, "claim", item=args.item)
    print(json.dumps({"claimed": args.item}))


def cmd_plan_backfill(args):
    from scripts.swarm_ledger import plan_backfill

    plan_backfill.run(args)


def cmd_task(args):
    if args.action == "add":
        if args.id == "-":
            args.id = ledger_tasks.next_id(resource(args.slug, "tasks", collection=True))
        title = " ".join(args.values)
        lists = {
            k: comma_list(v)
            for k, v in (("depends_on", args.depends_on), ("territory", args.territory), ("overlays", args.overlays))
            if v
        }
        if args.gain is not None:
            lists["gain"] = args.gain
        contract = {k: getattr(args, k) for k in ("must", "check", "judge") if getattr(args, k)}
        if contract:
            lists["contract"] = contract
        if args.artifact:
            lists["artifact"] = True
        options = (
            ("kind", args.kind),
            ("profile", args.profile),
            ("rank", args.rank),
            ("difficulty", args.difficulty),
            ("plan_url", args.plan),
            ("not_duplicate", args.not_duplicate),
            ("plan_slice", args.plan_slice),
        )
        lists.update((key, value) for key, value in options if value)
        if args.scaffold:
            task = {"id": args.id, "title": title, "description": args.description, "phase": args.phase, **lists}
            doc = call(args.slug) if args.kind == "plan" else None
            lists["workspace"] = str(ledger_workspace.scaffold(args.slug, task, doc))
        send(
            args,
            "task_add",
            task=args.id,
            title=title,
            lane=args.lane,
            phase=args.phase,
            description=args.description,
            **lists,
        )
        print(json.dumps({"task": args.id, "added": title}))
        return
    if args.action == "group":
        send(args, "task_group", item=f"tasks/{args.id}", members=args.values)
        print(json.dumps({"task": args.id, "group_members": args.values}))
        return
    fields = dict(value.split("=", 1) for value in args.values if "=" in value)
    if len(fields) != len(args.values):
        sys.exit("task set takes FIELD=VALUE pairs")
    for key in ("depends_on", "territory", "overlays"):
        if key in fields:
            fields[key] = comma_list(fields[key])
    if "artifact" in fields:
        if fields["artifact"] not in ("yes", "no"):
            sys.exit("task set takes artifact=yes or artifact=no")
        fields["artifact"] = fields["artifact"] == "yes"
    if "difficulty_confidence" in fields:
        try:
            fields["difficulty_confidence"] = float(fields["difficulty_confidence"])
        except ValueError:
            sys.exit("task set takes difficulty_confidence as a number from 0 to 1")
    for dotted in [key for key in fields if "." in key]:
        name, _, sub = dotted.partition(".")
        if not isinstance(fields.get(name, {}), dict):
            sys.exit(f"task set takes {name}=VALUE or {name}.KEY=VALUE pairs, not both")
        fields.setdefault(name, {})[sub] = fields.pop(dotted)
    refuse_plain_objects(fields)
    send(args, "task_update", item=f"tasks/{args.id}", fields=fields)
    print(json.dumps({"task": args.id, **fields}))


def comma_list(text):
    return [part.strip() for part in text.split(",") if part.strip()]


def refuse_plain_objects(fields):
    for name, (allowed, example) in OBJECT_FORMS.items():
        if isinstance(fields.get(name, {}), str):
            sys.exit(
                f"task set takes one {name}.KEY=VALUE pair per key among {allowed}, e.g. {example}, not {name}=VALUE"
            )


def cmd_prompt(args):
    me = "agentihooks ledger"
    print(
        f"You are a member of crew ledger `{args.slug}` as `{args.name}`. Run once: {me} --slug {args.slug} "
        f"--as {args.name} join. In a swarm, operator writes reach you as inbox messages at your next tool call and "
        f"the swarm wakes you when idle; a ledger without a swarm needs a Monitor on: {me} watch {args.slug} --as "
        f"{args.name}. Act on every OPERATOR "
        f"line, then run `{me} --slug {args.slug} --as {args.name} ack`. Record progress with the commands in "
        f"`{me} --help` (phase, followup, comment, scope, say, time-left). The orchestrator maintains Time Left as one remaining duration "
        'with `time-left "3h 20m"` on joining and whenever progress or blockers change it. Write for the operator in plain words: '
        "one status comment per item saying what was done or why it was skipped, amended instead of repeated; evidence stays "
        "in PRs and notes. A hook blocks you from stopping while operator events are unhandled."
    )


def cmd_url(args):
    print(ledger_link.page_line(args.slug))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug")
    parser.add_argument("--as", dest="name")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("join").add_argument("--role", choices=["orchestrator", "member"], default="member")
    for plain in ("leave", "status", "events", "prompt", "url"):
        sub.add_parser(plain)
    sub.add_parser("ack").add_argument("--rev", type=int)
    say = sub.add_parser("say")
    say.add_argument("text")
    say.add_argument("--long", action="store_true")
    for name, first, second in (("comment", "item", "text"), ("retext", "item", "text")):
        parser_ = sub.add_parser(name)
        parser_.add_argument(first)
        parser_.add_argument(second)
        if name == "comment":
            parser_.add_argument(
                "--image", action="append", default=[], help="image path to upload; repeat for several"
            )
    artifact = sub.add_parser("artifact")
    artifact.add_argument("path")
    artifact.add_argument("title")
    artifact.add_argument("--task", help="task id; default AGENTIHOOKS_SWARM_TASK, empty for none")
    artifact.add_argument("--request", help="id of the operator chat line or comment that asked for the file")
    sub.add_parser("artifact-purge")
    sub.add_parser("plan-backfill", help="compute missing plan lines for linked unfinished tasks")
    phase = sub.add_parser("phase")
    phase.add_argument("id")
    phase.add_argument("state")
    phase.add_argument("values", nargs="*")
    phase.add_argument("--status")
    phase.add_argument("--description", default="")
    phase.add_argument("--depends-on", default="")
    phase.add_argument("--planning", choices=["manual", "auto"], default=ledger_phases.PLANNING_DEFAULT)
    phase.add_argument("--release", action="store_true")
    followup = sub.add_parser("followup")
    followup.add_argument("action", choices=["add", "done", "open", "flag", "unflag"])
    followup.add_argument("value")
    followup.add_argument("--status")
    followup.add_argument("--needs-operator", action="store_true")
    question = sub.add_parser("question")
    question.add_argument("action", choices=["add"])
    question.add_argument("text")
    scope = sub.add_parser("scope")
    scope.add_argument("item")
    scope.add_argument("state", choices=["in", "out"])
    scope.add_argument("--status")
    edit = sub.add_parser("edit")
    for arg in ("target", "entry", "text"):
        edit.add_argument(arg)
    delete = sub.add_parser("delete")
    delete.add_argument("target")
    delete.add_argument("entries", nargs="+")
    sub.add_parser("audit")
    sub.add_parser("show")
    priority = sub.add_parser("priority")
    priority.add_argument("action", choices=["add", "clear"])
    priority.add_argument("values", nargs="*")
    priority.add_argument("--all", action="store_true")
    alert = sub.add_parser("alert")
    alert.add_argument("action", choices=["list", "claim", "close"])
    alert.add_argument("id", nargs="?")
    alert.add_argument("outcome", nargs="?")
    relay = sub.add_parser("relay")
    relay.add_argument("item")
    relay.add_argument("text")
    relay.add_argument("--quote", required=True)
    answer = sub.add_parser("answer")
    answer.add_argument("item")
    answer.add_argument("text")
    sub.add_parser("time-left").add_argument("minutes", type=_duration)
    sub.add_parser("claim").add_argument("item")
    task = sub.add_parser("task")
    task.add_argument("action", choices=["add", "set", "group"])
    task.add_argument("id", help="task id; task add - mints the next free t<n>")
    task.add_argument("values", nargs="+")
    task.add_argument("--lane", choices=["eng", "ci", "plan"], default="eng")
    task.add_argument("--phase", default="")
    task.add_argument("--description", default="")
    task.add_argument("--depends-on", default="", help="comma separated task ids that must be done first")
    task.add_argument("--territory", default="", help="comma separated files, folders or areas the task touches")
    task.add_argument(
        "--gain", type=float, help="what the task is expected to win, as a number the health check compares"
    )
    task.add_argument(
        "--kind", choices=ledger_kinds.KINDS, help="code (default), ci, ops, troubleshoot, tune, research"
    )
    task.add_argument("--must", default="", help="contract: what must be true when the task is done")
    task.add_argument("--check", default="", help="contract: how it is checked")
    task.add_argument("--judge", default="", help="contract: who judges it")
    task.add_argument(
        "--scaffold", action="store_true", help="create the task's work folder now and store it as its workspace"
    )
    task.add_argument("--artifact", action="store_true", help="the operator asked this task for a file to review")
    task.add_argument("--profile")
    task.add_argument("--overlays", help="comma separated overlays this task's agent wears, at most three")
    task.add_argument("--rank", help="queue rank: urgent, high, normal (default) or low; next means urgent")
    task.add_argument("--plan-slice", default="", help="task slice anchor; computes its plan lines")
    task.add_argument("--plan", default="", help="link to the published plan; default the phase's plan link")
    task.add_argument("--difficulty", choices=ledger_tasks.DIFFICULTIES, help="task size: S, M or L")
    task.add_argument(
        "--not-duplicate", default="", help="why the task differs from the one it resembles; skips the duplicate check"
    )
    publish = sub.add_parser("publish-plan")
    publish.add_argument("path")
    publish.add_argument("--phase", required=True, help="comma separated ids of the phases the plan fills")
    publish.add_argument("--title", default="", help="default the plan's first heading")
    publish.add_argument("--repo", default="", help="OWNER/NAME for the issue; default the current repo")
    plan = sub.add_parser("plan")
    plan.add_argument("action", choices=["phases"])
    plan.add_argument("path")
    return parser


def main():
    from scripts.gates import Who
    from scripts.gates.identity import refusal
    from scripts.swarm.naming import resolve_name

    args = build_parser().parse_args()
    if text := refusal(args.name, Who.from_env()):
        sys.exit(f"agentihooks ledger: {text}")
    args.name = resolve_name(args.name) if args.name else args.name
    if not args.slug or not (args.name or args.command in ("url", "show")):
        sys.exit("--slug and --as are required")
    globals()[f"cmd_{args.command.replace('-', '_')}"](args)


if __name__ == "__main__":
    main()
