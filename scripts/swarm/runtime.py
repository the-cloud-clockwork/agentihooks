"""Swarm agents as herdr panes: spawn through init-agent, find live ones by name, close through terminate-agent."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts import agent_choice
from scripts.swarm import prompt
from scripts.swarm.tick import Placed, SpawnError

SWARM_HOME = Path.home() / ".agentihooks" / "swarm"
SPAWN_TIMEOUT_S = 300
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


class HerdrRuntime:
    def __init__(self, home=SWARM_HOME, run=subprocess.run, choose=None, herdr=herdr_call):
        self.home, self.run, self.herdr = home, run, herdr
        self.choose = choose or agent_choice.choose

    def has_capacity(self):
        return self.choose("", dict(os.environ))[1] != agent_choice.ALL_FULL

    def spawn(self, config, lane, name, task):
        chosen = config.lanes.get(lane, {})
        agent, reason = self.choose(_set(chosen.get("agent")), dict(os.environ))
        if reason == agent_choice.ALL_FULL:
            raise SpawnError(reason)
        path = self.home / config.slug / "prompts" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = prompt.build(config.slug, config.repo, lane, name, task, role=chosen.get("role", ""))
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        argv = [_bin(), "init-agent", "--host", "herdr", "--workspace", f"swarm-{config.slug}", "--dir", config.repo]
        argv += ["--name", name, "--agent", agent, "--start-timeout", "30", "--route-timeout", "90"]
        try:
            proc = self.run(
                [*argv, "--prompt-file", str(path), *_model_args(agent, chosen)],
                capture_output=True,
                text=True,
                timeout=SPAWN_TIMEOUT_S,
                env={
                    **os.environ,
                    "AGENTIHOOKS_SWARM": config.slug,
                    "AGENTIHOOKS_SWARM_LANE": lane,
                    "AGENTIHOOKS_SWARM_TASK": task["id"],
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
        try:
            result = self.herdr(["agent", "get", herdr_target(agent.name)])
        except Exception:
            return "unknown"
        found = result.get("agent", result)
        return found.get("agent_status") or found.get("status") or "unknown"

    def nudge(self, agent, text):
        try:
            self.herdr(["agent", "prompt", herdr_target(agent.name), text])
        except Exception:
            pass
