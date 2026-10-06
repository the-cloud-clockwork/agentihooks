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
                                      ledger, planned manually and in review; a taken phase id refuses them all
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
  priority add ITEM TEXT              ask the operator: only what blocks on his answer, at most 20 words; ITEM may
                                      be phases/<id>, questions/<id>, followups/<id> or tasks/<id>. Unanswered
                                      questions, blocked tasks, merge approvals and flagged follow-ups show on their own
  priority clear ID... | --all        clear priorities once answered
  relay ITEM TEXT --quote WORDS       post the operator's decision from this pane as his answer to questions/<id>
                                      or his comment on another item; WORDS must be in an operator prompt or
                                      AskUserQuestion answer this session recorded in the last hour
  time-left DURATION                 record remaining time, e.g. "3h 20m"
  claim ITEM                          take ownership of an item's operator events
  task add ID TITLE --lane eng|ci [--phase P] [--description D] [--depends-on IDS] [--territory AREAS] [--gain N] [--profile NAME]
           [--kind K] [--must M --check C --judge J] [--scaffold] [--artifact]
                                      add a swarm task; IDS and AREAS are comma separated; K is code (default), ci,
                                      ops, troubleshoot, tune or research; M, C, J form its proof contract;
                                      --scaffold creates its work folder (steering, progress, proof) in the same call;
                                      --artifact marks a file the operator asked for, so the task may publish it
  task set ID FIELD=VALUE...          set state, claimed_by, issue_url, pr_url, depends_on, territory, kind or
                                      artifact (yes or no) of a task;
                                      proof.KEY=VALUE and contract.KEY=VALUE pairs form one object, e.g.
                                      proof.command=C proof.output=O
  prompt                              print the join paragraph for a launch prompt
  url                                 print the ledger page link for the operator (no --as needed)

Agent text is for the operator: plain words, what was done or why it was skipped. The server refuses
clock times, dates, hashes, run ids, file names, code identifiers, capital labels, dashes, arrows,
AI phrasing, more than one parenthesis or semicolon, and comments over 50 words (chat 100, items 40).

Env: LEDGER_DIR, LEDGER_HOST (127.0.0.1), LEDGER_PORT (8765).
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger_artifacts  # noqa: E402
import ledger_comments  # noqa: E402
import ledger_core as core  # noqa: E402
import ledger_gate as gate  # noqa: E402
import ledger_kinds  # noqa: E402
import ledger_link  # noqa: E402
import ledger_publish  # noqa: E402
import ledger_tasks  # noqa: E402
import ledger_workspace  # noqa: E402
import watch_ledger  # noqa: E402

BASE = ledger_link.base()
TALK_REFUSED = "talk refused"


def request(slug, ops=None):
    token = core.read_token(core.paths(slug)[0].read_text(encoding="utf-8")) or ""
    headers = {"Content-Type": "application/json", "X-Ledger-Token": token}
    body = None if ops is None else json.dumps({"ops": ops}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/{slug}?view=agent", data=body, headers=headers, method="GET" if ops is None else "PUT"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def call(slug, ops=None):
    try:
        return request(slug, ops)
    except urllib.error.HTTPError as exc:
        sys.exit(f"server refused: {exc.code} {exc.read().decode(errors='replace')}")
    except OSError:
        subprocess.run([sys.executable, str(HERE / "ledger_server.py"), "--ensure"], check=False, capture_output=True)
    try:
        return request(slug, ops)
    except (OSError, urllib.error.HTTPError) as exc:
        sys.exit(f"ledger server not answering on {BASE}: {exc}")


def op(kind, args, /, **fields):
    return {"op": kind, "id": f"{kind}-{uuid.uuid4().hex[:10]}", "by": args.name, **fields}


def talk_refused(state):
    if state.get("rejected"):
        gated = [w for w in state.get("_meta", {}).get("warnings", []) if w.startswith(TALK_REFUSED)]
        if gated:
            sys.exit("; ".join(gated))


def posted(state):
    talk_refused(state)
    print(json.dumps({"posted": not state.get("rejected")}))


def send(args, kind, /, **fields):
    state = call(args.slug, [op(kind, args, **fields)])
    talk_refused(state)
    if state.get("rejected"):
        if kind.startswith("phase_") or kind == "task_add":
            sys.exit("; ".join(state.get("_meta", {}).get("warnings", [])) or f"rejected: {state['rejected']}")
        sys.exit(f"rejected: {state['rejected']}")
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
    op = {"op": "add", "thread": "chat", "id": f"m-{uuid.uuid4().hex[:10]}", "text": text, "by": args.name}
    if args.long:
        op["long"] = True
    state = call(args.slug, [op])
    posted(state)


def upload_image(slug: str, name: str, path: str) -> dict:
    return upload(slug, name, path, "media", {})


def upload_artifact(slug: str, name: str, path: str) -> dict:
    return upload(slug, name, path, "artifacts", {"X-Artifact-Name": Path(path).name})


def upload(slug: str, name: str, path: str, route: str, extra: dict) -> dict:
    token = core.read_token(core.paths(slug)[0].read_text(encoding="utf-8")) or ""
    req = urllib.request.Request(
        f"{BASE}/api/{route}/{slug}",
        data=Path(path).read_bytes(),
        headers={
            "X-Ledger-Token": token,
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
    state = call(args.slug, [entry])
    posted(state)


def cmd_artifact(args):
    file = upload_artifact(args.slug, args.name, args.path)
    task = args.task if args.task is not None else os.environ.get("AGENTIHOOKS_SWARM_TASK", "")
    request = {"request": args.request} if args.request else {}
    state = call(args.slug, [op("artifact_add", args, task=task, title=args.title, file=file, **request)])
    print(json.dumps({"published": not state.get("rejected")}))
    if state.get("rejected"):
        if ledger_artifacts.REFUSED in state.get("_meta", {}).get("warnings", []):
            sys.exit(f"rejected: {ledger_artifacts.REFUSED}")
        sys.exit("rejected: join the ledger first and name a task it holds")


def cmd_publish_plan(args):
    phases = comma_list(args.phase)
    if not phases:
        sys.exit("publish-plan needs --phase with the ids of the phases the plan fills")
    title = args.title or ledger_publish.title_of(Path(args.path).read_text(encoding="utf-8"), phases)

    def artifact(path, title):
        file = upload_artifact(args.slug, args.name, path)
        task = os.environ.get("AGENTIHOOKS_SWARM_TASK", "")
        send(args, "artifact_add", task=task, title=title, file=file, plan=True)
        return f"{BASE}/artifacts/{args.slug}/{file['id']}"

    try:
        url, where = ledger_publish.publish(args.path, title, args.repo, artifact)
    except ledger_publish.PublishError as exc:
        sys.exit(str(exc))
    ops = []
    for phase in phases:
        ops.append(op("phase_update", args, item=f"phases/{phase}", fields={"plan_url": url}))
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
    state = call(args.slug, ops)
    if state.get("rejected"):
        sys.exit("; ".join(state.get("_meta", {}).get("warnings", [])) or f"rejected: {state['rejected']}")
    print(json.dumps({"plan_url": url, "published_to": where, "phases": phases}))


def cmd_plan(args):
    from scripts.swarm_ledger import ledger_phase_cli

    plan = json.loads(Path(args.path).read_text(encoding="utf-8"))
    phases = ledger_phase_cli.append_phases(plan, [phase["id"] for phase in call(args.slug)["phases"]])
    send(args, "phase_append", phases=phases)
    print(json.dumps({"appended": [phase["phase"] for phase in phases], "planning": "manual", "review": "pending"}))


def cmd_artifact_purge(args):
    state = send(args, "artifact_purge")
    event = next(e for e in reversed(state["_meta"]["events"]) if e["kind"] == "artifacts purged")
    print(json.dumps({"purged": event["count"], "artifacts": len(state["artifacts"])}))


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
    if rejected:
        sys.exit(1)


def cmd_relay(args):
    import ledger_relay

    if not ledger_relay.verified(args.name, args.quote):
        sys.exit(
            "relay refused: the quote is not in an operator prompt or answer this session recorded in the last hour"
        )
    send(args, "relay", item=args.item, text=args.text, quote=args.quote)
    print(json.dumps({"relayed": True, "item": args.item}))


def cmd_scope(args):
    send(args, "set", **with_status(args, path=f"{args.item}/out_of_scope", value=args.state == "out"))
    print(json.dumps({"item": args.item, "scope": args.state}))


def cmd_retext(args):
    send(args, "retext", item=args.item, text=args.text)
    print(json.dumps({"retext": args.item}))


def thread_of(target):
    return "chat" if target == "chat" else f"{target}/comments"


def cmd_edit(args):
    state = call(
        args.slug,
        [{"op": "edit", "thread": thread_of(args.target), "id": args.entry, "text": args.text, "by": args.name}],
    )
    if state.get("rejected"):
        sys.exit(f"rejected: {state['rejected']} (not yours, the operator's, or missing)")
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


def cmd_audit(args):
    rows = ledger_comments.audit(call(args.slug))
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


def cmd_task(args):
    if args.action == "add":
        if args.id == "-":
            args.id = ledger_tasks.next_id(call(args.slug).get("tasks", []))
        title = " ".join(args.values)
        lists = {k: comma_list(v) for k, v in (("depends_on", args.depends_on), ("territory", args.territory)) if v}
        if args.gain is not None:
            lists["gain"] = args.gain
        contract = {k: getattr(args, k) for k in ("must", "check", "judge") if getattr(args, k)}
        if contract:
            lists["contract"] = contract
        if args.kind:
            lists["kind"] = args.kind
        if args.artifact:
            lists["artifact"] = True
        if args.profile:
            lists["profile"] = args.profile
        if args.plan:
            lists["plan_url"] = args.plan
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
    fields = dict(value.split("=", 1) for value in args.values if "=" in value)
    if len(fields) != len(args.values):
        sys.exit("task set takes FIELD=VALUE pairs")
    for key in ("depends_on", "territory"):
        if key in fields:
            fields[key] = comma_list(fields[key])
    if "artifact" in fields:
        if fields["artifact"] not in ("yes", "no"):
            sys.exit("task set takes artifact=yes or artifact=no")
        fields["artifact"] = fields["artifact"] == "yes"
    for dotted in [key for key in fields if "." in key]:
        name, _, sub = dotted.partition(".")
        if not isinstance(fields.get(name, {}), dict):
            sys.exit(f"task set takes {name}=VALUE or {name}.KEY=VALUE pairs, not both")
        fields.setdefault(name, {})[sub] = fields.pop(dotted)
    send(args, "task_update", item=f"tasks/{args.id}", fields=fields)
    print(json.dumps({"task": args.id, **fields}))


def comma_list(text):
    return [part.strip() for part in text.split(",") if part.strip()]


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
    phase = sub.add_parser("phase")
    phase.add_argument("id")
    phase.add_argument("state")
    phase.add_argument("values", nargs="*")
    phase.add_argument("--status")
    phase.add_argument("--description", default="")
    phase.add_argument("--depends-on", default="")
    phase.add_argument("--planning", choices=["manual", "auto"], default="manual")
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
    priority = sub.add_parser("priority")
    priority.add_argument("action", choices=["add", "clear"])
    priority.add_argument("values", nargs="*")
    priority.add_argument("--all", action="store_true")
    relay = sub.add_parser("relay")
    relay.add_argument("item")
    relay.add_argument("text")
    relay.add_argument("--quote", required=True)
    sub.add_parser("time-left").add_argument("minutes", type=_duration)
    sub.add_parser("claim").add_argument("item")
    task = sub.add_parser("task")
    task.add_argument("action", choices=["add", "set"])
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
    task.add_argument("--plan", default="", help="link to the published plan; default the phase's plan link")
    publish = sub.add_parser("publish-plan")
    publish.add_argument("path")
    publish.add_argument("--phase", required=True, help="comma separated ids of the phases the plan fills")
    publish.add_argument("--title", default="", help="default the plan's first heading")
    publish.add_argument("--repo", default="", help="OWNER/NAME for the issue; default the current repo")
    plan = sub.add_parser("plan")
    plan.add_argument("action", choices=["phases"])
    plan.add_argument("path", help="JSON file with the plan's phases list, the init-swarm content shape")
    return parser


def main():
    from scripts.gates import Who
    from scripts.gates.identity import refusal
    from scripts.swarm.naming import resolve_name

    args = build_parser().parse_args()
    if text := refusal(args.name, Who.from_env()):
        sys.exit(f"agentihooks ledger: {text}")
    args.name = resolve_name(args.name) if args.name else args.name
    if not args.slug or not (args.name or args.command == "url"):
        sys.exit("--slug and --as are required")
    globals()[f"cmd_{args.command.replace('-', '_')}"](args)


if __name__ == "__main__":
    main()
