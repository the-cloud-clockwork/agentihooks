#!/usr/bin/env python3
"""Agent CLI for a ledger: join the crew, talk in chat, record progress, acknowledge operator events.

Usage: ledger.py --slug SLUG --as NAME <command> [args]

  join [--role orchestrator|member]   enter the crew (the gate binds this session)
  leave                               leave the crew
  status                              crew, my unhandled operator events
  events                              my unhandled operator events, one line each
  ack [--rev N]                       mark operator events up to N (default: latest) as handled
  say TEXT --reply-to ID [--long]     answer an operator chat line by its id (TEXT "-" reads stdin); --long only when he asked to expand
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
  followup add TEXT --needs-operator  operator and swarm only: agents are refused, they never ask the operator first
  followup flag|unflag ID             mark a follow-up as waiting on the operator's decision, or no longer
  question add TEXT                   operator and swarm only: agents send questions to master@<slug> by inbox
  scope ITEM in|out [--status T]      mark an item out of scope (or back in); T says why
  retext ITEM TEXT                    rewrite the text of a follow-up or question
  edit chat|ITEM ENTRY TEXT           rewrite an entry (yours; the orchestrator: any agent's)
  delete chat|ITEM ENTRY...           delete entries (yours; the orchestrator: any agent's)
  audit                               list every agent text the filter refuses, the cleanup worklist
  show                                print the whole ledger as JSON: every task, note, answer and comment (no --as needed);
                                      masters never run it, it slows every write
  get ITEM                            print one item through the ledger API: tasks/<id>, phases/<id>, followups/<id>
                                      or priorities/<id> (no --as needed)
  tasks [--state S] [--lane L] [--text T]
                                      list open tasks, one line each, by state, lane or title text (no --as needed)
  priority add ITEM TEXT              operator and swarm only, at most 20 words; agents are refused; ITEM may
                                      be phases/<id>, questions/<id>, followups/<id> or tasks/<id>. Unanswered
                                      questions, blocked tasks, merge approvals and flagged follow-ups show on their own
  priority clear ID... | --all        clear priorities once answered
  alert list | claim ID | close ID OUTCOME
                                      list open alerts, claim one, or close it done saying what was done
  relay ITEM TEXT --quote WORDS       post the operator's decision from this pane as his answer to questions/<id>
                                      or his comment on another item; WORDS, of any length, must be in an operator
                                      prompt or AskUserQuestion answer any master or planner of this swarm recorded
  answer ITEM TEXT                    the master answers questions/<id> as itself at delegate or full autonomy,
                                      which clears it from Priorities; members other than a master seat cannot answer
  fold candidates | check PLAN | apply PLAN
                                      fold open tasks that change the same code into one grouped task (fold-tasks skill)
  time-left DURATION                 record remaining time, e.g. "3h 20m"
  claim ITEM                          take ownership of an item's operator events
  task add ID TITLE --lane eng|ci [--phase P] [--description D] [--depends-on IDS] [--territory AREAS] [--gain N] [--profile NAME]
           [--kind K] [--must M --check C --judge J] [--push] [--scaffold] [--artifact] [--workstation] [--rank R]
           [--difficulty D]
                                      add a swarm task; IDS and AREAS are comma separated; K is code (default), ci,
                                      ops, troubleshoot, tune or research; M, C, J form its proof contract;
                                      --push marks a proof that needs a pushed branch and a CI run, so the task
                                      always gets the engineer profile, never qa;
                                      --scaffold creates its work folder (steering, progress, proof) in the same call;
                                      --artifact marks a file the operator asked for, so the task may publish it;
                                      --workstation keeps a task that acts on the workstation checkout or its
                                      task folders off every remote hive (task set workstation=yes or no);
                                      --needs CAPS claims it only on a hive declaring every capability in CAPS,
                                      such as headed (task set needs=CAPS);
                                      R is the queue rank, urgent, high, normal (default) or low, next meaning
                                      urgent: the swarm claims eligible tasks highest rank first, then by
                                      critical path; only the master, a planner or the operator sets it;
                                      D is the task size, S, M or L, recorded as the operator's choice
  task set ID FIELD=VALUE...          set state, claimed_by, issue_url, pr_url, depends_on, territory, kind, rank,
                                      difficulty (S, M or L), artifact (yes or no) or plan_slice of a task;
                                      after_release=vX.Y.Z holds its claim until a newer release tag exists;
                                      applied_at=YYYY-MM-DDTHH:MM:SSZ records when the Doctor applied a merged fix;
                                      phase moves it to another phase, from the master or a planner;
                                      a move naming no slice drops it, into a phase with a plan as a follow up;
                                      plan_slice computes its plan lines from the published plan;
                                      follow_up=yes marks it a follow up and clears its slice and plan lines,
                                      refused while the same write names a plan_slice or slice;
                                      proof.KEY=VALUE and contract.KEY=VALUE pairs form one object, e.g.
                                      proof.command=C proof.output=O; contract pairs update only the keys they
                                      name, e.g. contract.push=yes; --quote with rank=R relays the
                                      operator's words so the rank stays his and the dispatcher keeps it
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
import itertools
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from scripts.swarm_ledger import ledger_route

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
from scripts.inbox import outbox
from scripts.swarm_ledger import ledger_phases, ledger_plans, ledger_task_duplicates, ports, storage, workflow_task

repository = storage.default()

BASE = "" if ledger_link.remote() else ledger_link.base()
REMOTE_READ_ATTEMPTS = 3
REMOTE_READ_PAUSE = 0.5
REQUEST_TIMEOUT = 10
TALK_OPS = ("answer", "add_item", "relay")
TASK_LISTS = {"group": ("task_group", "members", "group_members"), "split": ("task_split", "halves", "split")}
CREDENTIAL_REFUSED = '"Missing or wrong ledger credential"'
AUTH_FAILURES = collections.Counter()
SHOW_JSON = functools.partial(json.dumps, indent=1, ensure_ascii=False)
LEDGER_ITEM = re.compile(r"(?:tasks|phases|followups|priorities)/[A-Za-z0-9][A-Za-z0-9._-]*")
OBJECT_FORMS = {
    "proof": (ledger_kinds.PROOF_KEYS, "proof.evidence=E proof.output=O"),
    "contract": (
        ledger_kinds.CONTRACT_KEYS + ledger_kinds.CONTRACT_TIMES + ledger_kinds.CONTRACT_FLAGS,
        "contract.must=M contract.check=C",
    ),
}


def base(slug=""):
    return ledger_route.url(slug) or BASE or ledger_link.base(slug=slug)


def credentials(slug, service=False):
    from scripts.swarm_ledger.authority_context import tick_headers

    who = Who.from_env()
    if ledger_link.remote(slug=slug):
        tick = tick_headers(slug)
        if tick:
            return tick
        controller = os.environ.get("AGENTIHOOKS_CONTROLLER_CREDENTIAL")
        if controller and not who.pinned:
            return {"X-Controller-Credential": controller}
        if service:
            sys.exit("a remote ledger client cannot make service writes; the operator credential stays on its host")
        if not who.pinned:
            raise unauthenticated(
                "a remote ledger client needs a pinned agent identity; the operator credential stays on its host"
            )
        token = os.environ.get("AGENTIHOOKS_LEDGER_AGENT_TOKEN") or derived_token(slug, who.name)
        return {"X-Ledger-Token": token, "X-Ledger-Agent": who.name}
    return local_credentials(slug, who.name if who.pinned else "", service)


def local_credentials(slug, agent, service):
    token = repository.token(slug) or ""
    if service or not agent:
        return {"X-Ledger-Token": token}
    return {"X-Ledger-Token": authority.agent_token(token, slug, agent), "X-Ledger-Agent": agent}


def replay(slug, ops, agent, service):
    from scripts.swarm_ledger.api.client import ResourceClient

    return ResourceClient(base(slug), local_credentials(slug, agent, service), REQUEST_TIMEOUT).mutate(slug, ops)


def derived_token(slug, name):
    from scripts.swarm_ledger.authority_context import secret as trusted_secret

    secret = trusted_secret()
    return authority.agent_token(secret, slug, name) if secret else launch_token(slug, name)


def launch_token(slug, name):
    from scripts.swarm_ledger.api.client import ResourceClient

    credential = os.environ.get("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL")
    if not credential:
        raise unauthenticated(
            "a remote ledger client needs AGENTIHOOKS_LEDGER_AGENT_TOKEN or the hive credential "
            "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL from agentihooks hive add"
        )
    client = ResourceClient(base(slug), {"X-Hive-Credential": credential, "X-Ledger-Agent": name}, REQUEST_TIMEOUT)
    try:
        return client.request(slug, "agent-token", {})["data"]["token"]
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise
        raise unauthenticated(f"the ledger server refused the hive credential: {exc.code}") from None


def request(slug, ops=None, service=False, timeout=REQUEST_TIMEOUT):
    from scripts.swarm_ledger.api.client import ResourceClient

    client = ResourceClient(base(slug), credentials(slug, service), timeout)
    return client.snapshot(slug) if ops is None else client.mutate(slug, ops)


def resource(
    slug: str, path: str, service: bool = False, collection: bool = False, headers: dict | None = None
) -> dict | list:
    from scripts.swarm_ledger.api.client import ResourceClient

    def read():
        client = ResourceClient(base(slug), credentials(slug, service) if headers is None else headers, REQUEST_TIMEOUT)
        return client.collection(slug, path) if collection else client.request(slug, path)["data"]

    return retried(read, REMOTE_READ_ATTEMPTS) if ledger_link.remote(slug=slug) else read()


def export(slug: str, service: bool = False) -> dict:
    from scripts.swarm_ledger.api.client import ResourceClient

    def read():
        return ResourceClient(base(slug), credentials(slug, service), REQUEST_TIMEOUT).request(slug, "export", {})[
            "data"
        ]

    return retried(read, REMOTE_READ_ATTEMPTS) if ledger_link.remote(slug=slug) else read()


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
    if ledger_link.remote(slug=slug):
        return remote_call(slug, ops, service)
    talking = talk(ops)
    if outbox.waiting("ledger"):
        outbox.flush(None)
    if ops and outbox.waiting("ledger"):
        if not talking:
            sys.exit(
                f"ledger talk queued while the server was down still waits in {outbox.path()}; retry once it lands"
            )
        return queue(slug, ops, service)
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
        if talking:
            return queue(slug, ops, service)
        sys.exit(f"ledger server not answering on {base()}: {exc}")


def talk(ops):
    return bool(ops) and all(
        o["op"] in TALK_OPS or (o["op"] == "add" and "by" in o and not o.get("attachments")) for o in ops
    )


def queue(slug, ops, service):
    who = Who.from_env()
    try:
        outbox.append(outbox.ledger_record(slug, ops, who.name if who.pinned else "", service))
    except outbox.OutboxFull as exc:
        sys.exit(str(exc))
    print(
        f"ledger server not answering on {base()}: queued locally, it replays when the server returns", file=sys.stderr
    )
    return {"queued": ops[0]["id"]}


def remote_call(slug, ops, service):
    deliver = functools.partial(request, slug, ops, service)
    try:
        return retried(deliver, REMOTE_READ_ATTEMPTS if ops is None else 1)
    except urllib.error.HTTPError as exc:
        sys.exit(f"server refused: {exc.code} {refusal_text(exc)}")
    except OSError as exc:
        sys.exit(f"ledger server not answering on {base()}: {exc}")


def retried(read, attempts):
    for attempt in itertools.count(1):
        try:
            return read()
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and CREDENTIAL_REFUSED in refusal_text(exc):
                raise unauthenticated("the ledger server refused the credential") from None
            if exc.code < 500 or attempt >= attempts:
                raise
        except OSError:
            if attempt >= attempts:
                raise
        time.sleep(REMOTE_READ_PAUSE * attempt)


def refusal_text(exc):
    if not hasattr(exc, "ledger_text"):
        exc.ledger_text = exc.read().decode(errors="replace")
    return exc.ledger_text


def op(kind, args, /, **fields):
    return {"op": kind, "id": f"{kind}-{uuid.uuid4().hex[:10]}", "by": args.name, **fields}


def refused(state, ops=(), fallback="", lead=""):
    if rejected := state.get("rejected"):
        warnings = state.get("_meta", {}).get("warnings", [])
        reasons = [
            w
            for w in warnings
            if not core.size_warning(w) and not (lead and w.startswith(ledger_task_duplicates.UNCHECKED_PREFIX))
        ] or [unexplained(o) for o in ops if o["id"] in rejected]
        sys.exit(lead + ("; ".join(reasons) or fallback or f"rejected: {rejected}"))


def unexplained(sent):
    where = next((sent[key] for key in ("item", "target", "path", "thread", "list") if sent.get(key)), "the ledger")
    return (
        f"{sent['op']} on {where} refused without a reason from the server: "
        "check that the entry exists, that you may change it and that its text is not empty"
    )


def posted(state, ops):
    queued = held(state)
    print(json.dumps({"posted": not queued and not state.get("rejected"), **queued}))
    refused(state, ops)


def held(state):
    return {"queued": True} if state.get("queued") else {}


def send(args, kind, /, **fields):
    ops = [op(kind, args, **fields)]
    lead = f"task {fields['task']} was not added: " if kind == "task_add" else ""
    try:
        state = call(args.slug, ops)
    except SystemExit as exc:
        if lead and isinstance(exc.code, str):
            raise type(exc)(lead + exc.code) from None
        raise
    refused(state, ops, lead=lead)
    if lead and not state.get("applied"):
        sys.exit(f"{lead}the ledger server did not apply it")
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


def cmd_tree(args):
    for row in resource(args.slug, f"hierarchy/subtree/{args.node}" if args.node else "hierarchy", collection=True):
        print(f"{'  ' * row['depth']}{row['node']}  {row['state']}")


def cmd_get(args):
    print(SHOW_JSON(resource(args.slug, args.item)))


def cmd_tasks(args):
    text = (args.text or "").lower()
    for task in resource(args.slug, "tasks", collection=True):
        state, lane, title = task.get("state") or "open", task.get("lane") or "", task.get("title") or ""
        if state == "done" or task.get("out_of_scope"):
            continue
        if args.state not in (None, state) or args.lane not in (None, lane):
            continue
        if text in title.lower():
            print(f"{task['id']}  {state}  {lane}  {title}")


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
    if vars(args).get("reply_to"):
        op["reply_to"] = args.reply_to
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
        with ledger_link.urlopen(req, REQUEST_TIMEOUT) as resp:
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


def _repoint_plan(args, phases):
    doc = call(args.slug)
    selected = [phase for phase in doc["phases"] if phase["id"] in phases]
    if {phase["id"] for phase in selected} != set(phases):
        sys.exit("publish-plan names an unknown phase")
    parents = {phase.get("plan", "") for phase in selected}
    if len(parents) != 1 or not next(iter(parents)):
        sys.exit("repoint needs phases belonging to one published plan")
    plan = ledger_plans.find(doc, next(iter(parents)))
    task = os.environ.get("AGENTIHOOKS_SWARM_TASK", "")
    if args.path:
        file = upload_artifact(args.slug, args.name, args.path, {"task": task, "title": plan["title"], "plan": True})
    else:
        file_id = plan["artifact"].rsplit("/", 1)[-1]
        file = next(
            (
                row["file"]
                for row in doc.get("artifacts", [])
                if row.get("plan") is True and row["file"]["id"] == file_id
            ),
            None,
        )
        if file is None:
            sys.exit("repoint needs the original artifact registered on this server")
    if ledger_plans.plan_id(file["id"]) != plan["id"]:
        sys.exit("repoint needs the original plan artifact")
    artifact = f"{base()}/artifacts/{args.slug}/{file['id']}"
    url = artifact if plan["url"] == plan["artifact"] else plan["url"]
    ops = [op("plan_add", args, plan=plan["id"], title=plan["title"], artifact=artifact, url=url, repoint=True)]
    if args.path:
        ops.insert(0, op("artifact_add", args, task=task, title=plan["title"], file=file, plan=True))
    refused(call(args.slug, ops), ops)
    affected = [phase["id"] for phase in doc["phases"] if phase.get("plan") == f"plans/{plan['id']}"]
    print(json.dumps({"plan_url": url, "artifact": artifact, "phases": affected}))


def cmd_publish_plan(args):
    from scripts.swarm_ledger import plan_ranges

    phases = comma_list(args.phase)
    if not phases:
        sys.exit("publish-plan needs --phase with the ids of the phases the plan fills")
    if args.repoint:
        _repoint_plan(args, phases)
        return
    if not args.path:
        sys.exit("publish-plan needs a path unless --repoint is used")
    text = Path(args.path).read_text(encoding="utf-8")
    doc = call(args.slug)
    selected = [phase for phase in doc["phases"] if phase["id"] in phases]
    if {phase["id"] for phase in selected} != set(phases):
        sys.exit("publish-plan names an unknown phase")
    try:
        ranges = plan_ranges.phase_lines(text, selected)
        plan_ranges.require_markers(text, ranges)
    except ValueError as exc:
        sys.exit(str(exc))
    title = args.title or ledger_publish.title_of(text, phases)
    stored = {}

    def artifact(path, title):
        task = os.environ.get("AGENTIHOOKS_SWARM_TASK", "")
        file = upload_artifact(args.slug, args.name, path, {"task": task, "title": title, "plan": True})
        stored["add"] = op("artifact_add", args, task=task, title=title, file=file, plan=True)
        stored["url"] = f"{base()}/artifacts/{args.slug}/{file['id']}"
        stored["plan"] = ledger_plans.plan_id(file["id"])
        return stored["url"]

    try:
        issue_title = ", ".join(phase["title"] for phase in selected)
        url, where = ledger_publish.publish(args.path, title, args.repo, artifact, issue_title=issue_title)
    except ledger_publish.PublishError as exc:
        sys.exit(str(exc))
    ops = [stored["add"], op("plan_add", args, plan=stored["plan"], title=title, artifact=stored["url"], url=url)]
    for phase in phases:
        fields = {
            "plan_url": url,
            "plan_ref": {"artifact": stored["url"], "lines": ranges[phase]},
            "plan": f"plans/{stored['plan']}",
        }
        ops.append(op("phase_update", args, item=f"phases/{phase}", fields=fields))
        note = (
            f"Plan published as a GitHub issue: {url}" if where == "issue" else f"Plan published on the ledger: {url}"
        )
        ops.append(
            {
                "op": "add",
                "thread": f"phases/{phase}/comments",
                "id": f"c-{uuid.uuid4().hex[:10]}",
                "text": note,
                "by": args.name,
            }
        )
        ops += [
            op("slice_add", args, phase=f"phases/{phase}", anchor=anchor)
            for anchor in plan_ranges.slice_anchors(text, ranges[phase])
        ]
    state = call(args.slug, ops)
    linked = {row.get("url") for row in doc.get("plans", [])} | {phase.get("plan_url") for phase in doc["phases"]}
    if state.get("rejected") and where == "issue" and url not in linked:
        try:
            ledger_publish.close_issue(url)
        except ledger_publish.PublishError as exc:
            print(exc, file=sys.stderr)
    refused(state, ops)
    tasks = ledger_plans.resliced(doc["tasks"], call(args.slug)["tasks"])
    reslice = {"tasks": tasks} if any(tasks.values()) else {}
    print(json.dumps({"plan_url": url, "published_to": where, "phases": phases, **reslice}))


def cmd_plan(args):
    from scripts.swarm_ledger import ledger_phase_cli

    plan = json.loads(Path(args.path).read_bytes())
    phases = ledger_phase_cli.append_phases(
        plan, [phase["id"] for phase in resource(args.slug, "phases", collection=True)]
    )
    state = send(args, "phase_append", phases=phases)
    repeats = ledger_phase_cli.repeats(state.get("_meta", {}).get("warnings", []), phases)
    for warning in repeats.values():
        print(warning, file=sys.stderr)
    print(
        json.dumps(
            {
                "appended": [phase["phase"] for phase in phases if phase["phase"] not in repeats],
                "planning": {p["phase"]: p["planning"] for p in phases if p["phase"] not in repeats},
            }
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
            **({"not_duplicate": args.not_duplicate} if args.not_duplicate else {}),
            **hive_fields(args),
            **({"task": task} if (task := os.environ.get("AGENTIHOOKS_SWARM_TASK")) else {}),
        )
    elif args.action in ("flag", "unflag"):
        send(args, "set", path=f"followups/{args.value}/needs_operator", value=args.action == "flag")
    else:
        send(args, "set", **with_status(args, path=f"followups/{args.value}/done", value=args.action == "done"))
    print(json.dumps({"followup": args.action}))


def cmd_question(args):
    result = send(args, "add_item", list="questions", text=args.text, **hive_fields(args))
    print(json.dumps({"question": args.action, **held(result)}))


def hive_fields(args):
    hive = vars(args).get("hive")
    return {"hive": hive} if hive is not None else {}


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
    queued = held(send(args, "relay", item=args.item, text=args.text, quote=args.quote))
    print(json.dumps({"relayed": not queued, "item": args.item, **queued}))


def swarm_autonomy(slug):
    SwarmError, connect = ports.swarm().store.SwarmError, ports.swarm().store.connect

    try:
        return connect().config(slug).autonomy
    except SwarmError:
        return ""


def cmd_answer(args):
    import ledger_answer

    reason = ledger_answer.refusal(swarm_autonomy(args.slug))
    if reason:
        sys.exit(reason)
    queued = held(send(args, "answer", item=args.item, text=args.text))
    print(json.dumps({"answered": not queued, "item": args.item, **queued}))


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


def _ledger_item(value):
    if not LEDGER_ITEM.fullmatch(value):
        raise argparse.ArgumentTypeError("name tasks/<id>, phases/<id>, followups/<id> or priorities/<id>")
    return value


def cmd_fold(args):
    from scripts.swarm_ledger import ledger_fold

    ledger_fold.run(args)


def cmd_time_left(args):
    send(args, "set", path="time_left_minutes", value=args.minutes)
    print(json.dumps({"time_left_minutes": args.minutes}))


def cmd_claim(args):
    send(args, "claim", item=args.item)
    print(json.dumps({"claimed": args.item}))


def cmd_plan_backfill(args):
    from scripts.swarm_ledger import plan_backfill

    plan_backfill.run(args)


def cmd_hierarchy(args) -> None:
    from scripts.swarm_ledger import hierarchy_backfill

    hierarchy_backfill.run(args)


def refuse_unset_quote(args):
    if vars(args).get("quote") and args.action != "set":
        sys.exit(f"task {args.action} takes no --quote: run task set <id> rank=<rank> --quote <his words> after it")


def _add_task(args, title, fields):
    if args.scaffold:
        task = {"id": args.id, "title": title, "description": args.description, "phase": args.phase, **fields}
        doc = call(args.slug) if args.kind == "plan" else None
        fields["workspace"] = str(ledger_workspace.folder(args.slug, args.id))
    send(
        args,
        "task_add",
        task=args.id,
        title=title,
        lane=args.lane,
        phase=args.phase,
        description=args.description,
        **fields,
    )
    if args.scaffold:
        try:
            ledger_workspace.scaffold(args.slug, task, doc)
        except OSError as exc:
            print(f"task {args.id} was added; its work folder was not created: {exc}", file=sys.stderr)
            return {"workspace_error": str(exc)}
    return {}


def _task_add_fields(args):
    lists = {
        k: comma_list(v)
        for k, v in (
            ("depends_on", args.depends_on),
            ("territory", args.territory),
            ("overlays", args.overlays),
            ("repositories", args.repositories),
            ("needs", args.needs),
        )
        if v
    }
    if args.gain is not None:
        lists["gain"] = args.gain
    contract = {k: getattr(args, k) for k in ("must", "check", "judge", "push") if getattr(args, k)}
    if contract:
        lists["contract"] = contract
    lists.update((key, True) for key in ("artifact", "follow_up", "workstation") if getattr(args, key))
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
    if args.kind == "workflow":
        try:
            lists.update(workflow_task.add_fields(args.workflow, args.case, args.lane, args.profile, Path.cwd()))
        except ValueError as exc:
            sys.exit(f"task {args.id} was not added: {exc}")
    return lists


def _task_set_fields(values):
    fields = dict(value.split("=", 1) for value in values if "=" in value)
    if len(fields) != len(values):
        sys.exit("task set takes FIELD=VALUE pairs")
    for key in ("depends_on", "territory", "overlays", "repositories", "split_pulls", "needs", "doc_items"):
        if key in fields:
            fields[key] = task_list(key, fields[key])
    for key in [key for key in ("artifact", "follow_up", "workstation") if key in fields]:
        if fields[key] not in ("yes", "no"):
            sys.exit(f"task set takes {key}=yes or {key}=no")
        fields[key] = fields[key] == "yes"
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
    return fields


def task_branch_fields(args: argparse.Namespace, fields: dict) -> dict:
    if "branch" not in fields:
        return fields
    swarm = ports.swarm()
    try:
        repo = swarm.store.connect().config(args.slug).repo
    except swarm.store.SwarmError:
        return fields
    task = resource(args.slug, f"tasks/{args.id}")
    branches = {**task.get("repository_branches", {}), **fields.get("repository_branches", {})}
    if fields["branch"]:
        branches[repo] = fields["branch"]
    else:
        branches.pop(repo, None)
    return {**fields, "repository_branches": branches}


def cmd_task(args):
    refuse_unset_quote(args)
    if args.action == "add":
        if args.id == "-":
            args.id = ledger_tasks.next_id(resource(args.slug, "tasks", collection=True))
        title = " ".join(args.values)
        lists = _task_add_fields(args)
        print(json.dumps({"task": args.id, "added": title, **_add_task(args, title, lists)}))
        return
    if args.action in TASK_LISTS:
        kind, field, shown = TASK_LISTS[args.action]
        send(args, kind, item=f"tasks/{args.id}", **{field: args.values})
        print(json.dumps({"task": args.id, shown: args.values}))
        return
    fields = _task_set_fields(args.values)
    refuse_plain_objects(fields)
    fields = task_branch_fields(args, fields)
    quote = vars(args).get("quote")
    send(args, "task_update", item=f"tasks/{args.id}", fields=fields, **({"quote": quote} if quote else {}))
    print(json.dumps({"task": args.id, **fields}))


def comma_list(text):
    return [part.strip() for part in text.split(",") if part.strip()]


def task_list(key: str, text: str) -> list[str]:
    if key == "split_pulls" and text.lstrip().startswith("["):
        return json.loads(text)
    return comma_list(text)


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
    say.add_argument("--reply-to")
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
    hierarchy = sub.add_parser("hierarchy").add_subparsers(dest="action", required=True)
    hierarchy.add_parser("backfill").add_argument("--apply", action="store_true")
    phase = sub.add_parser("phase")
    phase.add_argument("id")
    phase.add_argument("state")
    phase.add_argument("values", nargs="*")
    phase.add_argument("--status")
    phase.add_argument("--description", default="")
    phase.add_argument("--depends-on", default="")
    phase.add_argument("--planning", choices=["manual", "auto"], default=ledger_phases.PLANNING_DEFAULT)
    phase.add_argument("--release", action="store_true")
    phase.add_argument(
        "--not-duplicate", default="", help="why the phase differs from the one it resembles; skips the duplicate check"
    )
    followup = sub.add_parser("followup")
    followup.add_argument("action", choices=["add", "done", "open", "flag", "unflag"])
    followup.add_argument("value")
    followup.add_argument("--status")
    followup.add_argument("--needs-operator", action="store_true")
    followup.add_argument("--hive", help="hive whose master owns the new follow up")
    followup.add_argument(
        "--not-duplicate",
        default="",
        help="why the follow up differs from the item it resembles; skips the duplicate check",
    )
    question = sub.add_parser("question")
    question.add_argument("action", choices=["add"])
    question.add_argument("text")
    question.add_argument("--hive", help="hive whose master owns the new question")
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
    sub.add_parser("tree", help="print a plan, phase, slice or task and everything under it, with states").add_argument(
        "node", nargs="?", help="plans/<id>, phases/<id>, slices/<id> or tasks/<id>; default the whole ledger"
    )
    sub.add_parser("get", help="print one task, phase, follow up or priority through the ledger API").add_argument(
        "item", type=_ledger_item, help="tasks/<id>, phases/<id>, followups/<id> or priorities/<id>"
    )
    tasks = sub.add_parser("tasks", help="list open tasks, one line each, filtered by state, lane or title text")
    for flag in ("--state", "--lane", "--text"):
        tasks.add_argument(flag)
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
    fold = sub.add_parser("fold")
    fold.add_argument("action", choices=["candidates", "check", "apply"])
    fold.add_argument("plan", nargs="?")
    sub.add_parser("time-left").add_argument("minutes", type=_duration)
    sub.add_parser("claim").add_argument("item")
    task = sub.add_parser("task")
    task.add_argument("action", choices=["add", "set", "group", "split"])
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
        "--kind", choices=ledger_kinds.KINDS, help="code (default), ci, ops, troubleshoot, tune, research, workflow"
    )
    task.add_argument("--workflow", default="", help="workflow kind: the workflow name")
    task.add_argument("--case", help="workflow kind: the case file")
    task.add_argument("--must", default="", help="contract: what must be true when the task is done")
    task.add_argument("--check", default="", help="contract: how it is checked")
    task.add_argument("--judge", default="", help="contract: who judges it")
    task.add_argument(
        "--push",
        action="store_const",
        const="yes",
        default="",
        help="contract: the proof needs a pushed branch and a CI run, so never qa",
    )
    task.add_argument(
        "--scaffold", action="store_true", help="create the task's work folder now and store it as its workspace"
    )
    task.add_argument("--artifact", action="store_true", help="the operator asked this task for a file to review")
    task.add_argument(
        "--workstation",
        action="store_true",
        help="the task acts on the workstation checkout, so no remote hive runs it",
    )
    task.add_argument("--profile")
    task.add_argument("--overlays", help="comma separated overlays this task's agent wears, at most three")
    task.add_argument("--needs", help="comma separated hive capabilities, such as headed, that its hive must declare")
    task.add_argument(
        "--repositories", help="comma separated absolute local repository paths; defaults to the swarm repository"
    )
    task.add_argument("--rank", help="queue rank: urgent, high, normal (default) or low; next means urgent")
    task.add_argument(
        "--plan-slice", default="", help="task slice anchor; computes its plan lines; none uses the task description"
    )
    task.add_argument(
        "--follow-up", action="store_true", help="a follow up task: no slice in a sliced phase, judged by its text"
    )
    task.add_argument("--plan", default="", help="link to the published plan; default the phase's plan link")
    task.add_argument("--difficulty", choices=ledger_tasks.DIFFICULTIES, help="task size: S, M or L")
    task.add_argument(
        "--not-duplicate", default="", help="why the task differs from the one it resembles; skips the duplicate check"
    )
    task.add_argument("--quote", help="task set rank=...: the operator's words, so the rank stays his")
    publish = sub.add_parser("publish-plan")
    publish.add_argument("path", nargs="?", help="plan file; omit with --repoint to use the registered artifact")
    publish.add_argument(
        "--repoint", action="store_true", help="move an existing plan and all its phases to this server"
    )
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

    resolve_name = ports.swarm().naming.resolve_name

    args = build_parser().parse_args()
    if text := refusal(args.name, Who.from_env()):
        sys.exit(f"agentihooks ledger: {text}")
    args.name = resolve_name(args.name) if args.name else args.name
    if not args.slug or not (args.name or args.command in ("url", "show", "tree", "hierarchy", "get", "tasks")):
        sys.exit("--slug and --as are required")
    with ledger_route.session(args.slug):
        globals()[f"cmd_{args.command.replace('-', '_')}"](args)


if __name__ == "__main__":
    from scripts.swarm_ledger import composition

    composition.wire()
    main()
