import fcntl
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import FrameType

from hooks.proc import _process
from scripts.account_names import oauth_env
from scripts.swarm_v2 import filesystem
from scripts.swarm_v2 import supervision_processes as trees
from scripts.swarm_v2.checkpoints import runtime as checkpoint_runtime
from scripts.swarm_v2.supervision import Launch, LaunchRefused
from scripts.swarm_v2.supervision_agent import native_command
from scripts.swarm_v2.supervision_protocol import checkpoint, matches, read, write
from scripts.swarm_v2.worker import lease_renewal

PROMPT = "prompt.md"


class Supervisor:
    def __init__(self, launch: Launch):
        self.launch = launch
        self.root = launch.execution.path("runtime") / "supervision" / uuid.uuid4().hex
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
        self.failure_class = None
        self.phase = "startup"
        self.lease = lease_renewal.load(launch.attempt)
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        self.environment.update(oauth_env(self.environment))
        self.environment.update(
            filesystem.environment(launch.execution, launch.harness),
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

    def herdr(self, command: list[str], deadline: float) -> dict:
        argv = [*self.launch.herdr[:-1], *command]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, 0)
        result = subprocess.run(
            argv,
            env=self.environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=remaining,
            check=True,
        )
        return json.loads(result.stdout) if result.stdout.strip() else {}

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
                    self.herdr(["workspace", "list"], min(deadline, time.monotonic() + 0.5))
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
        roles = ("herdr", "exporter") if self.launch.exporter else ("herdr",)
        if self.launch.exporter:
            self.spawn("exporter", self.launch.exporter)
        for role in roles:
            if not self.ready(role, deadline):
                return "termination" if self.stop else f"{role}_startup_failure"
        directory = self.launch.checkout or self.launch.attempt
        agent = native_command(
            checkpoint_runtime.command(self.launch.attempt, self.launch.agent), directory, self.environment
        )
        command = [sys.executable, "-m", "scripts.swarm_v2.supervision_agent", *self.opening(), *agent]
        self.phase = "workspace_create"
        tab = self.herdr(["workspace", "create", "--cwd", str(directory), "--no-focus"], deadline)
        pane = tab["result"]["root_pane"]["pane_id"]
        self.phase = "agent_launch"
        self.herdr(["pane", "run", pane, shlex.join(command)], deadline)
        if not self.ready("agent", deadline):
            return "termination" if self.stop else "agent_startup_failure"
        if self.launch.prompt:
            self.delivered()
        write(self.root / "running.json", {**self.scope, "pane_id": pane, "status": "running"})
        return None

    def opening(self) -> list[str]:
        if not self.launch.prompt:
            return []
        path = self.root / PROMPT
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(self.launch.prompt.encode())
        return ["--prompt-file", str(path)]

    def delivered(self) -> None:
        text = self.launch.prompt.encode()
        digest = {"bytes": len(text), "sha256": hashlib.sha256(text).hexdigest()}
        print(json.dumps({"supervisor": "prompt_delivered", "incarnation": self.root.name, **digest}), flush=True)

    def running(self) -> str:
        while True:
            self.wait()
            if self.stop:
                return "termination"
            self.lease.tick()
            if self.lease.lost is not None:
                return "lease_lost"
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

    def validated(self, identifier: str, deadline: float) -> bool:
        verified = threading.Event()

        def validate():
            if checkpoint_runtime.validate_export(self.launch.attempt, identifier, self.environment):
                verified.set()

        worker = threading.Thread(target=validate, daemon=True)
        worker.start()
        worker.join(max(0, deadline - time.monotonic()))
        return verified.is_set() and time.monotonic() < deadline

    def drain(self, reason: str) -> dict:
        write(self.root / "drain.json", {**self.scope, "reason": reason, "status": "draining"})
        clean = self.quiesce()
        write(self.root / "quiesced.json", {**self.scope, "status": "quiesced" if clean else "forced"})
        identifier = None
        deadline = time.monotonic() + self.launch.budgets.checkpoint
        while clean and self.lease.lost is None and self.launch.exporter and time.monotonic() < deadline:
            identifier = checkpoint(self.launch.attempt, read(self.root / "exporter.checkpoint.json"), self.scope)
            if identifier:
                portable = (
                    self.environment.get(checkpoint_runtime.ROOT)
                    or self.environment.get(checkpoint_runtime.REMOTE) == "true"
                )
                if portable and not self.validated(identifier, deadline):
                    identifier = None
                break
            if "exporter" in self.exits:
                break
            self.wait()
        trees.cleanup(os.getpid(), self.launch.budgets.kill, self.observe)
        agent = read(self.root / "agent.json")
        if "agent" in self.exits and matches(agent, self.scope) and type(agent.get("pid")) is int:
            self.reaped.add(agent["pid"])
        result = {
            **self.scope,
            "reason": reason,
            "failure_class": self.failure_class,
            "failure_stage": self.phase if self.failure_class else None,
            "signal": self.stop,
            "checkpoint_status": "complete" if identifier else "incomplete",
            "checkpoint_id": identifier,
            "quiescence": "clean" if clean else "forced",
            "child_exits": self.exits,
            "supervisor_child_exit_total": len(self.reaped),
        }
        record = self.launch.record()
        record["checkpoint_acknowledgement"] = {
            **self.scope,
            "status": result["checkpoint_status"],
            "checkpoint_id": identifier,
            "quiescence": result["quiescence"],
            "acknowledged_at_ms": int(time.time() * 1000),
        }
        write(self.root / "launch.json", record)
        result["launch_record"] = record
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
            write(self.root / "launch.json", self.launch.record())
            trees.subreaper()
            previous = {sig: signal.signal(sig, self.signal) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGCHLD)}
            try:
                try:
                    reason = self.start() or self.running()
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
                    self.failure_class = type(exc).__name__
                    reason = "supervisor_failure"
                result = self.drain(reason)
                summary = {key: value for key, value in result.items() if key != "launch_record"}
                print(json.dumps(summary, sort_keys=True), flush=True)
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
