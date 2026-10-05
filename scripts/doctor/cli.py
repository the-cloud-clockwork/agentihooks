"""agentihooks doctor: a Doctor crew that watches the swarm on one ledger from its own linked ledger and swarm.

agentihooks doctor <slug> start [--repo DIR]   create ledger <slug>-doctor, link both ledgers, pair the two
                                               masters as inbox peers and start the Doctor swarm
agentihooks doctor <slug> stop                 close the Doctor ledger with every fix and the number it moved,
                                               retire its agents (the operator's chat line rig doctor stop does too)
agentihooks doctor <slug> status               the link, the peers and the Doctor swarm's status
agentihooks doctor <slug> verdict FINDING VERDICT [--note TEXT]
                                               the Doctor master judges a Doctor finding
agentihooks doctor <slug> task FINDING --fix code|tune
                                               an established or early-real finding becomes a troubleshoot task and
                                               a fix task naming the number to move
agentihooks doctor <slug> measure FINDING      run the detectors once and print the finding's number, 0 when gone
agentihooks doctor <slug> intervene ACTION [--to ADDRESS] [--text TEXT] [--file FILE]
                                               apply a merged fix to the watched swarm: pull-dev, restart-ledger-server,
                                               refresh-rules, culture, handoff-at-stop or message; logged on both ledgers
<slug> names the watched ledger or its Doctor ledger. Start refuses without a linked bundle.
Every swarm tick runs the detectors once per AGENTIHOOKS_DOCTOR_INTERVAL_MINUTES (10) and closes the Doctor after
AGENTIHOOKS_DOCTOR_QUIET_MINUTES (120) with no new finding.
"""

import argparse
import json
import os
import sys
from argparse import Namespace
from pathlib import Path

from scripts.doctor import detect, interventions, loop
from scripts.doctor.priming import SUFFIX
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import cli as swarm
from scripts.swarm.health.verdicts import VERDICTS
from scripts.swarm.ledger_client import LEDGER_DIR
from scripts.swarm.store import SwarmError

TEMPLATE = "doctor"
BY = "doctor"
ROOT = Path(__file__).resolve().parents[2]
NOTE_MAX = 4000
QUIET = "Closed on its own after two hours with no new finding.\n"
PHASES = [
    {"title": "Watch", "description": "Run the detectors on a timer and give every finding a verdict."},
    {
        "title": "Fix",
        "description": "Each finding judged real becomes a task naming the number it must move, measured before "
        "and after.",
    },
    {
        "title": "Apply",
        "description": "Apply each merged fix to the running swarm with the allowed interventions and log it on "
        "both ledgers.",
    },
]
POINTER = (
    "A Doctor crew now watches this swarm. Its ledger is listed under the original sources, and the two masters "
    "keep each other in sync through their inboxes."
)
OPENING = (
    "The Doctor crew on the linked ledger {doctor} now watches your swarm. Keep me in sync through the inbox: "
    "send me what changes in your swarm, and I send you each fix I apply and each intervention I make."
)


def linked_bundle():
    from scripts.install import _get_bundle_path

    return _get_bundle_path()


def _new_ledger():
    if str(LEDGER_DIR) not in sys.path:
        sys.path.insert(0, str(LEDGER_DIR))
    import new_ledger

    return new_ledger


def _path(slug):
    return str(_new_ledger().core.paths(slug)[1])


def _create_ledger(doctor, slug, title):
    overview = (
        f"The Doctor crew watches the swarm on the ledger {slug}, finds where its agents fall short, fixes the "
        "system with proof and applies each fix to the running swarm. Its master and the master of the watched "
        "swarm keep each other in sync through the inbox."
    )
    return _new_ledger().create(
        doctor, {"title": f"Doctor for {title}", "overview": overview, "phases": PHASES}, "swarm"
    )


def _link(ledger, slug, doctor):
    ledger.add_source(doctor, _path(slug), BY)
    if _path(doctor) not in ledger.state(slug).get("sources", []):
        ledger.add_source(slug, _path(doctor), BY)
        ledger.say(slug, POINTER, by="swarm")


def _pair(store, slug, doctor):
    first = store.peer(slug) != doctor
    store.set_peer(slug, doctor)
    store.set_peer(doctor, slug)
    if first:
        InboxStore(store.redis).send(f"master@{doctor}", f"master@{slug}", OPENING.format(doctor=doctor))


def _pair_of(store, slug):
    if slug.endswith(SUFFIX) and slug in store.slugs() and store.config(slug).template == TEMPLATE:
        slug = slug.removesuffix(SUFFIX)
    doctor = slug + SUFFIX
    if doctor not in store.slugs():
        raise SwarmError(f"no Doctor watches {slug}; start one with agentihooks doctor {slug} start")
    return slug, doctor


def cmd_start(store, args):
    bundle = linked_bundle()
    if bundle is None:
        raise SwarmError("rig doctor needs a linked bundle for its rules: link one with agentihooks init --bundle DIR")
    slug, doctor = args.slug, args.slug + SUFFIX
    if not swarm.SLUG_RE.match(doctor):
        raise SwarmError(f"the Doctor swarm id {doctor} is longer than a swarm id may be, 48 characters")
    ledger = swarm.LedgerClient()
    _create_ledger(doctor, slug, ledger.state(slug).get("title") or slug)
    _link(ledger, slug, doctor)
    if doctor not in store.slugs():
        repo = args.repo or str(ROOT if (ROOT / ".git").exists() else bundle)
        swarm.cmd_create(
            store, Namespace(slug=doctor, repo=repo, template=TEMPLATE, max_eng_agents=None, max_ci_agents=None)
        )
    _pair(store, slug, doctor)
    loop.reset(store, doctor)
    if ledger.closed(doctor):
        swarm.cmd_reopen(store, Namespace(slug=doctor, name="operator"))
    else:
        swarm.cmd_start(store, Namespace(slug=doctor))


def moved(task):
    proof, contract = task.get("proof") or {}, task.get("contract") or {}
    return proof.get("output") or contract.get("must") or "no number recorded"


def fixes_note(tasks):
    done = [t for t in tasks if t.get("state") == "done" and not t.get("out_of_scope")]
    lines = [f"Fixes {len(done)}:"]
    lines += [f"- {t['title']}: moved {moved(t)}" + (f" {t['pr_url']}" if t.get("pr_url") else "") for t in done]
    return "\n".join(lines)[:NOTE_MAX]


def _close(store, slug, doctor, lead=""):
    note = (lead + fixes_note(swarm.LedgerClient().tasks(doctor)))[:NOTE_MAX]
    swarm.cmd_close(store, Namespace(slug=doctor, note=note, now=True, name="operator"))
    store.clear_peer(slug)
    store.clear_peer(doctor)


def cmd_stop(store, args):
    _close(store, *_pair_of(store, args.slug))


def timer(store, doctor, now_ms):
    ledger = swarm.LedgerClient()
    return loop.run(
        store,
        doctor,
        now_ms,
        lambda watched: detect.collect(detect.readers(store, ledger, watched, now_ms)),
        lambda: _close(store, store.peer(doctor), doctor, QUIET),
    )


def cmd_status(store, args):
    slug, doctor = _pair_of(store, args.slug)
    paired = store.peer(slug) == doctor and store.peer(doctor) == slug
    closed = swarm.LedgerClient().closed(doctor)
    print(
        f"doctor {doctor} watches {slug}  peers {'registered' if paired else 'not registered'}  "
        f"ledger {'closed' if closed else 'open'}"
    )
    swarm.cmd_status(store, Namespace(slug=doctor, json=False))


def _name():
    return os.environ.get("AGENTIHOOKS_AGENT_NAME") or BY


def cmd_verdict(store, args):
    _, doctor = _pair_of(store, args.slug)
    name = os.environ.get("AGENTIHOOKS_AGENT_NAME") or "operator"
    verdict = loop.verdicts(store, doctor).judge(args.finding, args.verdict, args.note, name, swarm.now_ms())
    print(json.dumps({"finding": args.finding, "verdict": verdict["value"]}))


def cmd_task(store, args):
    slug, doctor = _pair_of(store, args.slug)
    specs = loop.fix_tasks(slug, *loop.judged(store, doctor, args.finding), args.fix)
    ledger = swarm.LedgerClient()
    for spec in specs:
        ledger.add_task(doctor, spec, _name())
    print(json.dumps({"finding": args.finding, "tasks": [spec["task"] for spec in specs]}))


def cmd_measure(store, args):
    slug, _ = _pair_of(store, args.slug)
    found, failed = detect.collect(detect.readers(store, swarm.LedgerClient(), slug, swarm.now_ms()))
    for line in failed:
        print(line, file=sys.stderr)
    print(f"{args.finding} {next((f.measure for f in found if f.id == args.finding), 0)}")


def cmd_intervene(store, args):
    slug, doctor = _pair_of(store, args.slug)
    ctx = interventions.Context(store, swarm.LedgerClient(), slug, doctor, _name())
    print(json.dumps({"intervention": args.action, "logged": interventions.apply(ctx, args.action, args)}))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="agentihooks doctor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("slug")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start").add_argument("--repo", default="")
    sub.add_parser("stop")
    sub.add_parser("status")
    verdict = sub.add_parser("verdict")
    verdict.add_argument("finding")
    verdict.add_argument("verdict", choices=VERDICTS)
    verdict.add_argument("--note", default="")
    task = sub.add_parser("task")
    task.add_argument("finding")
    task.add_argument("--fix", required=True, choices=loop.FIXES)
    sub.add_parser("measure").add_argument("finding")
    intervene = sub.add_parser("intervene")
    intervene.add_argument("action")
    for flag in ("--to", "--text", "--file"):
        intervene.add_argument(flag, default="")
    return parser


COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "verdict": cmd_verdict,
    "task": cmd_task,
    "measure": cmd_measure,
    "intervene": cmd_intervene,
}


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        COMMANDS[args.command](swarm.connect(), args)
    except (SwarmError, InboxError) as exc:
        print(f"doctor: {exc}", file=sys.stderr)
        return 1
    return 0
