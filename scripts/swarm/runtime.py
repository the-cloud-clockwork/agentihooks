"""Swarm agents as herdr panes: spawn through init-agent, find live ones by name, close through terminate-agent."""

import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from scripts import agent_choice
from scripts.swarm import prompt
from scripts.swarm.store import codex_split
from scripts.swarm.tick import Placed, SpawnError

SWARM_HOME = Path.home() / ".agentihooks" / "swarm"
SPAWN_TIMEOUT_S = 300
RESUME_CHECKS, RESUME_CHECK_S = 30, 2
STARTED_ROUTES = ("routed", "bare", "direct")
AUTO = "auto"


def _bin():
    return shutil.which("agentihooks") or str(Path(sys.argv[0]).resolve())


def parse_fields(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith(" "))


def _set(value):
    return "" if value in (None, "", AUTO) else value


def _model_args(agent, chosen):
    from scripts.init_agent import model_flags

    flags = model_flags(agent, _set(chosen.get("model")), _set(chosen.get("effort")))
    return ["--", *flags] if flags else []


def herdr_call(args):
    from scripts.herdr_host import _cli

    return _cli(args, dict(os.environ))


def herdr_target(name):
    from scripts.herdr_host import agent_name

    return agent_name(name)


def _conversation_id(session):
    if not isinstance(session, dict) or session.get("kind") != "id":
        return ""
    return session.get("value") or ""


class HerdrRuntime:
    def __init__(self, home=SWARM_HOME, run=subprocess.run, choose=None, herdr=herdr_call):
        self.home, self.run, self.herdr = home, run, herdr
        self.choose = choose or agent_choice.choose
        self.sleep = time.sleep

    def has_capacity(self):
        return self.choose("", dict(os.environ))[1] != agent_choice.ALL_FULL

    def spawn(self, config, lane, name, task, spawns=None):
        chosen, environ = config.lanes.get(lane, {}), dict(os.environ)
        if spawns is None:
            agent, reason = self.choose(_set(chosen.get("agent")), environ)
        else:
            share, floor = codex_split(config, environ)
            agent, reason = agent_choice.choose_shared(
                _set(chosen.get("agent")), environ, spawns, share, floor, choose=self.choose
            )
        if reason == agent_choice.ALL_FULL:
            raise SpawnError(reason)
        text = prompt.build(
            config.slug, config.repo, lane, name, task, role=chosen.get("role", ""), autonomy=config.autonomy
        )
        argv = self._argv(config, name, agent, text, f"{name}.md")
        return self._launch(config, lane, task["id"], name, [*argv, *_model_args(agent, chosen)])

    def resume(self, config, agent, text):
        """Reopen the agent's own conversation in a new pane of the same name; SpawnError unless herdr shows it there."""
        from scripts.init_agent import model_flags

        argv = self._argv(config, agent.name, agent.harness, text, f"{agent.name}-restored.md")
        flags = (["--route", agent.account] if agent.account else []) + model_flags(
            agent.harness, agent.model, agent.effort
        )
        argv += ["--resume", agent.conversation_id, *(["--", *flags] if flags else [])]
        placed = self._launch(config, agent.lane, agent.task, agent.name, argv)
        if not self._holds(placed.pane_id, agent.conversation_id):
            self.retire(replace(agent, pane_id=placed.pane_id), True)
            raise SpawnError(f"herdr never showed conversation {agent.conversation_id} on pane {placed.pane_id}")
        return placed

    def _holds(self, pane_id, conversation_id):
        for _ in range(RESUME_CHECKS):
            if (self.conversations() or {}).get(pane_id) == conversation_id:
                return True
            self.sleep(RESUME_CHECK_S)
        return False

    def _argv(self, config, name, agent, text, prompt_name):
        path = self.home / config.slug / "prompts" / prompt_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        argv = [_bin(), "init-agent", "--host", "herdr", "--workspace", f"swarm-{config.slug}", "--dir", config.repo]
        argv += ["--name", name, "--agent", agent, "--start-timeout", "30", "--route-timeout", "90"]
        return [*argv, "--prompt-file", str(path)]

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
        return Placed(
            fields.get("pane_id", ""),
            fields.get("agent", agent),
            fields.get("account", ""),
            fields.get("model", ""),
            fields.get("effort", ""),
        )

    def live_names(self):
        from scripts.terminate_agent import sessions

        return {s.name for s in sessions() if s.name}

    def _terminate(self, name):
        try:
            proc = self.run(
                [_bin(), "terminate-agent", name, "--force-shared"], capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired:
            return False
        return proc.returncode == 0

    def retire(self, agent, live):
        if live and not self._terminate(agent.name):
            return False
        if agent.pane_id:
            try:
                self.herdr(["pane", "close", agent.pane_id])
            except Exception:
                pass
        return True

    def status(self, agent):
        found = self._get(herdr_target(agent.name))
        if found is None and self.name_pane(agent):
            found = self._get(herdr_target(agent.name))
        if found is None:
            return "unknown"
        return found.get("agent_status") or found.get("status") or "unknown"

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
        try:
            self.herdr(["agent", "prompt", herdr_target(agent.name), text])
        except Exception:
            pass
