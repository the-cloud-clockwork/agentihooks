#!/usr/bin/env python3
"""Launch and step the operator's troubleshooting crew on one swarm ledger.

Usage: crew.py STAGE SLUG [options]; every stage takes --dry-run, which prints the commands and writes nothing.

plan     prints the whole crew plan for the given counts, models and efforts and runs nothing.
scout    saves the earlier focus and eng lane settings, sets the eng lane to the scout model and effort, adds one
         eng troubleshoot task per scout, focuses only the crew, records the order as a canon seat note and prints
         the thirty minute intent cron and the restore command.
fix      refuses until every scout task is done, then adds the fixer tasks for the proven causes and focuses them.
critics  prints the critic sub agents the master launches over the fixers' pull requests.
harden   adds the hardening task and focuses it.
restore  unfocuses the crew, focuses the earlier targets in their order and puts the eng lane settings back.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, astuple, dataclass, field
from pathlib import Path
from typing import Any

ORDER = (
    "You will send 5 scouts agents running opus effort max each to trouble shoot. You will continue this until the "
    "cause is proven. Once the cause is proven you will send 5 engineers working together to fix the issues running "
    "opus effort xhigh and complete the priorities. Then you will send 3 qa agenrs critics running fable effort xhigh "
    "to critic everything and the engineers will fix it again. Once the old fallback is removed and we know our tick "
    "and controller is working fine we going to blind it with feature map testing, semgrep, unit test, smoke test and "
    "anti regression. This is the verbatin intent that the cron should remember the master every 30 mins so never "
    "drift. This is the only priority. Only this and follow ups real resulting from the troubleshooting priority "
    "medium, high and critical."
)
LAUNCH_QUOTE = (
    "when we face for lets say 2 hours max a problem that cannot solve we stop all work and launch that skill so the "
    "master gets a bruteforce crew"
)
HARDENING = ("feature map tests", "Semgrep rules", "unit tests", "smoke tests", "anti regression tests")
CRON = "*/30 * * * *"
EFFORT_RANK = ("low", "medium", "high", "xhigh", "max")
STATE_NAME = "troubleshooting-crew.json"
ROLES = ("scouts", "fixers", "critics")

Runner = Callable[[list[str]], str]


@dataclass(frozen=True)
class Stage:
    """One crew stage: how many agents, on which model and effort."""

    count: int
    model: str
    effort: str


@dataclass(frozen=True)
class Crew:
    """The crew layout, the operator's order by default."""

    scouts: Stage = Stage(5, "opus", "max")
    fixers: Stage = Stage(5, "opus", "xhigh")
    critics: Stage = Stage(3, "fable", "xhigh")
    harden: bool = True


@dataclass
class State:
    """What a launch saved so later stages and the restore can run."""

    crew_id: str
    problem: str
    crew: dict[str, Any]
    earlier_focus: list[str] = field(default_factory=list)
    earlier_lane: dict[str, str] = field(default_factory=dict)
    scout_tasks: list[str] = field(default_factory=list)
    fixer_tasks: list[str] = field(default_factory=list)
    harden_task: str = ""
    causes: list[str] = field(default_factory=list)
    launched: bool = False
    stopped_agents: list[dict[str, str]] = field(default_factory=list)


def crew_id(now: float) -> str:
    """Name a crew from the UTC minute it launched."""
    return "crew-" + time.strftime("%m%d%H%M", time.gmtime(now))


def scout_ids(cid: str, count: int, start: int = 0) -> list[str]:
    """Task ids of the scouts of crew ``cid``, numbered after the ``start`` scouts it already has."""
    return [f"{cid}-scout-{n}" for n in range(start + 1, start + count + 1)]


def fixer_ids(cid: str, count: int) -> list[str]:
    """Task ids of the fixers of crew ``cid``."""
    return [f"{cid}-fix-{n}" for n in range(1, count + 1)]


def hypotheses_for(count: int, given: Sequence[str]) -> list[str]:
    """One hypothesis per scout; scouts past the given ones hunt for a cause no other scout holds."""
    spare = "open: find a cause no other scout's hypothesis names"
    return [given[n] if n < len(given) else spare for n in range(count)]


def swarm(slug: str, *rest: str) -> list[str]:
    """An agentihooks swarm command on ``slug``."""
    return ["agentihooks", "swarm", slug, *rest]


def ledger(slug: str, who: str, *rest: str) -> list[str]:
    """An agentihooks ledger command on ``slug`` as ``who``."""
    return ["agentihooks", "ledger", "--slug", slug, "--as", who, *rest]


def lane_commands(slug: str, stage: Stage) -> list[list[str]]:
    """Set the eng lane to the stage's model and effort, raising the swarm effort ceiling when it is lower."""
    settings = [f"eng-model={stage.model}", f"eng-effort={stage.effort}"]
    if stage.effort == "max":
        settings.append("effort-max=max")
    return [swarm(slug, "set", *settings)]


def focus_commands(slug: str, tasks: Sequence[str], quote: str, only: bool) -> list[list[str]]:
    """Focus ``tasks`` in order; with ``only`` the first focus drops every other focus."""
    reason = "troubleshooting crew"
    commands = []
    for n, task in enumerate(tasks):
        flags = ["--only"] if only and n == 0 else ["--bottom"]
        commands.append(swarm(slug, "focus", f"tasks/{task}", *flags, "--reason", reason, "--quote", quote))
    return commands


def scout_task(slug: str, who: str, task: str, problem: str, hypothesis: str) -> list[str]:
    """Add one scout as an eng lane troubleshoot task: an engineer in scouting mode, no code."""
    description = (
        f"Problem: {problem}\nHypothesis to prove or refute: {hypothesis}\n"
        "Scouting mode: read live data and the code, write no code and commit nothing. Mark every claim confirmed, "
        "refuted or unproven with its evidence. Close with done and the root cause, the evidence and the filed follow up."
    )
    return ledger(
        slug,
        who,
        "task",
        "add",
        task,
        f"Troubleshooting crew {task}: {hypothesis}"[:200],
        "--not-duplicate",
        "one scout of the troubleshooting crew, each with its own hypothesis",
        "--lane",
        "eng",
        "--kind",
        "troubleshoot",
        "--rank",
        "urgent",
        "--description",
        description,
        "--must",
        "the hypothesis is confirmed, refuted or unproven with evidence",
        "--check",
        "the evidence cited on the task",
        "--judge",
        "master",
    )


def fixer_task(slug: str, who: str, task: str, causes: Sequence[str], peers: Sequence[str]) -> list[str]:
    """Add one fixer as an eng lane code task over the proven causes."""
    proven = "\n".join(f"- {cause}" for cause in causes)
    description = (
        f"Proven causes:\n{proven}\nFix only what these causes name, working together with the fixer tasks "
        f"{', '.join(peers)} through the inbox, and complete the priorities. Every fix ships its regression test."
    )
    return ledger(
        slug,
        who,
        "task",
        "add",
        task,
        f"Troubleshooting crew {task}",
        "--not-duplicate",
        "one fixer of the troubleshooting crew working with its peers",
        "--lane",
        "eng",
        "--kind",
        "code",
        "--rank",
        "urgent",
        "--description",
        description,
    )


def harden_task(slug: str, who: str, task: str, causes: Sequence[str]) -> list[str]:
    """Add the hardening task that locks the fixed behaviour."""
    description = (
        "Lock the fixed behaviour once the fixes are merged and proven live: "
        + ", ".join(HARDENING)
        + ". Causes fixed: "
        + "; ".join(causes)
    )
    return ledger(
        slug,
        who,
        "task",
        "add",
        task,
        f"Troubleshooting crew {task}",
        "--not-duplicate",
        "the hardening task of one troubleshooting crew",
        "--lane",
        "eng",
        "--kind",
        "code",
        "--rank",
        "urgent",
        "--description",
        description,
    )


def layout(crew: Crew) -> str:
    """This run's crew layout in plain words."""
    stages = (("scouts", crew.scouts), ("fixers", crew.fixers), ("critics", crew.critics))
    return ", ".join(f"{role} {stage.count} at {stage.model} {stage.effort}" for role, stage in stages)


def canon_note(slug: str, crew: Crew) -> list[str]:
    """Record the crew order verbatim and this run's layout as a canon seat note."""
    text = (
        f'Troubleshooting crew order of the operator, verbatim: "{ORDER}" This run\'s layout: {layout(crew)}. '
        "Follow it until the crew ends because the operator made this crew the only priority while it runs"
    )
    return swarm(slug, "learned", text, "--maturity", "canon")


def cron(slug: str, crew: Crew) -> dict[str, Any]:
    """The session cron the master sets: the verbatim intent every thirty minutes and this run's layout."""
    prompt = f"Troubleshooting crew on {slug}, the operator's order: {ORDER} This run's layout: {layout(crew)}."
    return {"cron": CRON, "recurring": True, "prompt": prompt}


def critic_agents(crew: Crew, state: State) -> list[dict[str, Any]]:
    """The critic sub agents the master launches; each reviews every fixer pull request and edits nothing."""
    brief = (
        f"Critic for the troubleshooting crew on: {state.problem}. Proven causes: {'; '.join(state.causes)}. "
        f"Review every pull request of the tasks {', '.join(state.fixer_tasks)}. Edit nothing; send every finding "
        "with its evidence to the fixer that owns it."
    )
    stage = crew.critics
    return [
        {"name": f"critic-{n}", "model": stage.model, "effort": stage.effort, "prompt": brief}
        for n in range(1, stage.count + 1)
    ]


def restore_commands(slug: str, state: State, quote: str) -> list[list[str]]:
    """Put the eng lane back, unfocus the crew and focus the earlier targets in their order."""
    keys = {"agent": "eng-agent", "model": "eng-model", "effort": "eng-effort", "effort_max": "effort-max"}
    settings = [f"{keys[key]}={value}" for key, value in state.earlier_lane.items() if key in keys]
    commands = [swarm(slug, "set", *settings)] if settings else []
    crew_tasks = [*state.scout_tasks, *state.fixer_tasks, *([state.harden_task] if state.harden_task else [])]
    reason = "troubleshooting crew ended"
    commands += [swarm(slug, "unfreeze", f"tasks/{t}", "--reason", reason, "--quote", quote) for t in crew_tasks]
    for target in state.earlier_focus:
        commands.append(swarm(slug, "focus", target, "--bottom", "--reason", reason, "--quote", quote))
    return commands


def plan(slug: str, crew: Crew, problem: str, hypotheses: Sequence[str], now: float) -> dict[str, Any]:
    """The whole crew plan in stage order, for a dry run."""
    cid = crew_id(now)
    scouts = scout_ids(cid, crew.scouts.count)
    fixers = fixer_ids(cid, crew.fixers.count)
    stages: list[dict[str, Any]] = [
        {"stage": "scout", **asdict(crew.scouts), "tasks": dict(zip(scouts, hypotheses_for(len(scouts), hypotheses)))},
        {"stage": "fix", **asdict(crew.fixers), "tasks": fixers, "after": "every scout task is done"},
        {"stage": "critics", **asdict(crew.critics), "as": "master sub agents", "then": "the fixers fix again"},
    ]
    if crew.harden:
        stages.append({"stage": "harden", "task": f"{cid}-harden", "locks": list(HARDENING)})
    return {"slug": slug, "crew": cid, "problem": problem, "stages": stages, "cron": cron(slug, crew)}


def state_path(slug: str) -> Path:
    """Where the crew state of ``slug`` lives."""
    home = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))
    return home / "swarm" / slug / STATE_NAME


def load_state(slug: str) -> State:
    """Read the saved crew state, refusing when no crew was launched."""
    path = state_path(slug)
    if not path.exists():
        sys.exit(f"no troubleshooting crew on {slug}: run the scout stage first")
    return State(**json.loads(path.read_text()))


def save_state(slug: str, state: State) -> None:
    """Write the crew state of ``slug``."""
    path = state_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2) + "\n")


def run(command: list[str]) -> str:
    """Run one agentihooks command and return its output, raising CommandFailed on a failure."""
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise CommandFailed(f"{' '.join(command[:4])} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


class CommandFailed(Exception):
    """An agentihooks command the crew ran exited with an error."""


def alive(slug: str, target: str, runner: Runner) -> bool:
    """Whether a focus target still exists on the ledger and is not done."""
    try:
        row = json.loads(runner(["agentihooks", "ledger", "--slug", slug, "get", target]) or "{}")
    except CommandFailed:
        return False
    return bool(row) and not row.get("done") and row.get("state") != "done"


def earlier(slug: str, runner: Runner) -> tuple[list[str], dict[str, str], list[dict[str, str]]]:
    """The focus targets in their order, the eng lane settings and the working agents before the crew."""
    rows = json.loads(runner(["agentihooks", "ledger", "--slug", slug, "get", "freezes"]) or "[]")
    focus = [row["target"] for row in rows if row.get("verb") == "focus"]
    status = json.loads(runner(swarm(slug, "status", "--json")))
    config = status["config"]
    eng = (config.get("lanes") or {}).get("eng") or {}
    lane = {"agent": eng.get("agent"), "model": eng.get("model"), "effort": eng.get("effort")}
    lane["effort_max"] = config.get("effort_max")
    agents = status.get("agents") or []
    working = [{"name": a["name"], "task": a["task"]} for a in agents if a.get("task") and a.get("lane") != "master"]
    return focus, {key: value for key, value in lane.items() if value}, working


def task_done(slug: str, task: str, runner: Runner) -> bool:
    """Whether ``task`` is done on the ledger."""
    row = json.loads(runner(["agentihooks", "ledger", "--slug", slug, "get", f"tasks/{task}"]))
    return bool(row.get("done")) or row.get("state") == "done"


def execute(
    commands: Sequence[list[str]], runner: Runner, dry: bool, landed: Callable[[list[str]], None] | None = None
) -> None:
    """Print each command; unless this is a dry run, run it and report it to ``landed`` once it succeeded."""
    for command in commands:
        print(json.dumps(command))
        if not dry:
            runner(command)
            if landed:
                landed(command)


def launched(args: argparse.Namespace, crew: Crew, runner: Runner) -> State | None:
    """The running crew's state, or a new state holding the focus, eng lane and working agents from before it."""
    if state_path(args.slug).exists():
        return load_state(args.slug)
    if args.dry_run:
        return None
    focus, lane, working = earlier(args.slug, runner)
    return State(crew_id(args.now), args.problem, asdict(crew), focus, lane, stopped_agents=working)


def stop_commands(slug: str, state: State) -> list[list[str]]:
    """Ask every agent working on other tasks to hand off at its next stop, so its task waits for the restore."""
    text = (
        f"The operator's troubleshooting crew {state.crew_id} stopped all other work on {slug}. At your next stop "
        f"write your Handoff v2 and run agentihooks swarm {slug} handoff with it, then stop."
    )
    return [["agentihooks", "msg", "send", agent["name"], text] for agent in state.stopped_agents]


def stage_scout(args: argparse.Namespace, crew: Crew, runner: Runner) -> dict[str, Any]:
    """Launch the crew, finish a launch that failed partway, or add scouts to the running crew.

    The earlier focus and lane are saved before any write, and each scout is recorded only once its add landed.
    """
    state = launched(args, crew, runner) or State(crew_id(args.now), args.problem, asdict(crew))
    first = not state.launched
    scouts = scout_ids(state.crew_id, crew.scouts.count, 0 if first else len(state.scout_tasks))
    hypotheses = dict(zip(scouts, hypotheses_for(len(scouts), args.hypothesis)))
    commands = lane_commands(args.slug, crew.scouts)
    commands += [
        scout_task(args.slug, args.who, t, args.problem, h) for t, h in hypotheses.items() if t not in state.scout_tasks
    ]
    commands += focus_commands(args.slug, scouts, args.quote, only=first)
    if first:
        commands.append(canon_note(args.slug, crew))
        commands += stop_commands(args.slug, state)

    def landed(command: list[str]) -> None:
        if command[6:8] == ["task", "add"]:
            state.scout_tasks = [*state.scout_tasks, command[8]]
            save_state(args.slug, state)

    if not args.dry_run:
        save_state(args.slug, state)
    execute(commands, runner, args.dry_run, landed)
    if not args.dry_run:
        state.launched = True
        save_state(args.slug, state)
    restore = f"python3 {Path(__file__).resolve()} restore {args.slug}"
    focus = state.earlier_focus
    return {
        "crew": state.crew_id,
        "scouts": scouts,
        "earlier_focus": focus,
        "cron": cron(args.slug, crew),
        "restore": restore,
    }


def stage_fix(args: argparse.Namespace, crew: Crew, runner: Runner) -> dict[str, Any]:
    """Add the fixers once every scout is done and the master names the proven causes."""
    if not args.cause:
        sys.exit("name every proven cause with --cause; a hypothesis is never a cause")
    state = load_state(args.slug)
    open_scouts = [t for t in state.scout_tasks if not task_done(args.slug, t, runner)]
    if open_scouts:
        sys.exit(f"the scouts {', '.join(open_scouts)} are not done: keep scouting until every cause is proven")
    fixers = fixer_ids(state.crew_id, crew.fixers.count)
    commands = lane_commands(args.slug, crew.fixers)
    commands += [fixer_task(args.slug, args.who, t, args.cause, [p for p in fixers if p != t]) for t in fixers]
    commands += focus_commands(args.slug, fixers, args.quote, only=False)
    execute(commands, runner, args.dry_run)
    if not args.dry_run:
        state.fixer_tasks, state.causes = fixers, list(args.cause)
        save_state(args.slug, state)
    return {"crew": state.crew_id, "fixers": fixers, "causes": list(args.cause)}


def stage_critics(args: argparse.Namespace, crew: Crew, runner: Runner) -> dict[str, Any]:
    """Print the critic sub agents for the master to launch, once the fixers exist."""
    state = load_state(args.slug)
    if not state.fixer_tasks:
        sys.exit("no fixer tasks yet: run the fix stage once every cause is proven")
    return {"crew": state.crew_id, "critics": critic_agents(crew, state)}


def stage_harden(args: argparse.Namespace, crew: Crew, runner: Runner) -> dict[str, Any]:
    """Add and focus the hardening task."""
    state = load_state(args.slug)
    if not state.crew.get("harden", True):
        sys.exit("this crew was launched with --no-harden")
    if not state.fixer_tasks:
        sys.exit("no fixer tasks yet: harden after the fixes")
    task = f"{state.crew_id}-harden"
    commands = [harden_task(args.slug, args.who, task, state.causes)]
    commands += focus_commands(args.slug, [task], args.quote, only=False)
    execute(commands, runner, args.dry_run)
    if not args.dry_run:
        state.harden_task = task
        save_state(args.slug, state)
    return {"crew": state.crew_id, "harden": task}


def stage_restore(args: argparse.Namespace, crew: Crew, runner: Runner) -> dict[str, Any]:
    """Put the eng lane and the earlier focus back, skipping targets done or gone since, then set the state aside."""
    state = load_state(args.slug)
    gone = [target for target in state.earlier_focus if not alive(args.slug, target, runner)]
    state.earlier_focus = [target for target in state.earlier_focus if target not in gone]
    execute(restore_commands(args.slug, state, args.quote), runner, args.dry_run)
    if not args.dry_run:
        path = state_path(args.slug)
        path.rename(path.with_name(f"troubleshooting-crew-{state.crew_id}.json"))
    restored = {"restored_focus": state.earlier_focus, "skipped_focus": gone, "restored_lane": state.earlier_lane}
    return {"crew": state.crew_id, **restored, "stopped_agents": state.stopped_agents}


def stage_plan(args: argparse.Namespace, crew: Crew, runner: Runner) -> dict[str, Any]:
    """The dry run plan of every stage."""
    return plan(args.slug, crew, args.problem, args.hypothesis, args.now)


STAGES = {
    "plan": stage_plan,
    "scout": stage_scout,
    "fix": stage_fix,
    "critics": stage_critics,
    "harden": stage_harden,
    "restore": stage_restore,
}


def parser() -> argparse.ArgumentParser:
    """The command line of every stage."""
    parse = argparse.ArgumentParser(prog="crew.py", description="the operator's troubleshooting crew")
    parse.add_argument("stage", choices=list(STAGES))
    parse.add_argument("slug")
    parse.add_argument("--problem", default="")
    parse.add_argument("--hypothesis", action="append", default=[])
    parse.add_argument("--cause", action="append", default=[])
    parse.add_argument("--quote", default=LAUNCH_QUOTE)
    parse.add_argument("--as", dest="who", default=os.environ.get("AGENTIHOOKS_AGENT_NAME", ""))
    parse.add_argument("--dry-run", action="store_true")
    parse.add_argument("--no-harden", action="store_true")
    for role in ROLES:
        parse.add_argument(f"--{role}", type=int)
        parse.add_argument(f"--{role}-model")
        parse.add_argument(f"--{role}-effort", choices=EFFORT_RANK)
    return parse


def crew_from(args: argparse.Namespace, saved: dict[str, Any] | None = None) -> Crew:
    """The crew layout: the running crew's saved layout or the operator's default, with the call's flags on top."""
    base = Crew() if saved is None else Crew(**{r: Stage(**saved[r]) for r in ROLES}, harden=saved["harden"])
    stages = {}
    for role in ROLES:
        stage: Stage = getattr(base, role)
        given = (getattr(args, role), getattr(args, f"{role}_model"), getattr(args, f"{role}_effort"))
        stages[role] = Stage(*(new if new is not None else old for new, old in zip(given, astuple(stage))))
    if any(stage.count < 1 for stage in stages.values()):
        sys.exit("every crew stage needs at least one agent")
    return Crew(**stages, harden=base.harden and not args.no_harden)


def main(argv: Sequence[str] | None = None, runner: Runner = run, now: float | None = None) -> int:
    """Run one crew stage and print its result as JSON."""
    args = parser().parse_args(argv)
    args.now = time.time() if now is None else now
    if args.stage in ("plan", "scout") and not args.problem:
        sys.exit("state the problem with --problem")
    if args.stage in ("scout", "fix", "harden") and not args.who and not args.dry_run:
        sys.exit("name this session with --as or AGENTIHOOKS_AGENT_NAME")
    path = state_path(args.slug)
    saved = json.loads(path.read_text())["crew"] if args.stage != "plan" and path.exists() else None
    try:
        result = STAGES[args.stage](args, crew_from(args, saved), runner)
    except CommandFailed as failed:
        sys.exit(str(failed))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
