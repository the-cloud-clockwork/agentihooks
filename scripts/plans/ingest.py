"""Plan ingest: put any plan into a ledger as linked phases and slices in one atomic operations batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.plans.normalize import Normalized, PlanError, normalize
from scripts.swarm_ledger import ledger_phase_cli, ledger_plans, plan_ranges

LIMIT = 100
OPERATOR = "operator"
MASTER = "master@"


class IngestError(ValueError):
    """A refusal the command prints and exits on."""


@dataclass(frozen=True)
class Author:
    """Who the ledger records as the plan's author, and whether that name is a seat that must join."""

    name: str
    seat: bool


@dataclass(frozen=True)
class Links:
    """Where the stored plan lives once uploaded, and the plan id it maps to."""

    plan: str
    file: dict[str, Any]
    artifact: str
    url: str
    task: str = ""
    source: dict[str, Any] | None = None


def author(name: str, swarm: str, slug: str, remote: bool) -> Author:
    """Resolve the author from the session's credential: a bound seat, the operator on the host, or a refusal."""
    if name and swarm:
        if swarm != slug:
            raise IngestError(f"a seat of swarm {swarm} cannot ingest a plan into ledger {slug}")
        return Author(name, True)
    if remote:
        raise IngestError(
            "an unbound session cannot ingest into a remote ledger; the operator credential stays on its host"
        )
    return Author(OPERATOR, False)


def plan_of(markdown: str) -> str:
    """The plan id the ledger derives from the stored artifact of this markdown."""
    return ledger_plans.plan_id(hashlib.sha256(markdown.encode()).hexdigest())


def entries(plan: Normalized, taken: list[str], planning: str = "") -> list[dict[str, Any]]:
    """The phase entries with their ledger ids, depends resolved and the planning override applied."""
    legacy: Any = ledger_phase_cli
    normalized: Any = plan
    content: dict[str, Any] = {
        **normalized.content,
        "phases": [{**phase, "planning": planning} if planning else phase for phase in normalized.content["phases"]],
    }
    return legacy.append_phases(content, taken)


def batch(plan: Normalized, phases: list[dict[str, Any]], links: Links, by: str) -> list[dict[str, Any]]:
    """Build the ops: the artifact, the plan, one phase op each with its plan links, then a slice per anchor."""
    legacy: Any = plan_ranges
    ranges: dict[str, str] = legacy.phase_lines(
        plan.markdown, [{"id": entry["phase"], "title": entry["title"]} for entry in phases]
    )
    ops: list[dict[str, Any]] = [
        {"op": "artifact_add", "task": links.task, "title": plan.title, "file": links.file, "plan": True},
        {
            "op": "plan_add",
            "plan": links.plan,
            "title": plan.title,
            "artifact": links.artifact,
            "url": links.url,
            **({"source": links.source} if links.source else {}),
        },
    ]
    for entry in phases:
        linked = {
            **entry,
            "plan": f"plans/{links.plan}",
            "plan_url": links.url,
            "plan_ref": {"artifact": links.artifact, "lines": ranges[entry["phase"]]},
        }
        ops.append(
            {"op": "phase_add", **linked} if "not_duplicate" in entry else {"op": "phase_append", "phases": [linked]}
        )
    for entry in phases:
        ops += [
            {"op": "slice_add", "phase": f"phases/{entry['phase']}", "anchor": anchor}
            for anchor in plan.slices[entry["title"]]
        ]
    if len(ops) > LIMIT:
        raise IngestError(f"the plan needs {len(ops)} operations; one ingest holds at most {LIMIT}")
    return [{**op, "id": f"{op['op']}-{links.plan}-{n}", "by": by} for n, op in enumerate(ops)]


def swarm_state(slug: str, connect: Callable[[], Any]) -> str:
    """Whether the ledger's swarm is running, paused, another stored state, or absent."""
    from scripts.swarm.store import SwarmError

    try:
        return connect().config(slug).state
    except SwarmError:
        return "absent"


def report(doc: Mapping[str, list[dict[str, Any]]], plan: str, state: str) -> dict[str, Any]:
    """The command's JSON: the plan, its phases, each phase's lifecycle and the swarm state."""
    from scripts.swarm import phase_state

    legacy: Any = phase_state
    linked = [phase for phase in doc["phases"] if phase.get("plan") == f"plans/{plan}"]
    return {
        "plan": plan,
        "phases": [phase["id"] for phase in linked],
        "lifecycle": {phase["id"]: legacy.lifecycle(phase, doc) for phase in linked},
        "swarm": state,
    }


def source(env: Mapping[str, str], path: str, markdown: str) -> dict[str, Any]:
    """The provenance an operator-authored plan row records."""
    return {
        "harness": env.get("AGENTIHOOKS_TARGET", "claude"),
        "session": env.get("CLAUDE_CODE_SESSION_ID") or env.get("CLAUDE_SESSION_ID", ""),
        "path": "" if path == "-" else str(Path(path).resolve()),
        "sha256": hashlib.sha256(markdown.encode()).hexdigest(),
    }


def parser() -> argparse.ArgumentParser:
    """The argument parser of agentihooks plan ingest."""
    found = argparse.ArgumentParser(prog="agentihooks plan ingest", description=__doc__)
    found.add_argument("path", help="plan file, or - for standard input")
    found.add_argument("--ledger", required=True, help="target ledger slug")
    found.add_argument("--planning", choices=("auto", "manual"), default="", help="planning for every phase")
    found.add_argument("--dry-run", action="store_true", help="print what would be written and write nothing")
    found.add_argument("--repo", default="", help="OWNER/NAME for the plan issue; default the current repo")
    return found


def read_plan(path: str) -> bytes:
    """The plan bytes from a file or standard input."""
    return sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()


def ingest(args: argparse.Namespace, env: Mapping[str, str]) -> dict[str, Any]:
    """Run one ingest and return its JSON report, or raise IngestError."""
    from scripts.gates import base
    from scripts.swarm import store
    from scripts.swarm_ledger import ledger as ledger_module
    from scripts.swarm_ledger import ledger_link

    identity: Any = base
    ledger: Any = ledger_module
    link: Any = ledger_link
    swarm: Any = store

    slug = args.ledger
    who = identity.Who.from_env(env)
    by = author(who.name, who.swarm, slug, link.remote(env, slug=slug))
    doc: dict[str, list[dict[str, Any]]] = {
        key: ledger.resource(slug, key, collection=True) for key in ("phases", "tasks", "plans")
    }
    try:
        plan = normalize(
            read_plan(args.path), "stdin.md" if args.path == "-" else args.path, [p["id"] for p in doc["phases"]]
        )
    except PlanError as exc:
        raise IngestError(str(exc)) from None
    plan_id = plan_of(plan.markdown)
    if any(row["id"] == plan_id for row in doc["plans"]):
        return {**report(doc, plan_id, swarm_state(slug, swarm.connect)), "ingested": False, "note": "already ingested"}
    phases = entries(plan, [p["id"] for p in doc["phases"]], args.planning)
    placeholder = Links(plan_id, {}, "https://ledger.invalid/plan", "https://ledger.invalid/plan")
    ops = batch(plan, phases, placeholder, by.name)
    if args.dry_run:
        return {
            "dry_run": True,
            "plan": plan_id,
            "by": by.name,
            "phases": [e["phase"] for e in phases],
            "ops": len(ops),
        }
    if by.seat:
        ledger.refused(ledger.call(slug, [{"op": "join", "id": f"join-{plan_id}", "by": by.name, "role": "member"}]))
    ops, url, where = publish(args, env, plan, phases, by, plan_id)
    state = ledger.call(slug, ops)
    if state.get("rejected") and where == "issue":
        from scripts.swarm_ledger import ledger_publish

        publisher: Any = ledger_publish
        publisher.close_issue(url)
    ledger.refused(state, ops)
    after: dict[str, list[dict[str, Any]]] = {
        key: ledger.resource(slug, key, collection=True) for key in ("phases", "tasks")
    }
    if not by.name.startswith(MASTER):
        notify(slug, plan, by.name, len(phases))
    return {
        **report(after, plan_id, swarm_state(slug, swarm.connect)),
        "ingested": True,
        "plan_url": url,
        "published_to": where,
    }


def publish(
    args: argparse.Namespace,
    env: Mapping[str, str],
    plan: Normalized,
    phases: list[dict[str, Any]],
    by: Author,
    plan_id: str,
) -> tuple[list[dict[str, Any]], str, str]:
    """Upload the canonical markdown, open the issue where the repo has issues, and build the batch."""
    from scripts.swarm_ledger import ledger as ledger_module
    from scripts.swarm_ledger import ledger_publish

    ledger: Any = ledger_module
    publisher: Any = ledger_publish
    stored: dict[str, Any] = {}
    task = env.get("AGENTIHOOKS_SWARM_TASK", "") if by.seat else ""

    def artifact(path: str, title: str) -> str:
        stored["file"] = ledger.upload_artifact(
            args.ledger, by.name, path, {"task": task, "title": title, "plan": True}
        )
        return f"{ledger.base()}/artifacts/{args.ledger}/{stored['file']['id']}"

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"{Path('stdin' if args.path == '-' else args.path).stem}.md"
        path.write_bytes(plan.markdown.encode())
        try:
            url, where = publisher.publish(str(path), plan.title, args.repo, artifact, issue_title=plan.title)
        except ledger_publish.PublishError as exc:
            raise IngestError(str(exc)) from None
    artifact_url = f"{ledger.base()}/artifacts/{args.ledger}/{stored['file']['id']}"
    origin = None if by.seat else source(env, args.path, plan.markdown)
    links = Links(plan_id, stored["file"], artifact_url, url, task, origin)
    return batch(plan, phases, links, by.name), url, where


def notify(slug: str, plan: Normalized, by: str, count: int) -> None:
    """Tell the ledger's master, as information, that a plan landed."""
    from scripts.inbox import cli as inbox

    legacy: Any = inbox
    with redirect_stdout(sys.stderr):
        legacy.main(["send", f"{MASTER}{slug}", "--fyi", f"{by} ingested the plan {plan.title} as {count} phases."])


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Entry point of agentihooks plan ingest."""
    args = parser().parse_args(argv)
    try:
        result = ingest(args, os.environ if environ is None else environ)
    except IngestError as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(result))
    return 0
