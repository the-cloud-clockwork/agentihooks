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
SPAWN_TIMEOUT_S = 240


def _bin():
    return shutil.which("agentihooks") or str(Path(sys.argv[0]).resolve())


def parse_fields(text):
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line and not line.startswith(" "))


class HerdrRuntime:
    def __init__(self, home=SWARM_HOME, run=subprocess.run, choose=None):
        self.home, self.run, self.choose = home, run, choose or agent_choice.choose

    def spawn(self, config, lane, name, task):
        agent, reason = self.choose("", dict(os.environ))
        if reason == agent_choice.ALL_FULL:
            raise SpawnError(reason)
        path = self.home / config.slug / "prompts" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt.build(config.slug, config.repo, lane, name, task), encoding="utf-8")
        path.chmod(0o600)
        argv = [_bin(), "init-agent", "--workspace", f"swarm-{config.slug}", "--dir", config.repo, "--name", name]
        argv += ["--agent", agent]
        try:
            proc = self.run(
                [*argv, "--prompt-file", str(path)], capture_output=True, text=True, timeout=SPAWN_TIMEOUT_S
            )
        except subprocess.TimeoutExpired as exc:
            raise SpawnError(f"init-agent timed out for {name}") from exc
        fields = parse_fields(proc.stdout)
        if proc.returncode or fields.get("status") != "started":
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            raise SpawnError(tail[-1] if tail else f"init-agent exit {proc.returncode}")
        return Placed(fields.get("pane_id", ""), fields.get("agent", "claude"), fields.get("account", ""))

    def live_names(self):
        from scripts.terminate_agent import sessions

        return {s.name for s in sessions() if s.name}

    def terminate(self, name):
        self.run([_bin(), "terminate-agent", name], capture_output=True, text=True, timeout=60)
