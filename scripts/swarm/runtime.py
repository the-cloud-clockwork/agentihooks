"""Swarm agents as herdr panes: spawn through init-agent, find live ones by name, retire by the process recorded at
spawn."""

import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from scripts import agent_choice
from scripts.profiles import binding, plugins
from scripts.swarm import (
    affinity,
    effort_range,
    live_binding,
    model_pick,
    naming,
    priming_trace,
    profile_choice,
    prompt,
    reaper,
)
from scripts.swarm.pane import PaneObservation, selection_prompt, typed_input
from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig, codex_split
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
        raise SpawnError("unsupported transfer: saved effort is outside the current swarm range")
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


def _transfer(task):
    saved = (task.get("handoff_envelope") or {}).get("launch")
    if not task.get("handoff") and not saved:
        return {}
    if not saved or not all(saved.get(key) for key in ("profile", "harness", "model", "effort")):
        raise SpawnError("unsupported handoff: original profile and run options are missing")
    if saved["harness"] not in ("claude", "codex"):
        raise SpawnError(f"unsupported handoff harness: {saved['harness']}")
    return saved


class HerdrRuntime:
    def __init__(self, home=SWARM_HOME, run=subprocess.run, choose=None, herdr=herdr_call):
        self.home, self.run, self.herdr = home, run, herdr
        self.choose = choose or agent_choice.choose
        self.sleep = time.sleep
        self._binding_pids = {}
        self.refusals = {}
        self.end, self.reap = reaper.retire, reaper.reap

    def has_capacity(self, config):
        environ = dict(os.environ)
        share, floor = codex_split(config, environ)
        return (
            agent_choice.choose_shared("", environ, None, share, floor, choose=self.choose)[1] != agent_choice.ALL_FULL
        )

    def spawn(self, config, lane, name, task, spawns=None):
        chosen, environ = config.lanes.get(lane, {}), dict(os.environ)
        relaunch = live_binding.complete(task.get("launch_assignment"))
        saved = relaunch or _transfer(task)
        decision = (
            profile_choice.ProfileDecision(saved["profile"], "handoff", "original seat profile")
            if saved and (relaunch or not task.get("profile"))
            else profile_choice.choose(config.slug, lane, chosen, task, environ)
        )
        profile = decision.profile
        requested = "claude" if plugins.claude_only(profile) else _set(chosen.get("agent"))
        want = affinity.desired(config) if lane == MASTER else ""
        if want and plugins.claude_only(profile) and want != "claude":
            raise SpawnError(f"master affinity {want} cannot mount the claude only profile {profile}")
        if saved and want and saved["harness"] != want:
            saved = {}
        if want and not saved:
            agent, reason = self.choose(want, environ)
        elif saved:
            if plugins.claude_only(profile) and saved["harness"] != "claude":
                raise SpawnError("unsupported handoff: required profile cannot mount on the original harness")
            requested = saved["harness"]
            agent, reason = self.choose(requested, environ)
        else:
            share, floor = codex_split(config, environ)
            agent, reason = agent_choice.choose_shared(requested, environ, spawns, share, floor, choose=self.choose)
        if reason == agent_choice.ALL_FULL:
            raise SpawnError(reason)
        if saved and agent != saved["harness"]:
            raise SpawnError("unsupported handoff: router substituted the original harness")
        task = {**task, "harness": agent}
        text = prompt.build(
            config.slug, config.repo, lane, name, task, role=chosen.get("role", ""), autonomy=config.autonomy
        )
        priming_trace.write(self.home, config.slug, name, task)
        argv = self._argv(config, name, agent, text, f"{name}.md", profile)
        if saved:
            picked = model_pick.ModelPick(
                saved["model"],
                saved["effort"],
                source=saved.get("model_source", "handoff"),
                confidence=saved.get("model_confidence"),
            )
        elif lane in PICKED_LANES:
            picked = model_pick.pick(agent, chosen, task, environ)
        else:
            picked = _lane_default(lane, agent, chosen)
        mode = PLAN_MODE if (lane, agent) == ("plan", "claude") else []
        route = ["--route", saved["account"]] if saved.get("account") else []
        placed = self._launch(
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
        )
        return replace(
            placed,
            model_source=picked.source,
            model_confidence=picked.confidence,
            profile_decision={**decision.record(), **placed.profile_decision},
            choice=agent_choice.choice_kind(reason),
        )

    def resume(self, config, agent, text):
        """Reopen the agent's own conversation in a new pane of the same name; SpawnError unless herdr shows it there."""
        if not agent.profile:
            raise SpawnError("unsupported resume: original profile is missing")
        argv = self._argv(
            config,
            agent.name,
            agent.harness,
            text,
            f"{agent.name}-restored.md",
            agent.profile,
        )
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
            raise SpawnError(f"herdr never showed conversation {agent.conversation_id} on pane {placed.pane_id}")
        return replace(
            placed, model_source=picked.source, profile_decision={**agent.profile_decision, **placed.profile_decision}
        )

    def _holds(self, pane_id, conversation_id):
        for _ in range(RESUME_CHECKS):
            if (self.conversations() or {}).get(pane_id) == conversation_id:
                return True
            self.sleep(RESUME_CHECK_S)
        return False

    def _argv(self, config, name, agent, text, prompt_name, profile):
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
        return [*argv, "--profile", profile, "--prompt-file", str(path)]

    def _launch(self, config, lane, task_id, name, argv):
        agent = argv[argv.index("--agent") + 1]
        try:
            proc = self.run(
                argv,
                capture_output=True,
                text=True,
                timeout=SPAWN_TIMEOUT_S,
                env={
                    **os.environ,
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
            raise SpawnError(f"init-agent timed out for {name}") from exc
        fields = parse_fields(proc.stdout)
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
            session = live_binding.bound_session(agent, items)
            if session is not None:
                facts[agent.name] = live_binding.read(agent, session.process.pid)
                self._binding_pids[agent.name] = str(session.process.pid)
            elif agent.profile_decision.get("validation", {}).get("pid"):
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
        outcome = self.end(agent.name, pid, list(homes))
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
        """Each herdr pane's resumable conversation id, empty when herdr reports none; None when herdr cannot answer."""
        try:
            listed = self.herdr(["agent", "list"]).get("agents", [])
        except Exception:
            return None
        return {row["pane_id"]: _conversation_id(row.get("agent_session")) for row in listed if row.get("pane_id")}

    def nudge(self, agent, text):
        from scripts.swarm.delivery import marked

        try:
            self.herdr(["agent", "prompt", pane_target(agent), marked(text)])
        except Exception:
            pass
