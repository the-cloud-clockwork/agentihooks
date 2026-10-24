"""Swarm agents as herdr panes: spawn through init-agent, find live ones by name, retire by the process recorded at
spawn."""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from scripts import agent_choice, master_claude_login
from scripts.handoff import envelope
from scripts.init_agent import PREDECESSOR
from scripts.profiles import binding, plugins
from scripts.routing import native_login
from scripts.swarm import (
    affinity,
    capacity,
    default_models,
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
    remote_control,
    timing,
)
from scripts.swarm.pane import (
    PaneObservation,
    content_refusal,
    provider_error,
    provider_error_id,
    refusal_input,
    selection_prompt,
    typed_input,
)
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig, connect
from scripts.swarm.tick import Placed, SpawnError
from scripts.swarm_v2 import front_door

SWARM_HOME = Path.home() / ".agentihooks" / "swarm"
SPAWN_TIMEOUT_S = 300
RESUME_CHECKS, RESUME_CHECK_S = 30, 2
STARTED_ROUTES = ("routed", "bare", "direct")
AUTO = "auto"
PICKED_LANES = ("eng", "ci")
PLAN_MODE = ["--permission-mode", "plan"]
REMOTE_PANE = "a remote agent's pane lives on its worker herdr server, never on the local one"
MANAGED_RESUME = "unsupported resume: a managed Codex execution continues through its supervised server"
TICK_ONLY = ("SWARM_SECRET", "AGENTIHOOKS_LEDGER_AGENT_TOKEN")
REPO_VARIABLE = "AGENTIHOOKS_SWARM_REPO"


def codex_plan_mode(environ):
    home = Path(environ.get("HOME") or Path.home())
    ledger = Path(environ.get("LEDGER_DIR") or home / "development-ledger").expanduser()
    grants = ", ".join(
        f'{json.dumps(str(root))}="write"' for root in (ledger, home / ".agentihooks" / "swarm", home / "scratchpad")
    )
    return [
        "-c",
        f'permissions.planner={{extends=":read-only", network={{enabled=true}}, filesystem={{{grants}}}}}',
        "-c",
        'default_permissions="planner"',
    ]


def _bin():
    return shutil.which("agentihooks") or str(Path(sys.argv[0]).resolve())


def parse_fields(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith(" "))


def _ledger_token(slug, name):
    from scripts.swarm_ledger.authority_context import secret as trusted_secret

    secret = trusted_secret()
    if not secret:
        return {}
    from scripts.swarm_ledger.ledger_authority import agent_token

    return {"AGENTIHOOKS_LEDGER_AGENT_TOKEN": agent_token(secret, slug, name)}


def _set(value):
    return "" if value in (None, "", AUTO) else value


def _prefer(profile, avoid):
    order = profile_choice.preferred(profile)
    if not avoid:
        return order
    return tuple(harness for harness in (order or agent_choice.AGENTS) if harness != avoid)


def _model_args(agent, chosen, environ, bounds, preserve=False):
    from scripts.init_agent import model_effort, model_flags

    model, effort = model_effort(agent, [], environ)
    saved = _set(chosen.get("effort")) or effort
    effort = effort_range.clamp(agent, saved, bounds)
    if (
        not preserve
        and chosen.get("source") == model_pick.BAND
        and chosen.get("model") == model_pick.SONNET
        and effort != model_pick.SONNET_EFFORT
    ):
        raise SpawnError("unsupported automatic Sonnet: the swarm range must allow xhigh", "unsupported")
    if preserve and effort_range.rank(effort) != effort_range.rank(effort_range.named(agent, saved)):
        raise SpawnError("unsupported transfer: saved effort is outside the current swarm range", "unsupported")
    return model_flags(agent, _set(chosen.get("model")) or model, effort)


def preserves_effort(recorded: bool, lane: str) -> bool:
    return recorded and lane != MASTER


def _lane_default(lane, agent, chosen, config=None):
    model = default_models.model(config, agent)
    if lane == MASTER:
        return model_pick.frontier(agent, model)
    return model_pick.ModelPick(_set(chosen.get("model")) or model, chosen.get("effort"))


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


def _titled(slug, lane, seat):
    from scripts.swarm_v2 import masters

    return ["--title", masters.title(slug, seat)] if lane == MASTER else []


def _launch_records(name):
    from scripts.codex_session.attach import launch_records

    return launch_records(name, dict(os.environ))


def _opened_pane(name, since_ms):
    found = sorted(
        (r for r in _launch_records(name) if r.launched_at >= since_ms), key=lambda record: record.launched_at
    )
    return found[-1].pane_id if found else ""


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


def _command(action, agent, text="", nonce=""):
    from scripts.hive.orders import CommandOrder

    order_id = f"{action}:{agent.execution_id}:{agent.generation}" + (f":{nonce}" if nonce else "")
    slug = agent.seat.rpartition("@")[2]
    return CommandOrder(order_id, action, slug, agent.name, agent.execution_id, agent.generation, text)


def _carries(pid, name):
    from scripts.swarm.naming import resolve_name
    from scripts.terminate_agent import agent_environ

    carried = agent_environ(pid, ("AGENTIHOOKS_AGENT_NAME",))[0]
    return bool(carried) and (carried == name or resolve_name(carried) == name)


def _pane_process(agent):
    from scripts.herdr_host import server_socket
    from scripts.terminate_agent import herdr_pane, sessions

    socket = server_socket(dict(os.environ))
    owned = []
    for session in sessions():
        pane, target_socket = herdr_pane(session.process.pid)
        if (
            session.name == agent.name
            and pane == agent.pane_id
            and server_socket({"HERDR_SOCKET_PATH": target_socket}) == socket
        ):
            owned.append(session.process)
    return min(owned, key=lambda process: process.start_time, default=None)


def _launch_timeouts(agent: str, passed: list[str], argv: list[str]) -> tuple[list[str], float]:
    from scripts.master_login_chain import BUDGET

    if agent != "claude" or passed[:2] != ["--route", "master-login"]:
        return argv, SPAWN_TIMEOUT_S
    routed = list(argv)
    routed[routed.index("--route-timeout") + 1] = str(BUDGET.route)
    return routed, BUDGET.spawn


class HerdrRuntime:
    quota_accounts: Callable[..., list] | None = None

    def __init__(self, home=SWARM_HOME, run=subprocess.run, choose=None, herdr=herdr_call):
        self.home, self.run, self.herdr = home, run, herdr
        self.choose = choose or agent_choice.choose
        self.sleep = time.sleep
        self._binding_pids = {}
        self._managed = {}
        self.refusals = {}
        self.hive = None
        self.end, self.reap = reaper.retire, reaper.reap
        self._quota_previous = {}
        self._host_spent = capacity.no_spawns

    def has_capacity(self, config):
        environ = dict(os.environ)
        if self.quota_accounts is not None:
            self._quota_accounts = self.quota_accounts(environ, time.time(), refresh=False)
        if hasattr(self, "_quota_accounts"):
            from scripts.swarm.capacity import free_seats

            return any(free_seats(row) for row in self._quota_open(""))
        return self.choose("", environ)[1] != agent_choice.ALL_FULL

    def _quota_warned(self):
        from scripts.swarm import quota_handoff

        thresholds = quota_handoff.Thresholds.from_env(dict(os.environ))
        return {
            (row.harness, row.name): window
            for row in self._quota_accounts
            if (window := quota_handoff.trigger(row, thresholds))
        }

    def _quota_open(self, lane):
        from scripts.swarm import capacity, quota_handoff

        rows = capacity.unreserved(self._quota_accounts) if lane == MASTER else self._quota_accounts
        thresholds = quota_handoff.Thresholds.from_env(dict(os.environ))
        return [row for row in rows if not quota_handoff.trigger(row, thresholds)]

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
        refresh: bool = True,
    ) -> dict:
        from scripts.swarm import capacity

        read = self.quota_accounts or capacity.accounts
        self._quota_accounts = read(dict(os.environ), now, refresh=refresh)
        self._quota_held = {}
        accounts = self._quota_successor_accounts(requirements) if requirements else None
        if requirements:
            requirements = self._quota_preferring(requirements)
        warned = self._quota_warned()
        previous = self._quota_previous
        inputs = capacity.ScaleInputs(
            self._quota_accounts, agents, demand, self.host, previous, warned, self._host_spent, int(now * 1000)
        )
        host = capacity.granted(capacity.host_room(config, inputs), previous, inputs.now_ms)
        config, scaled = capacity.autoscaled(config, inputs, host)
        decision = capacity.calculate(
            replace(config, lanes={"master": {}, **config.lanes}),
            self._quota_accounts,
            agents,
            demand,
            requirements,
            accounts,
            warned=warned,
        )
        decision["host"] = host
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

    def host(self) -> host_budget.HostSample | host_budget.Room | None:
        return host_budget.controller_host(os.environ)

    def quota_previous(self, decision: dict) -> None:
        self._quota_previous = decision

    def quota_spent(self, counter: Callable[[int], int]) -> None:
        self._host_spent = counter

    def quota_requirements(self, config: SwarmConfig, ready: dict) -> dict:
        from scripts.swarm.capacity import _harnesses

        self._quota_ready_ids = {lane: [task["id"] for task in tasks] for lane, tasks in ready.items()}
        self._quota_handoffs = {}
        self._quota_preferred = {}
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
                if requested:
                    options.append((requested,))
                elif plugins.claude_only(profile):
                    options.append(("claude",))
                elif (
                    saved.get("harness") in agent_choice.AGENTS
                    and (task.get("handoff_envelope") or {}).get("reason") != "quota"
                ):
                    self._quota_preferred[lane, len(options)] = agent_choice.preferring(saved["harness"])
                    options.append(agent_choice.AGENTS)
                else:
                    order = profile_choice.preferred(profile)
                    if order and (lane, len(options)) not in self._quota_handoffs:
                        self._quota_preferred[lane, len(options)] = order
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
            first = next(
                (h for h in agent_choice.preferring(predecessor[0]) if any(row.harness == h for row in eligible)), None
            )
            accounts.setdefault(lane, {})[index] = {(row.harness, row.name) for row in eligible if row.harness == first}
            if first is None:
                task = self._quota_ready_ids[lane][index]
                self._quota_held[task] = quota_handoff.refusal(predecessor, harnesses, rows, reason)
        return accounts

    def _quota_preferring(self, requirements):
        from scripts.swarm.capacity import free_seats

        room = {
            h: sum(free_seats(row) for row in self._quota_open("") if row.harness == h) for h in agent_choice.AGENTS
        }
        for choice in (choice for options in requirements.values() for choice in options if len(choice) == 1):
            room[choice[0]] -= 1
        narrowed = {lane: list(options) for lane, options in requirements.items()}
        for (lane, index), order in getattr(self, "_quota_preferred", {}).items():
            first = next((h for h in order if h in narrowed[lane][index] and room[h] > 0), None)
            if first:
                narrowed[lane][index] = (first,)
                room[first] -= 1
        return narrowed

    def _quota_eligible(self, harness, lane):
        from scripts.swarm.capacity import free_seats

        return [row for row in self._quota_open(lane) if row.harness == harness and free_seats(row)]

    def _quota_choice(self, agent, reason, fixed, lane):
        if not hasattr(self, "_quota_accounts"):
            return agent, reason
        allocation = getattr(self, "_quota_allocations", {}).get(lane)
        eligible = [
            h for h in agent_choice.AGENTS if self._quota_eligible(h, lane) and (allocation is None or allocation[h])
        ]
        if agent in eligible:
            return agent, reason
        if not fixed and eligible:
            return eligible[0], f"fallthrough: {agent} has no placeable quota seats"
        raise SpawnError(self._quota_refusal(agent, None if fixed else agent_choice.AGENTS), "unavailable")

    def _rotation(self, requested, environ, prefer=()):
        if requested or not hasattr(self, "_quota_accounts"):
            return self.choose(requested, environ)
        from scripts.swarm.capacity import offered, pick

        seats = offered(self._quota_accounts, self._quota_warned())
        seat = next(filter(None, (pick(seats, lambda s, h=h: s.harness == h) for h in prefer)), None) or pick(seats)
        return (
            (seat.harness, "rotation") if seat else (agent_choice.fallback(self._quota_accounts), agent_choice.ALL_FULL)
        )

    def _quota_transfer(self, saved, profile, environ, lane, want, planned=None):
        from scripts.swarm import capacity, quota_handoff

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

        rows = capacity.unreserved(self._quota_accounts) if lane == MASTER else self._quota_accounts
        candidates = [row for row in rows if not blocked(row)]
        preferred = [row for row in candidates if planned and row.harness == planned[0]]
        order = agent_choice.preferring(saved["harness"])
        account = (
            next((row for row in preferred if row.name == planned[1]), None)
            or quota_handoff.successor(preferred, order, thresholds)
            or quota_handoff.successor(candidates, order, thresholds)
        )
        if account is None:
            raise SpawnError(quota_handoff.refusal(predecessor, harnesses, rows, blocked), "unavailable")
        return {
            **saved,
            "harness": account.harness,
            "account": account.name,
            **(
                {"model": "", "effort": effort_range.named(account.harness, saved["effort"])}
                if account.harness != saved["harness"]
                else {}
            ),
        }

    def _saved_choice(self, saved, profile, quota_transfer, environ):
        if quota_transfer:
            return saved["harness"], "quota handoff"
        if plugins.claude_only(profile) and saved["harness"] != "claude":
            raise SpawnError(
                "unsupported handoff: required profile cannot mount on the original harness", "unsupported"
            )
        agent, reason = self.choose(saved["harness"], environ)
        if agent != saved["harness"]:
            raise SpawnError("unsupported handoff: router substituted the original harness", "unsupported")
        return agent, reason

    def _profile_decision(self, config, lane, chosen, task, saved, relaunch, environ):
        decision = profile_choice.kept(task, saved, relaunch)
        if decision is None:
            decision = timing.call(
                profile_choice.choose, config.slug, lane, chosen, task, environ, getattr(config, "overlays", {})
            )
        if not decision.bundle_revision:
            kept = _recorded_revision(saved) if saved.get("profile") == decision.profile else ""
            decision = replace(decision, bundle_revision=kept or overlays.revision())
        return decision

    def planned_account(self, task_id, agent):
        planned = getattr(self, "_quota_tasks", {}).get(task_id)
        return getattr(self, "_quota_task_accounts", {}).get(task_id) if planned == agent else None

    def _planned_slot(self, task_id):
        harness = getattr(self, "_quota_tasks", {}).get(task_id)
        return (harness, getattr(self, "_quota_task_accounts", {}).get(task_id)) if harness else None

    def _quota_account(self, agent, preferred, excluded, lane):
        from scripts.swarm.capacity import offered, pick, unreserved

        eligible = [row for row in self._quota_eligible(agent, lane) if row.name != excluded]
        row = next((row for row in eligible if row.name == preferred), None)
        if row is None:
            rows = unreserved(self._quota_accounts) if lane == MASTER else self._quota_accounts
            rows = [row for row in rows if row.harness == agent]
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
                replace(row, sessions=row.sessions + 1)
                if (row.harness, row.name) == (account.harness, account.name)
                else row
                for row in self._quota_accounts
            ]

    def _fallback_choice(self, saved, profile, quota_transfer, environ, requested, avoid):
        if saved:
            return self._saved_choice(saved, profile, quota_transfer, environ)
        return self._rotation(requested, environ, _prefer(profile, avoid))

    def _route_kind(self, lane, harness, task, slug):
        if master_claude_login.enabled(slug, task.get("seat", ""), lane, harness or "claude"):
            return "interactive"
        explicit = task.get("route_kind")
        if explicit or lane != MASTER or harness not in ("", "claude"):
            return explicit or "subscription"
        try:
            native_login.check(os.environ, "native", self.run)
        except native_login.LoginError:
            return "subscription"
        return "interactive"

    def spawn(self, config, lane, name, task):
        chosen, environ = config.lanes.get(lane, {}), dict(os.environ)
        relaunch = live_binding.complete(task.get("launch_assignment"))
        saved = relaunch or profile_choice.transfer(task)
        decision = self._profile_decision(config, lane, chosen, task, saved, relaunch, environ)
        profile = decision.profile
        requested = "claude" if plugins.claude_only(profile) else _set(chosen.get("agent"))
        want = affinity.desired(config) if lane == MASTER else _set(chosen.get("agent"))
        if want and plugins.claude_only(profile) and want != "claude":
            kind = "master affinity" if lane == MASTER else "lane harness"
            raise SpawnError(f"{kind} {want} cannot mount the claude only profile {profile}", "unsupported")
        route_kind = self._route_kind(lane, want or requested or saved.get("harness", ""), task, config.slug)
        quota_transfer = (task.get("handoff_envelope") or {}).get("reason") == "quota"
        saved = (
            self._quota_transfer(saved, profile, environ, lane, want, self._planned_slot(task["id"]))
            if saved and quota_transfer and route_kind != "interactive"
            else saved
        )
        avoid = (task.get("transfer") or {}).get("avoid_harness")
        if saved and not quota_transfer and (saved["harness"] != want if want else saved["harness"] == avoid):
            saved = {}
        if route_kind == "interactive" and lane == MASTER:
            agent, reason = want or requested or saved.get("harness") or "claude", "interactive login"
        elif want and not saved:
            agent, reason = self.choose(want, environ)
        else:
            agent, reason = self._fallback_choice(saved, profile, quota_transfer, environ, requested, avoid)
        if reason == agent_choice.ALL_FULL and not hasattr(self, "_quota_accounts") and route_kind != "interactive":
            raise SpawnError(reason, "unavailable")
        planned = getattr(self, "_quota_tasks", {}).get(task["id"])
        if planned and planned != avoid and not (requested or saved or want):
            agent, reason = planned, "fallthrough: quota reservation"
        agent, reason = (
            (agent, reason)
            if route_kind == "interactive"
            else self._quota_choice(agent, reason, bool(requested or quota_transfer or want), lane)
        )
        if saved and agent != saved["harness"]:
            saved = {key: value for key, value in saved.items() if key not in ("model", "account")}
        from scripts.swarm_v2.runtime import codex

        transport = codex.transport(config, agent)
        task = {**task, "harness": agent, **({"transport": transport} if transport else {})}
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
        argv += ["--claim-fence", json.dumps(task["claim_fence"])] if task.get("claim_fence") else []
        argv += _titled(config.slug, lane, task.get("seat", ""))
        argv += codex.arguments(config, agent, task)
        argv += _pinned(decision.overlays, decision.bundle_revision)
        if saved.get("model"):
            picked = model_pick.ModelPick(
                saved["model"],
                saved["effort"],
                source=saved.get("model_source", "handoff"),
                confidence=saved.get("model_confidence"),
            )
        elif lane in PICKED_LANES:
            picked = timing.call(
                model_pick.pick,
                agent,
                {} if quota_transfer else chosen,
                task,
                environ,
                effort_range.of(config),
                default_models.model(config, agent),
            )
        else:
            picked = _lane_default(lane, agent, {} if quota_transfer else chosen, config)
        picked = replace(picked, model=_set(picked.model) or default_models.model(config, agent))
        picked = replace(picked, effort=saved["effort"]) if saved else picked
        mode = [] if lane != "plan" else PLAN_MODE if agent == "claude" else codex_plan_mode(environ)
        front = front_door.holds(config.slug, task.get("seat", ""))
        login = master_claude_login.enabled(config.slug, task.get("seat", ""), lane, agent)
        route_kind = "interactive" if login else route_kind
        route = master_claude_login.route(login) or (
            ["--route", "interactive"]
            if route_kind == "interactive"
            else (["--route", saved["account"]] if saved.get("account") else [])
        )
        rc = remote_control.decision(lane, agent, route_kind, config.slug, login) if front else {}
        account = None
        if hasattr(self, "_quota_accounts") and route_kind != "interactive":
            account = self._quota_account(
                agent, saved.get("account") or self.planned_account(task["id"], agent), None, lane
            )
            route = ["--route", account.name]
        placed = self._account_launch(
            config,
            lane,
            task,
            name,
            [
                *argv,
                "--",
                *route,
                *remote_control.arguments(rc),
                *_model_args(
                    agent,
                    picked.__dict__,
                    environ,
                    effort_range.bounds(config, lane),
                    preserve=preserves_effort(bool(saved), lane),
                ),
                *mode,
            ],
        )
        self._reserve_account(account, lane, agent)
        placed = codex.complete(config, name, placed, self._terminate)
        placed_kind = "api" if placed.account == "api" else route_kind
        return replace(
            placed,
            model_source=picked.source,
            model_confidence=picked.confidence,
            profile_decision={**decision.record(), **placed.profile_decision},
            choice=agent_choice.choice_kind(reason),
            overlays=list(decision.overlays),
            route_kind=placed_kind,
            remote_control=remote_control.decision(lane, agent, placed_kind, config.slug, login) if front else {},
            desired_route_kind=None
            if lane != MASTER
            else "interactive"
            if front and agent == "claude" and not task.get("route_kind")
            else "",
        )

    def resume(self, config, agent, text):
        """Reopen the agent's own conversation in a new pane of the same name; SpawnError unless herdr shows it there."""
        if live_binding.managed(agent):
            raise SpawnError(MANAGED_RESUME, "unsupported")
        if not agent.profile:
            raise SpawnError("unsupported resume: original profile is missing", "unsupported")
        if agent.harness not in effort_range.EFFORTS:
            raise SpawnError(f"unsupported resume harness: {agent.harness}", "unsupported")
        from scripts.profiles import render

        if agent.harness == "codex" and not render.has_codex_rollout(
            agent.profile, agent.overlays, agent.conversation_id
        ):
            raise SpawnError(f"Codex has no saved rollout for conversation {agent.conversation_id}", "unsupported")
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
        argv += _titled(config.slug, agent.lane, agent.seat)
        defaults = _lane_default(agent.lane, agent.harness, config.lanes.get(agent.lane, {}), config)
        picked = model_pick.ModelPick(
            agent.model or defaults.model,
            agent.effort or defaults.effort,
            source=agent.model_source or ("recorded" if agent.model else defaults.source),
        )
        front = front_door.holds(config.slug, agent.seat)
        login = master_claude_login.enabled(config.slug, agent.seat, agent.lane, agent.harness)
        route = master_claude_login.route(login) or (
            ["--route", "interactive"]
            if agent.route_kind == "interactive"
            else (["--route", agent.account] if agent.account else [])
        )
        model = _model_args(
            agent.harness,
            picked.__dict__,
            dict(os.environ),
            effort_range.bounds(config, agent.lane),
            preserve=preserves_effort(bool(agent.effort), agent.lane),
        )
        kind = "interactive" if login else agent.route_kind
        rc = remote_control.decision(agent.lane, agent.harness, kind, config.slug, login) if front else {}
        argv += ["--resume", agent.conversation_id, "--", *route, *remote_control.arguments(rc), *model]
        placed = timing.call(self._launch, config, agent.lane, agent.task, agent.name, argv)
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
            conversation_id=agent.conversation_id,
            model_source=picked.source,
            profile_decision={**agent.profile_decision, **placed.profile_decision},
            overlays=agent.overlays,
            route_kind=kind,
            remote_control=rc,
            desired_route_kind=agent.desired_route_kind,
        )

    def operator(self, config, name, profile, text):
        """Open a claude session for the operator in the swarm's space, bound to the swarm with no task or lane slot."""
        argv = self._argv(config, name, "claude", text, f"{name}.md", profile)
        model = _model_args(
            "claude",
            model_pick.frontier("claude", default_models.model(config, "claude")).__dict__,
            dict(os.environ),
            effort_range.of(config),
        )
        return self._launch(config, naming.OPERATOR, "", name, [*argv, "--", *model])

    def _account_launch(self, config, lane, task, name, argv):
        if task.get("account_slot_hive"):
            from scripts.hive.account_slots import EXECUTION_ENV, HIVE_ENV

            argv = [
                "env",
                f"{HIVE_ENV}={task['account_slot_hive']}",
                f"{EXECUTION_ENV}={task['execution_id']}",
                *argv,
            ]
        return timing.call(
            self._launch,
            config,
            lane,
            task["id"],
            name,
            argv,
            predecessor=_predecessor(task),
            controller_epoch=task.get("controller_epoch"),
        )

    def _holds(self, pane_id, conversation_id):
        for _ in range(RESUME_CHECKS):
            if (self.conversations() or {}).get(pane_id) == conversation_id:
                return True
            self.sleep(RESUME_CHECK_S)
        return False

    def _argv(self, config, name, agent, text, prompt_name, profile, worn=()):
        from scripts.hive import daemon

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
            daemon.local_checkout(config.repo),
        ]
        argv += [
            "--name",
            naming.plain(name) if agent == "claude" else name,
            "--agent",
            agent,
            "--start-timeout",
            "30",
            "--route-timeout",
            "30" if agent == "codex" else "90",
        ]
        argv += ["--inbox-channel"] if agent == "claude" else []
        argv += [arg for overlay in worn for arg in ("--overlay", overlay)]
        return [*argv, "--profile", profile, "--prompt-file", str(path)]

    def _launch(self, config, lane, task_id, name, argv, predecessor=None, controller_epoch=None):
        if controller_epoch is not None:
            from scripts.swarm import lease

            lease.require_epoch(connect(), config.slug, controller_epoch)
        agent = argv[argv.index("--agent") + 1]
        proof = None
        passed = argv[argv.index("--") + 1 :] if "--" in argv else []
        argv, timeout = _launch_timeouts(agent, passed, argv)
        if agent == "claude" and passed[:2] == ["--route", "interactive"]:
            try:
                proof = native_login.check(os.environ, "native", self.run)
            except native_login.LoginError as exc:
                raise SpawnError(str(exc), "unavailable") from exc
        launched_at = int(time.time() * 1000)
        load_at_launch = list(os.getloadavg())
        try:
            proc = self.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={
                    **{key: value for key, value in os.environ.items() if key not in (PREDECESSOR, *TICK_ONLY)},
                    **({PREDECESSOR: predecessor} if predecessor else {}),
                    **_ledger_token(config.slug, name),
                    "AGENTIHOOKS_SWARM": config.slug,
                    REPO_VARIABLE: argv[argv.index("--dir") + 1],
                    "AGENTIHOOKS_SWARM_LANE": lane,
                    "AGENTIHOOKS_SWARM_TASK": task_id,
                    "AGENTIHOOKS_SWARM_AUTONOMY": config.autonomy,
                    "AGENTIHOOKS_SWARM_SPAWN": "1",
                    "AGENTIHOOKS_AGENT_NAME": name,
                    effort_range.VARIABLE: ":".join(effort_range.bounds(config, lane)),
                    **({"AGENTIHOOKS_COMPACT_LIMIT": str(config.compact_limit)} if config.compact_limit else {}),
                },
            )
        except subprocess.TimeoutExpired as exc:
            self._terminate(name)
            raise SpawnError(f"init-agent timed out for {name}", "ambiguous", _opened_pane(name, launched_at)) from exc
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
            message = tail[-1] if tail else f"init-agent exit {proc.returncode}"
            raise SpawnError(message, pane_id=fields.get("pane_id") or _opened_pane(name, launched_at))
        try:
            validated = binding.fields(fields, argv[argv.index("--profile") + 1], agent)
        except ValueError as exc:
            self._terminate(name)
            raise SpawnError(str(exc), pane_id=fields.get("pane_id", "")) from exc
        if proof is not None:
            native_login.record(self.home, config.slug, proof)
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
            conversation_id=(self.conversations() or {}).get(fields.get("pane_id"), ""),
        )

    def recover(self, name: str) -> Placed:
        found = self._get(herdr_target(name)) or {}
        return Placed(pane_id=found.get("pane_id", ""), harness=found.get("agent", ""))

    def live_names(self):
        from scripts.terminate_agent import sessions

        ordinary = {s.name for s in sessions() if s.name}
        managed = self._managed_agents()
        names = (ordinary - managed.keys()) | {
            name
            for name, agent in managed.items()
            if live_binding.server_alive(self._managed_record(agent)) is not False
        }
        return names | self.hive.names() if self.hive is not None else names

    def _managed_agents(self):
        from scripts.swarm.store import Unreachable

        try:
            store = connect()
            self._managed = {
                agent.name: agent
                for slug in store.slugs()
                for agent in store.agents(slug)
                if live_binding.managed(agent) and not self.remote(agent)
            }
        except Unreachable:
            pass
        return self._managed

    def _managed_record(self, agent):
        return live_binding.transport(connect(), agent)

    def remote(self, agent: AgentRecord) -> str:
        from scripts.hive import reports

        return reports.host(agent)

    def _hosted(self, agent: AgentRecord) -> dict:
        return (self.hive.execution(agent) or {}) if self.hive is not None else {}

    def _order(self, host: str, order, name: str) -> bool:
        from scripts.hive.orders import Orders

        if self.hive is None:
            self.refusals[name] = {"process": 0, "refusal": f"no hive reports reach this runtime for hive {host}"}
            return False
        orders = Orders(self.hive.redis)
        ack = orders.command(host, order)
        if ack is not None and ack.status == "done":
            self.refusals.pop(name, None)
            return True
        if ack is None:
            why = f"{order.action} ordered on hive {host}, waiting for its acknowledgement"
        else:
            orders.forget(host, order.id)
            why = f"hive {host} refused {order.action}: {ack.failure}"
        self.refusals[name] = {"process": 0, "refusal": why}
        return False

    def reported(self, agent: AgentRecord) -> bool:
        from scripts.terminate_agent import sessions

        if self.remote(agent):
            return self._hosted(agent).get("reported") is True
        if live_binding.managed(agent):
            return self.observe(agent).state not in {"dead", "unknown"}
        session = live_binding.bound_session(agent, sessions())
        return session is not None and session.status == "alive"

    def bindings(self, agents: list[AgentRecord]) -> dict:
        from scripts.terminate_agent import sessions

        items, facts = sessions(), {}
        self._binding_pids = {}
        for agent in agents:
            if agent.runtime_backend != "local":
                continue
            if self.remote(agent):
                found = self._hosted(agent).get("binding")
                if found is not None:
                    facts[agent.name] = {field: value for field, value in found.items() if field != "rebound"}
                continue
            if live_binding.managed(agent):
                record = self._managed_record(agent)
                alive = live_binding.server_alive(record)
                facts[agent.name] = live_binding.read(agent, record.server_pid) if alive else {"process": alive}
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
        if host := self.remote(agent):
            return self._order(host, _command("retire", agent), agent.name)
        if live_binding.managed(agent):
            return self._retire_managed(agent)
        pid = agent.profile_decision.get("validation", {}).get("pid") or self._binding_pids.get(agent.name)
        return self.retire_process(agent, pid, homes)

    def retire_pane(self, agent: AgentRecord, homes: tuple = ()) -> bool:
        found = None
        if agent.pane_id:
            try:
                result = self.herdr(["agent", "get", agent.pane_id])
                found = result.get("agent", result)
            except Exception as exc:
                if "not found" not in str(exc):
                    self.refusals[agent.name] = {"process": 0, "refusal": str(exc)}
                    return False
        if found is not None and _owns(found, agent):
            conversation = _conversation_id(found.get("agent_session"))
            if found.get("name") or agent.conversation_id and conversation == agent.conversation_id:
                launch = _pane_process(agent)
                if launch is not None:
                    return self.retire_process(agent, launch.pid, homes, launch.start_time)
                if agent.name not in self.live_names():
                    return self.retire_process(agent, None, homes)
        if found is None and agent.name not in self.live_names():
            return self.retire_process(replace(agent, pane_id=""), None, homes)
        self.refusals[agent.name] = {"process": 0, "refusal": "no owned pane for unrecorded process"}
        return False

    def _retire_managed(self, agent):
        from scripts.codex_session.binding import EXITED, BindingStore
        from scripts.codex_session.supervisor import execution_directory, remove_home

        store = connect()
        record = live_binding.transport(store, agent)
        if record is None or not record.server_pid or not record.server_start_identity:
            self.refusals[agent.name] = {"process": 0, "refusal": "managed server identity is unknown"}
            return False
        outcome = self.end(agent.name, record.server_pid, [], record.server_start_identity)
        if outcome.refusal:
            self.refusals[agent.name] = {"process": outcome.process, "refusal": outcome.refusal}
            return False
        bindings = BindingStore(store)
        slug = store.names.slug_of(agent.name)
        recorded = bindings.viewers(slug, record.execution_id)
        try:
            owned = self._owned_panes(agent, recorded)
        except Exception as exc:
            self.refusals[agent.name] = {"process": record.server_pid, "refusal": f"pane list: {exc}"}
            return False
        for pane in owned:
            launch = pane == agent.pane_id
            try:
                if launch:
                    self._close_owned_pane(agent)
                else:
                    self.close_pane(pane)
            except Exception as exc:
                refusal = f"pane {pane}: {exc}" if launch else f"viewer pane {pane}: {exc}"
                self.refusals[agent.name] = {"process": record.server_pid, "refusal": refusal}
                return False
        directory = execution_directory(record)
        if directory is not None and directory.exists():
            try:
                remove_home(directory)
            except OSError as exc:
                self.refusals[agent.name] = {"process": record.server_pid, "refusal": f"runtime home: {exc}"}
                return False
        bindings.put(slug, replace(record, state=EXITED, active_turn_id=""))
        bindings.release(slug, record)
        bindings.forget_viewers(slug, list(recorded))
        self.refusals.pop(agent.name, None)
        return True

    def _owned_panes(self, agent, recorded):
        from scripts.codex_session.attach import recorded_terminals

        known = recorded_terminals(agent.name, recorded, dict(os.environ))
        if not known:
            return []
        listed = {p["pane_id"]: p.get("terminal_id") for p in self.herdr(["pane", "list"])["panes"]}
        return [pane for pane, terminal in known.items() if listed.get(pane) == terminal]

    def retire_process(self, agent, pid, homes=(), start=0):
        if live_binding.managed(agent):
            return self._retire_managed(agent)
        if agent.runtime_backend != "local":
            self.refusals[agent.name] = {"process": 0, "refusal": REMOTE_PANE}
            return False
        outcome = self.end(agent.name, pid, list(homes), start)
        if outcome.refusal:
            self.refusals[agent.name] = {"process": outcome.process, "refusal": outcome.refusal}
            return False
        if agent.pane_id:
            try:
                self._close_owned_pane(agent)
            except Exception as exc:
                self.refusals[agent.name] = {"process": pid or 0, "refusal": f"pane {agent.pane_id}: {exc}"}
                return False
        self.refusals.pop(agent.name, None)
        return True

    def refusal(self, agent):
        return self.refusals.get(agent.name, {"process": 0, "refusal": "unknown"})

    def reap_name(self, name):
        from scripts.hive.orders import CommandOrder, Orders
        from scripts.terminate_agent import sessions

        held = [s.process.pid for s in sessions() if s.name == name]
        host = "" if held or self.hive is None else self.hive.holder(name)
        if not host:
            carrying = [pid for pid in held if _carries(pid, name)]
            return not self.reap(carrying).refusal and len(carrying) == len(held)
        order = CommandOrder(f"reap:{name}", "reap", name=name)
        reaped = self._order(host, order, name)
        if reaped:
            Orders(self.hive.redis).forget(host, order.id)
        return reaped

    def pane_tail(self, pane_id, lines):
        args = ["pane", "read", pane_id, "--source", "recent", "--lines", str(lines), "--format", "text"]
        return self.herdr(args)["text"]

    def launched_pane(self, pane_id, name):
        listed = {p["pane_id"]: p.get("terminal_id") for p in self.herdr(["pane", "list"]).get("panes", [])}
        return any(r.pane_id == pane_id and r.terminal_id == listed.get(pane_id) for r in _launch_records(name))

    def close_pane(self, pane_id):
        try:
            self.herdr(["pane", "close", pane_id])
        except Exception as exc:
            if "not found" not in str(exc):
                raise

    def _close_owned_pane(self, agent):
        """herdr reuses a closed pane's id, so a pane it lists under another owner is left open."""
        try:
            found = self.herdr(["agent", "get", agent.pane_id])
        except Exception as exc:
            if "not found" not in str(exc):
                raise
            found = None
        if found is None or _owns(found.get("agent", found), agent):
            self.close_pane(agent.pane_id)

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
        if self.remote(agent):
            if self.hive is not None and self.hive.gone(agent):
                return PaneObservation("exited")
            pane = self._hosted(agent).get("pane")
            return PaneObservation(**pane) if pane else PaneObservation("unknown")
        if live_binding.managed(agent):
            return live_binding.observe_managed(self._managed_record(agent))
        if agent.runtime_backend != "local":
            return PaneObservation("unknown")
        found = self._get(pane_target(agent))
        if found is None or not _owns(found, agent):
            return PaneObservation("unknown")
        if agent.pane_id and found.get("name") not in (agent.name, herdr_target(agent.name)):
            self.name_pane(agent)
        state = found.get("agent_status") or found.get("status") or "unknown"
        try:
            capture = self.herdr(["pane", "read", found["pane_id"], "--source", "visible", "--format", "ansi"])
        except Exception:
            return PaneObservation("unknown" if agent.lane == MASTER else state)
        text = capture.get("text")
        if not isinstance(text, str):
            return PaneObservation(state)
        error = provider_error(agent.harness, text)
        error_id = provider_error_id(agent.harness, text)
        title = selection_prompt(text)
        refusal = content_refusal(agent.harness, text)
        if state in {"idle", "blocked"} and refusal and not refusal_input(text):
            return PaneObservation("idle", provider_checked=True, content_refusal=refusal)
        if title or state == "blocked":
            return PaneObservation(
                "waiting", title, provider_error=error, provider_checked=True, provider_error_id=error_id
            )
        return PaneObservation(
            state, typed=typed_input(text), provider_error=error, provider_checked=True, provider_error_id=error_id
        )

    def _get(self, target):
        try:
            result = self.herdr(["agent", "get", target])
        except Exception:
            return None
        return result.get("agent", result)

    def name_pane(self, agent):
        """Name the agent's own pane: unnamed and holding its conversation, or carrying the 32 character cut of its name."""
        if host := self.remote(agent):
            return self._order(host, _command("name", agent), agent.name)
        found = self._get(agent.pane_id) if agent.pane_id and agent.runtime_backend == "local" else None
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

        managed = {}
        for agent in self._managed_agents().values():
            record = self._managed_record(agent)
            if record is not None:
                managed[agent.pane_id or agent.execution_id] = record.thread_id
        try:
            listed = self.herdr(["agent", "list"]).get("agents", [])
        except Exception:
            return managed or None
        registered = _registered_conversations(sessions())
        local = {
            row["pane_id"]: _conversation_id(row.get("agent_session")) or registered.get(row.get("name"), "")
            for row in listed
            if row.get("pane_id")
        }
        return {**local, **managed}

    def nudge(self, agent, text):
        from scripts.hive.orders import Orders
        from scripts.swarm.delivery import marked

        if host := self.remote(agent):
            if self.hive is None or self.hive.read(host) is None:
                return f"hive {host} is not reporting"
            Orders(self.hive.redis).command(host, _command("nudge", agent, text, uuid.uuid4().hex))
            return None
        if agent.runtime_backend != "local":
            return
        try:
            if live_binding.managed(agent):
                from scripts.inbox.store import InboxStore
                from scripts.inbox.wake import BY

                InboxStore(connect().redis).send(BY, agent.name, text, fyi=True)
                return
            self.herdr(["agent", "prompt", pane_target(agent), marked(text)])
        except Exception as exc:
            return str(exc) or type(exc).__name__
