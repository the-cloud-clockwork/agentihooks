"""Swarm agents as herdr panes: spawn through init-agent, find live ones by name, retire by the process recorded at
spawn."""

import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from scripts import agent_choice
from scripts.handoff import envelope
from scripts.init_agent import PREDECESSOR
from scripts.profiles import binding, plugins
from scripts.swarm import (
    affinity,
    effort_range,
    host_budget,
    live_binding,
    model_pick,
    naming,
    overlays,
    priming_trace,
    profile_choice,
    prompt,
    reaper,
    timing,
)
from scripts.swarm.pane import PaneObservation, selection_prompt, typed_input
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig, connect
from scripts.swarm.tick import Placed, SpawnError

SWARM_HOME = Path.home() / ".agentihooks" / "swarm"
SPAWN_TIMEOUT_S = 300
RESUME_CHECKS, RESUME_CHECK_S = 30, 2
STARTED_ROUTES = ("routed", "bare", "direct")
AUTO = "auto"
PICKED_LANES = ("eng", "ci")
PLAN_MODE = ["--permission-mode", "plan"]


def _bin():
    return shutil.which("agentihooks") or str(Path(sys.argv[0]).resolve())


def parse_fields(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith(" "))


def _set(value):
    return "" if value in (None, "", AUTO) else value


def _model_args(agent, chosen, environ, bounds, preserve=False):
    from scripts.init_agent import model_effort, model_flags

    model, effort = model_effort(agent, [], environ)
    saved = _set(chosen.get("effort")) or effort
    effort = effort_range.clamp(agent, saved, bounds)
    if preserve and effort != saved:
        raise SpawnError("unsupported transfer: saved effort is outside the current swarm range", "unsupported")
    return model_flags(agent, _set(chosen.get("model")) or model, effort)


def _lane_default(lane, agent, chosen):
    if lane == MASTER:
        return model_pick.frontier(agent)
    return model_pick.ModelPick(chosen.get("model"), chosen.get("effort"))


def herdr_call(args):
    from scripts.herdr_host import _cli, binary

    if args[:2] == ["pane", "read"]:
        done = subprocess.run([binary(), *args], capture_output=True, text=True, timeout=30, check=True)
        return {"text": done.stdout}
    return _cli(args, dict(os.environ))


def herdr_target(name):
    from scripts.herdr_host import agent_name

    return agent_name(name)


def pane_target(agent):
    return agent.pane_id or herdr_target(agent.name)


def _owns(found, agent):
    """A pane belongs to the agent when it carries the agent's herdr name or its 32 character cut, or is unnamed and
    holds no other conversation."""
    name = found.get("name")
    if name:
        return name in (agent.name, herdr_target(agent.name), agent.name[:32])
    own = _conversation_id(found.get("agent_session"))
    return not own or not agent.conversation_id or own == agent.conversation_id


def _conversation_id(session):
    if not isinstance(session, dict) or session.get("kind") != "id":
        return ""
    return session.get("value") or ""


def _registered_conversations(found):
    """Each named session's main conversation id by herdr pane name; a sub-agent id is no UUID and never counts."""
    ids = {}
    for session in found:
        try:
            uuid.UUID(session.session_id)
        except ValueError:
            continue
        if session.name:
            ids.setdefault(herdr_target(session.name), session.session_id)
    return ids


def _transfer(task):
    saved = (task.get("handoff_envelope") or {}).get("launch")
    if not task.get("handoff") and not saved:
        return {}
    if not saved or not all(saved.get(key) for key in ("profile", "harness", "model", "effort")):
        raise SpawnError("unsupported handoff: original profile and run options are missing", "unsupported")
    if saved["harness"] not in ("claude", "codex"):
        raise SpawnError(f"unsupported handoff harness: {saved['harness']}", "unsupported")
    return saved


def _recorded_revision(saved):
    return saved.get("bundle_revision") or saved.get("profile_decision", {}).get("bundle_revision")


def _pinned(worn, revision):
    if not worn:
        return []
    if not revision:
        raise SpawnError("an overlay launch needs the bundle commit it renders from, and none was recorded")
    return ["--bundle-revision", revision]


def _predecessor(task):
    conversation = (task.get("handoff_envelope") or {}).get("conversation_id")
    return conversation if task.get("transfer") and conversation != envelope.UNKNOWN else None


class HerdrRuntime:
    def __init__(self, home=SWARM_HOME, run=subprocess.run, choose=None, herdr=herdr_call):
        self.home, self.run, self.herdr = home, run, herdr
        self.choose = choose or agent_choice.choose
        self.sleep = time.sleep
        self._binding_pids = {}
        self.refusals = {}
        self.end, self.reap = reaper.retire, reaper.reap
        self._quota_previous = {}

    def has_capacity(self, config):
        environ = dict(os.environ)
        if hasattr(self, "_quota_accounts"):
            from scripts.swarm.capacity import free_seats

            return any(free_seats(row) for row in self._quota_open())
        return self.choose("", environ)[1] != agent_choice.ALL_FULL

    def _quota_warned(self):
        from scripts.swarm import quota_handoff

        thresholds = quota_handoff.Thresholds.from_env(dict(os.environ))
        return {
            (row.harness, row.name): window
            for row in self._quota_accounts
            if (window := quota_handoff.trigger(row, thresholds))
        }

    def _quota_open(self):
        warned = self._quota_warned()
        return [row for row in self._quota_accounts if (row.harness, row.name) not in warned]

    def _quota_refusal(self, agent, harnesses=None):
        from scripts.swarm.capacity import warning

        harnesses = harnesses or (agent,)
        warnings = "; ".join(
            f"{harness} {name} {warning(window)}"
            for (harness, name), window in self._quota_warned().items()
            if harness in harnesses
        )
        return f"no {' or '.join(harnesses)} account has placeable quota seats" + (f": {warnings}" if warnings else "")

    def quota_capacity(
        self,
        config: SwarmConfig,
        agents: list[AgentRecord],
        now: float,
        demand: dict | None = None,
        requirements: dict | None = None,
    ) -> dict:
        from scripts.swarm import capacity

        placing = demand is None or any(demand.values())
        self._quota_accounts = capacity.accounts(dict(os.environ), now, refresh=placing)
        self._quota_held = {}
        accounts = self._quota_successor_accounts(requirements) if requirements else None
        warned = self._quota_warned()
        inputs = capacity.ScaleInputs(self._quota_accounts, agents, demand, self.host, self._quota_previous, warned)
        config, scaled = capacity.autoscaled(config, inputs)
        decision = capacity.calculate(
            config, self._quota_accounts, agents, demand, requirements, accounts, warned=warned
        )
        if scaled:
            decision["autoscale"] = scaled
        for task, reason in self._quota_held.items():
            decision["reason"] += f"; quota handoff {task} waits: {reason}"
        self._quota_allocations = decision["allocation"]
        if hasattr(self, "_quota_ready_ids"):
            slots = {
                self._quota_ready_ids[lane][slot["index"]]: slot
                for lane, placed in decision["placements"].items()
                for slot in placed
            }
            self._quota_tasks = {task: slot["harness"] for task, slot in slots.items()}
            self._quota_task_accounts = {task: slot["account"] for task, slot in slots.items()}
            decision["tasks"] = dict(self._quota_tasks)
        return decision

    def host(self) -> host_budget.HostSample:
        return host_budget.read_host()

    def quota_previous(self, decision: dict) -> None:
        self._quota_previous = decision

    def quota_requirements(self, config: SwarmConfig, ready: dict) -> dict:
        from scripts.swarm.capacity import _harnesses

        self._quota_ready_ids = {lane: [task["id"] for task in tasks] for lane, tasks in ready.items()}
        self._quota_handoffs = {}
        requirements = {}
        for lane, tasks in ready.items():
            options = []
            for task in tasks:
                chosen = config.lanes.get(lane, {})
                saved = task.get("launch_assignment") or (task.get("handoff_envelope") or {}).get("launch") or {}
                if (task.get("handoff_envelope") or {}).get("reason") == "quota" and saved.get("harness"):
                    self._quota_handoffs[lane, len(options)] = (saved["harness"], saved.get("account"))
                profile = (
                    saved.get("profile")
                    or task.get("profile")
                    or chosen.get("profile")
                    or profile_choice.DEFAULT_PROFILES[lane]
                )
                requested = _set(chosen.get("agent"))
                if plugins.claude_only(profile):
                    options.append(("claude",))
                elif requested:
                    options.append((requested,))
                elif (
                    saved.get("harness") in agent_choice.AGENTS
                    and (task.get("handoff_envelope") or {}).get("reason") != "quota"
                ):
                    options.append((saved["harness"],))
                else:
                    options.append(_harnesses(config, lane))
            requirements[lane] = options
        return requirements

    def _quota_successor_accounts(self, requirements):
        from scripts.swarm import quota_handoff

        thresholds = quota_handoff.Thresholds.from_env(dict(os.environ))
        accounts = {}
        for (lane, index), predecessor in getattr(self, "_quota_handoffs", {}).items():
            harnesses = requirements[lane][index]
            rows = [row for row in self._quota_accounts if row.harness in harnesses]

            def reason(row):
                return quota_handoff.exclusion(row, thresholds, predecessor)

            eligible = [row for row in rows if not reason(row)]
            first = next((h for h in ("claude", "codex") if any(row.harness == h for row in eligible)), None)
            accounts.setdefault(lane, {})[index] = {(row.harness, row.name) for row in eligible if row.harness == first}
            if first is None:
                task = self._quota_ready_ids[lane][index]
                self._quota_held[task] = quota_handoff.refusal(predecessor, harnesses, rows, reason)
        return accounts

    def _quota_eligible(self, harness):
        from scripts.swarm.capacity import free_seats

        return [row for row in self._quota_open() if row.harness == harness and free_seats(row)]

    def _quota_choice(self, agent, reason, fixed, lane):
        if not hasattr(self, "_quota_accounts"):
            return agent, reason
        allocation = getattr(self, "_quota_allocations", {}).get(lane)
        eligible = [h for h in agent_choice.AGENTS if self._quota_eligible(h) and (allocation is None or allocation[h])]
        if agent in eligible:
            return agent, reason
        if not fixed and eligible:
            return eligible[0], f"fallthrough: {agent} has no placeable quota seats"
        raise SpawnError(self._quota_refusal(agent, None if fixed else agent_choice.AGENTS), "unavailable")

    def _rotation(self, requested, environ):
        if requested or not hasattr(self, "_quota_accounts"):
            return self.choose(requested, environ)
        from scripts.swarm.capacity import offered, pick

        seat = pick(offered(self._quota_accounts, self._quota_warned()))
        return (seat.harness, "rotation") if seat else ("claude", agent_choice.ALL_FULL)

    def _quota_transfer(self, saved, profile, environ, lane, want, planned=None):
        from scripts.swarm import quota_handoff

        thresholds = quota_handoff.Thresholds.from_env(environ)
        allocation = getattr(self, "_quota_allocations", {}).get(lane)
        predecessor = (saved["harness"], saved.get("account"))
        harnesses = (want,) if want else ("claude", "codex") if not plugins.claude_only(profile) else ("claude",)

        def blocked(row):
            if row.harness not in harnesses:
                return f"is not a {harnesses[0]} account"
            if allocation is not None and not allocation[row.harness]:
                return f"has no seat in the {lane} allocation"
            return quota_handoff.exclusion(row, thresholds, predecessor)

        candidates = [row for row in self._quota_accounts if not blocked(row)]
        preferred = [row for row in candidates if planned and row.harness == planned[0]]
        account = (
            next((row for row in preferred if row.name == planned[1]), None)
            or quota_handoff.successor(preferred, True, thresholds)
            or quota_handoff.successor(candidates, "codex" in harnesses, thresholds)
        )
        if account is None:
            raise SpawnError(
                quota_handoff.refusal(predecessor, harnesses, self._quota_accounts, blocked), "unavailable"
            )
        return {
            **saved,
            "harness": account.harness,
            "account": account.name,
            **({"model": ""} if account.harness != saved["harness"] else {}),
        }

    def _saved_choice(self, saved, profile, quota_transfer, environ):
        if quota_transfer:
            return saved["harness"], "quota handoff"
        if plugins.claude_only(profile) and saved["harness"] != "claude":
            raise SpawnError(
                "unsupported handoff: required profile cannot mount on the original harness", "unsupported"
            )
        return self.choose(saved["harness"], environ)

    def _profile_decision(self, config, lane, chosen, task, saved, relaunch, environ):
        decision = (
            profile_choice.ProfileDecision(
                saved["profile"],
                "handoff",
                "original seat profile",
                overlays=tuple(saved.get("overlays", ())),
            )
            if saved and (relaunch or not task.get("profile"))
            else timing.call(
                profile_choice.choose, config.slug, lane, chosen, task, environ, getattr(config, "overlays", {})
            )
        )
        if not decision.bundle_revision:
            kept = _recorded_revision(saved) if saved.get("profile") == decision.profile else ""
            decision = replace(decision, bundle_revision=kept or overlays.revision())
        return decision

    def _planned_account(self, task_id, agent):
        planned = getattr(self, "_quota_tasks", {}).get(task_id)
        return getattr(self, "_quota_task_accounts", {}).get(task_id) if planned == agent else None

    def _planned_slot(self, task_id):
        harness = getattr(self, "_quota_tasks", {}).get(task_id)
        return (harness, getattr(self, "_quota_task_accounts", {}).get(task_id)) if harness else None

    def _quota_account(self, agent, preferred, excluded):
        from scripts.swarm.capacity import offered, pick

        eligible = [row for row in self._quota_eligible(agent) if row.name != excluded]
        row = next((row for row in eligible if row.name == preferred), None)
        if row is None:
            rows = [row for row in self._quota_accounts if row.harness == agent]
            seat = pick(offered(rows, {(row.harness, row.name) for row in rows if row not in eligible}))
            if seat is None:
                raise SpawnError(self._quota_refusal(agent), "unavailable")
            row = next(row for row in eligible if row.name == seat.account)
        return row

    def _reserve_account(self, account, lane, agent):
        if account is not None:
            if lane in getattr(self, "_quota_allocations", {}):
                self._quota_allocations[lane][agent] -= 1
            self._quota_accounts = [
                replace(row, sessions=row.sessions + 1) if row == account else row for row in self._quota_accounts
            ]

    def spawn(self, config, lane, name, task):
        chosen, environ = config.lanes.get(lane, {}), dict(os.environ)
        relaunch = live_binding.complete(task.get("launch_assignment"))
        saved = relaunch or _transfer(task)
        decision = self._profile_decision(config, lane, chosen, task, saved, relaunch, environ)
        profile = decision.profile
        requested = "claude" if plugins.claude_only(profile) else _set(chosen.get("agent"))
        want = affinity.desired(config) if lane == MASTER else _set(chosen.get("agent"))
        if want and plugins.claude_only(profile) and want != "claude":
            kind = "master affinity" if lane == MASTER else "lane harness"
            raise SpawnError(f"{kind} {want} cannot mount the claude only profile {profile}", "unsupported")
        quota_transfer = (task.get("handoff_envelope") or {}).get("reason") == "quota"
        saved = (
            self._quota_transfer(saved, profile, environ, lane, want, self._planned_slot(task["id"]))
            if saved and quota_transfer
            else saved
        )
        if saved and want and saved["harness"] != want and not quota_transfer:
            saved = {}
        if want and not saved:
            agent, reason = self.choose(want, environ)
        elif saved:
            requested = saved["harness"]
            agent, reason = self._saved_choice(saved, profile, quota_transfer, environ)
        else:
            agent, reason = self._rotation(requested, environ)
        if reason == agent_choice.ALL_FULL and not hasattr(self, "_quota_accounts"):
            raise SpawnError(reason, "unavailable")
        planned = getattr(self, "_quota_tasks", {}).get(task["id"])
        if planned and not (requested or saved or want):
            agent, reason = planned, "fallthrough: quota reservation"
        agent, reason = self._quota_choice(agent, reason, bool(requested or saved or want), lane)
        if saved and agent != saved["harness"]:
            raise SpawnError("unsupported handoff: router substituted the original harness", "unsupported")
        task = {**task, "harness": agent}
        text = timing.call(
            prompt.build,
            config.slug,
            config.repo,
            lane,
            name,
            task,
            role=chosen.get("role", ""),
            autonomy=config.autonomy,
        )
        timing.call(priming_trace.write, self.home, config.slug, name, task)
        argv = self._argv(config, name, agent, text, f"{name}.md", profile, decision.overlays)
        argv += _pinned(decision.overlays, decision.bundle_revision)
        if saved.get("model"):
            picked = model_pick.ModelPick(
                saved["model"],
                saved["effort"],
                source=saved.get("model_source", "handoff"),
                confidence=saved.get("model_confidence"),
            )
        elif lane in PICKED_LANES:
            picked = timing.call(model_pick.pick, agent, {} if quota_transfer else chosen, task, environ)
        else:
            picked = _lane_default(lane, agent, {} if quota_transfer else chosen)
        mode = PLAN_MODE if (lane, agent) == ("plan", "claude") else []
        route = ["--route", saved["account"]] if saved.get("account") else []
        account = None
        if hasattr(self, "_quota_accounts"):
            account = self._quota_account(agent, saved.get("account") or self._planned_account(task["id"], agent), None)
            route = ["--route", account.name]
        placed = timing.call(
            self._launch,
            config,
            lane,
            task["id"],
            name,
            [
                *argv,
                "--",
                *route,
                *_model_args(agent, picked.__dict__, environ, effort_range.of(config), preserve=bool(saved)),
                *mode,
            ],
            predecessor=_predecessor(task),
            controller_epoch=task.get("controller_epoch"),
        )
        self._reserve_account(account, lane, agent)
        return replace(
            placed,
            model_source=picked.source,
            model_confidence=picked.confidence,
            profile_decision={**decision.record(), **placed.profile_decision},
            choice=agent_choice.choice_kind(reason),
            overlays=list(decision.overlays),
        )

    def resume(self, config, agent, text):
        """Reopen the agent's own conversation in a new pane of the same name; SpawnError unless herdr shows it there."""
        if not agent.profile:
            raise SpawnError("unsupported resume: original profile is missing", "unsupported")
        argv = self._argv(
            config,
            agent.name,
            agent.harness,
            text,
            f"{agent.name}-restored.md",
            agent.profile,
            agent.overlays,
        )
        argv += _pinned(agent.overlays, agent.profile_decision.get("bundle_revision"))
        defaults = _lane_default(agent.lane, agent.harness, config.lanes.get(agent.lane, {}))
        picked = model_pick.ModelPick(
            agent.model or defaults.model,
            agent.effort or defaults.effort,
            source=agent.model_source or ("recorded" if agent.model else defaults.source),
        )
        route = ["--route", agent.account] if agent.account else []
        model = _model_args(
            agent.harness, picked.__dict__, dict(os.environ), effort_range.of(config), preserve=bool(agent.effort)
        )
        argv += ["--resume", agent.conversation_id, "--", *route, *model]
        placed = self._launch(config, agent.lane, agent.task, agent.name, argv)
        if not self._holds(placed.pane_id, agent.conversation_id):
            self.retire(
                replace(agent, pane_id=placed.pane_id, profile_decision=placed.profile_decision),
                homes=reaper.scratch_homes(config.slug, agent.task),
            )
            raise SpawnError(
                f"herdr never showed conversation {agent.conversation_id} on pane {placed.pane_id}", "ambiguous"
            )
        return replace(
            placed,
            model_source=picked.source,
            profile_decision={**agent.profile_decision, **placed.profile_decision},
            overlays=agent.overlays,
        )

    def operator(self, config, name, profile, text):
        """Open a claude session for the operator in the swarm's space, bound to the swarm with no task or lane slot."""
        argv = self._argv(config, name, "claude", text, f"{name}.md", profile)
        model = _model_args("claude", model_pick.frontier("claude").__dict__, dict(os.environ), effort_range.of(config))
        return self._launch(config, naming.OPERATOR, "", name, [*argv, "--", *model])

    def _holds(self, pane_id, conversation_id):
        for _ in range(RESUME_CHECKS):
            if (self.conversations() or {}).get(pane_id) == conversation_id:
                return True
            self.sleep(RESUME_CHECK_S)
        return False

    def _argv(self, config, name, agent, text, prompt_name, profile, worn=()):
        path = self.home / config.slug / "prompts" / prompt_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        argv = [
            _bin(),
            "init-agent",
            "--host",
            "herdr",
            "--workspace",
            naming.space(config.repo, config.code, config.slug),
            "--dir",
            config.repo,
        ]
        argv += ["--name", name, "--agent", agent, "--start-timeout", "30", "--route-timeout", "90"]
        argv += ["--inbox-channel"] if agent == "claude" else []
        argv += [arg for overlay in worn for arg in ("--overlay", overlay)]
        return [*argv, "--profile", profile, "--prompt-file", str(path)]

    def _launch(self, config, lane, task_id, name, argv, predecessor=None, controller_epoch=None):
        if controller_epoch is not None:
            from scripts.swarm import lease

            lease.require_epoch(connect(), config.slug, controller_epoch)
        agent = argv[argv.index("--agent") + 1]
        launched_at = int(time.time() * 1000)
        load_at_launch = list(os.getloadavg())
        try:
            proc = self.run(
                argv,
                capture_output=True,
                text=True,
                timeout=SPAWN_TIMEOUT_S,
                env={
                    **{key: value for key, value in os.environ.items() if key != PREDECESSOR},
                    **({PREDECESSOR: predecessor} if predecessor else {}),
                    "AGENTIHOOKS_SWARM": config.slug,
                    "AGENTIHOOKS_SWARM_LANE": lane,
                    "AGENTIHOOKS_SWARM_TASK": task_id,
                    "AGENTIHOOKS_SWARM_AUTONOMY": config.autonomy,
                    "AGENTIHOOKS_SWARM_SPAWN": "1",
                    effort_range.VARIABLE: ":".join(effort_range.of(config)),
                    **({"AGENTIHOOKS_COMPACT_LIMIT": str(config.compact_limit)} if config.compact_limit else {}),
                },
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate(name)
            raise SpawnError(f"init-agent timed out for {name}", "ambiguous") from exc
        timings = {
            "launched_at": launched_at,
            "returned_at": int(time.time() * 1000),
            "load_at_launch": load_at_launch,
            "load_at_return": list(os.getloadavg()),
        }
        fields = parse_fields(proc.stdout)
        for step in ("launcher_at", "harness_at"):
            if step in fields and fields[step].isdigit():
                timings[step] = int(fields[step])
        if proc.returncode or fields.get("status") != "started" or fields.get("route_status") not in STARTED_ROUTES:
            self._terminate(name)
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            raise SpawnError(tail[-1] if tail else f"init-agent exit {proc.returncode}")
        try:
            validated = binding.fields(fields, argv[argv.index("--profile") + 1], agent)
        except ValueError as exc:
            self._terminate(name)
            raise SpawnError(str(exc)) from exc
        return Placed(
            fields.get("pane_id", ""),
            fields.get("agent", agent),
            fields.get("account", ""),
            fields.get("model", ""),
            fields.get("effort", ""),
            fields.get("placement", ""),
            validated["profile"],
            profile_decision={"validation": validated},
            launched_at=launched_at,
            launch_timings=timings,
        )

    def recover(self, name: str) -> Placed:
        found = self._get(herdr_target(name)) or {}
        return Placed(pane_id=found.get("pane_id", ""), harness=found.get("agent", ""))

    def live_names(self):
        from scripts.terminate_agent import sessions

        return {s.name for s in sessions() if s.name}

    def reported(self, agent: AgentRecord) -> bool:
        from scripts.terminate_agent import sessions

        session = live_binding.bound_session(agent, sessions())
        return session is not None and session.status == "alive"

    def bindings(self, agents: list[AgentRecord]) -> dict:
        from scripts.terminate_agent import sessions

        items, facts = sessions(), {}
        self._binding_pids = {}
        for agent in agents:
            if agent.runtime_backend != "local":
                continue
            session = live_binding.bound_session(agent, items)
            validated = agent.profile_decision.get("validation", {}).get("pid")
            if session is not None:
                facts[agent.name] = live_binding.read(agent, session.process.pid)
                self._binding_pids[agent.name] = str(session.process.pid)
                if validated and validated != session.process.pid and facts[agent.name].get("process") is not False:
                    facts[agent.name]["rebound"] = session.process.pid
            elif validated:
                facts[agent.name] = {"process": False}
                self._binding_pids[agent.name] = None
        return facts

    def _terminate(self, name):
        selector = self._binding_pids.get(name, name)
        if selector is None:
            return True
        try:
            proc = self.run(
                [_bin(), "terminate-agent", selector, "--force-shared"], capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired:
            return False
        return proc.returncode == 0

    def retire(self, agent, homes=()):
        """End the launch process recorded at spawn with its group and every process from the task's scratch homes,
        then close the pane; the agent's name alone never selects a process."""
        pid = agent.profile_decision.get("validation", {}).get("pid") or self._binding_pids.get(agent.name)
        return self.retire_process(agent, pid, homes)

    def retire_process(self, agent, pid, homes=(), start=0):
        outcome = self.end(agent.name, pid, list(homes), start)
        if outcome.refusal:
            self.refusals[agent.name] = {"process": outcome.process, "refusal": outcome.refusal}
            return False
        if agent.pane_id:
            try:
                self.herdr(["pane", "close", agent.pane_id])
            except Exception as exc:
                if "not found" not in str(exc):
                    self.refusals[agent.name] = {"process": pid or 0, "refusal": f"pane {agent.pane_id}: {exc}"}
                    return False
        self.refusals.pop(agent.name, None)
        return True

    def refusal(self, agent):
        return self.refusals.get(agent.name, {"process": 0, "refusal": "unknown"})

    def reap_name(self, name):
        from scripts.terminate_agent import sessions

        pids = [s.process.pid for s in sessions() if s.name == name]
        return not self.reap(pids).refusal

    def close_space(self, config: SwarmConfig) -> bool:
        try:
            spaces = self.herdr(["workspace", "list"])["workspaces"]
            agents = self.herdr(["agent", "list"])["agents"]
            closed, labels = False, (naming.space(config.repo, config.code, config.slug), f"swarm-{config.slug}")
            for space in spaces:
                if space.get("label") not in labels:
                    continue
                workspace = space["workspace_id"]
                if any(
                    a.get("workspace_id") == workspace or a.get("pane_id", "").startswith(workspace + ":")
                    for a in agents
                ):
                    return False
                self.herdr(["workspace", "close", workspace])
                closed = True
            return closed
        except Exception:
            return False

    def status(self, agent):
        return self.observe(agent).state

    def observe(self, agent: AgentRecord) -> PaneObservation:
        found = self._get(pane_target(agent))
        if found is None or not _owns(found, agent):
            return PaneObservation("unknown")
        if agent.pane_id and found.get("name") not in (agent.name, herdr_target(agent.name)):
            self.name_pane(agent)
        state = found.get("agent_status") or found.get("status") or "unknown"
        try:
            capture = self.herdr(["pane", "read", found["pane_id"], "--source", "visible", "--format", "ansi"])
        except Exception:
            return PaneObservation(state)
        text = capture.get("text", "")
        title = selection_prompt(text)
        if title or state == "blocked":
            return PaneObservation("waiting", title)
        return PaneObservation(state, typed=typed_input(text))

    def _get(self, target):
        try:
            result = self.herdr(["agent", "get", target])
        except Exception:
            return None
        return result.get("agent", result)

    def name_pane(self, agent):
        """Name the agent's own pane: unnamed and holding its conversation, or carrying the 32 character cut of its name."""
        found = self._get(agent.pane_id) if agent.pane_id else None
        if found is None:
            return False
        name = found.get("name")
        own = _conversation_id(found.get("agent_session"))
        if name == herdr_target(agent.name):
            return False
        if name != agent.name[:32] and (name or not own or own != agent.conversation_id):
            return False
        try:
            self.herdr(["agent", "rename", agent.pane_id, herdr_target(agent.name)])
        except Exception:
            return False
        return True

    def conversations(self):
        """Each herdr pane's resumable conversation id, else its named session's registered main id, else empty;
        None when herdr cannot answer."""
        from scripts.terminate_agent import sessions

        try:
            listed = self.herdr(["agent", "list"]).get("agents", [])
        except Exception:
            return None
        registered = _registered_conversations(sessions())
        return {
            row["pane_id"]: _conversation_id(row.get("agent_session")) or registered.get(row.get("name"), "")
            for row in listed
            if row.get("pane_id")
        }

    def nudge(self, agent, text):
        from scripts.swarm.delivery import marked

        try:
            self.herdr(["agent", "prompt", pane_target(agent), marked(text)])
        except Exception:
            pass
