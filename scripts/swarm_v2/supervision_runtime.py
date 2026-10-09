import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import FrameType

from hooks.proc import _process
from scripts.swarm_v2 import supervision_processes as trees
from scripts.swarm_v2.supervision import Launch, LaunchRefused
from scripts.swarm_v2.supervision_agent import native_command
from scripts.swarm_v2.supervision_protocol import checkpoint, matches, read, write


class Supervisor:
    def __init__(self, launch: Launch):
        self.launch = launch
        self.root = launch.attempt / "run" / "supervision" / uuid.uuid4().hex
        self.scope = {
            "authority": launch.authority,
            "incarnation": self.root.name,
            "supervisor_pid": os.getpid(),
            "process_namespace": os.readlink("/proc/self/ns/pid"),
        }
        self.children = {}
        self.exits = {}
        self.reaped = set()
        self.stop = None
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        self.environment.update(
            HOME=str(launch.home),
            CODEX_HOME=str(launch.home / ".codex"),
            CLAUDE_CONFIG_DIR=str(launch.home / ".claude"),
            XDG_RUNTIME_DIR=str(launch.attempt / "tmp"),
            HERDR_CONFIG_PATH=str(self.root / "herdr.toml"),
            SWARM_SUPERVISION_DIR=str(self.root),
        )

    def signal(self, signum: int, _frame: FrameType | None) -> None:
        if signum in (signal.SIGTERM, signal.SIGINT):
            self.stop = signum

    def wait(self) -> None:
        time.sleep(0.05)
        self.observe()

    def observe(self) -> None:
        for pid, code in trees.reap():
            self.reaped.add(pid)
            for role, child in self.children.items():
                if child.pid == pid:
                    child.returncode = code
                    self.exits.setdefault(role, code)
        value = read(self.root / "agent.exit.json")
        if matches(value, self.scope) and type(value.get("exit_code")) is int:
            self.exits.setdefault("agent", value["exit_code"])
        agent = read(self.root / "agent.json")
        if matches(agent, self.scope) and type(agent.get("pid")) is int:
            found = _process(agent["pid"], Path("/proc"))
            if found is None or found.start_time != agent.get("pid_start") or found.state == "Z":
                self.exits.setdefault("agent", "unknown")

    def spawn(self, role: str, command: tuple[str, ...]) -> None:
        self.children[role] = subprocess.Popen(
            command,
            env=self.environment,
            cwd=self.launch.attempt,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def herdr(self, command: list[str]) -> dict:
        result = subprocess.run(
            [*self.launch.herdr[:-1], *command],
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=min(0.5, self.launch.budgets.startup),
            check=True,
        )
        return json.loads(result.stdout)

    def ready(self, role: str, deadline: float) -> bool:
        while time.monotonic() < deadline and not self.stop:
            self.observe()
            receipt = read(self.root / f"{role}.json")
            if role == "agent" and matches(receipt, self.scope) and receipt.get("status") == "ready":
                return True
            if self.exits:
                return False
            if role == "herdr":
                try:
                    self.herdr(["workspace", "list"])
                    return True
                except (subprocess.SubprocessError, ValueError):
                    pass
            elif (
                matches(read(self.root / f"{role}.json"), self.scope)
                and read(self.root / f"{role}.json").get("status") == "ready"
            ):
                return True
            self.wait()
        return False

    def start(self) -> str | None:
        deadline = time.monotonic() + self.launch.budgets.startup
        self.spawn("herdr", self.launch.herdr)
        self.spawn("exporter", self.launch.exporter)
        for role in ("herdr", "exporter"):
            if not self.ready(role, deadline):
                return "termination" if self.stop else f"{role}_startup_failure"
        agent = native_command(self.launch.agent, self.launch.attempt, self.environment)
        command = [sys.executable, "-m", "scripts.swarm_v2.supervision_agent", *agent]
        tab = self.herdr(["workspace", "create", "--cwd", str(self.launch.attempt), "--no-focus"])
        pane = tab["result"]["root_pane"]["pane_id"]
        self.herdr(["pane", "run", pane, shlex.join(command)])
        if not self.ready("agent", deadline):
            return "termination" if self.stop else "agent_startup_failure"
        write(self.root / "running.json", {**self.scope, "pane_id": pane, "status": "running"})
        return None

    def running(self) -> str:
        while True:
            self.wait()
            if self.stop:
                return "termination"
            for role in ("herdr", "exporter", "agent"):
                if role in self.exits:
                    return "agent_completed" if role == "agent" and self.exits[role] == 0 else f"{role}_failure"

    def quiesce(self) -> bool:
        exporter = self.children.get("exporter")
        pid = exporter.pid if exporter else None
        remaining = trees.living(os.getpid(), pid)
        trees.send(remaining, signal.SIGTERM)
        deadline = time.monotonic() + self.launch.budgets.quiesce
        while remaining and time.monotonic() < deadline:
            self.wait()
            remaining = trees.living(os.getpid(), pid)
        if remaining:
            trees.send(remaining, signal.SIGKILL)
            kill_deadline = time.monotonic() + self.launch.budgets.kill
            while trees.living(os.getpid(), pid) and time.monotonic() < kill_deadline:
                self.wait()
            return False
        return True

    def drain(self, reason: str) -> dict:
        write(self.root / "drain.json", {**self.scope, "reason": reason, "status": "draining"})
        clean = self.quiesce()
        write(self.root / "quiesced.json", {**self.scope, "status": "quiesced" if clean else "forced"})
        identifier = None
        deadline = time.monotonic() + self.launch.budgets.checkpoint
        while clean and time.monotonic() < deadline:
            identifier = checkpoint(self.launch.attempt, read(self.root / "exporter.checkpoint.json"), self.scope)
            if identifier or "exporter" in self.exits:
                break
            self.wait()
        trees.cleanup(os.getpid(), self.launch.budgets.kill, self.observe)
        agent = read(self.root / "agent.json")
        if "agent" in self.exits and matches(agent, self.scope) and type(agent.get("pid")) is int:
            self.reaped.add(agent["pid"])
        result = {
            **self.scope,
            "reason": reason,
            "signal": self.stop,
            "checkpoint_status": "complete" if identifier else "incomplete",
            "checkpoint_id": identifier,
            "quiescence": "clean" if clean else "forced",
            "child_exits": self.exits,
            "supervisor_child_exit_total": len(self.reaped),
        }
        write(self.root / "result.json", result)
        return result

    def run(self) -> int:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        with (self.root.parent / "owner.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LaunchRefused("execution already supervised") from exc
            self.root.mkdir()
            write(self.root / "context.json", self.scope)
            trees.subreaper()
            previous = {sig: signal.signal(sig, self.signal) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGCHLD)}
            try:
                try:
                    reason = self.start() or self.running()
                except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                    reason = "supervisor_failure"
                result = self.drain(reason)
                print(json.dumps(result, sort_keys=True), flush=True)
                if reason not in ("agent_completed", "termination"):
                    return 70
                return 0 if result["checkpoint_status"] == "complete" else 75
            finally:
                trees.cleanup(os.getpid(), self.launch.budgets.kill, self.observe)
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("ERROR supervisor requires attempt directory and launch specification", file=sys.stderr)
        return 64
    try:
        return Supervisor(Launch.load(Path(args[0]), Path(args[1]))).run()
    except (OSError, ValueError, KeyError, TypeError):
        print("ERROR supervisor launch refused", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
