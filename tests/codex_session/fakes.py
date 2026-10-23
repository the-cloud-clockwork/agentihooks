"""A scripted Codex app-server: the methods the supervisor calls, with one attribute per fault."""

import os
import subprocess
from pathlib import Path

from scripts.codex_session import protocol, supervisor

OWN_START = 11
SERVER_START = 77
FIRST_PID = 4_190_001


def hooks(source, trust="trusted", events=supervisor.REQUIRED_HOOKS):
    return [
        {"eventName": event, "trustStatus": trust, "key": f"k:{event}", "currentHash": "h", "sourcePath": source}
        for event in events
    ]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Process:
    def __init__(self, pid, ignores_terminate=False):
        self.pid = pid
        self.returncode = None
        self.stopped = False
        self.killed = False
        self.ignores_terminate = ignores_terminate
        self.waits = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.stopped = True
        if not self.ignores_terminate:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("codex", timeout)
        return self.returncode


class Connection:
    def __init__(self, server):
        self.server = server
        self.closed = False
        self.listener = protocol.ignore
        self.events = []

    def poll(self, timeout):
        if timeout <= 0 or not self.events:
            return False
        self.listener(self.events.pop(0))
        return True

    def request(self, method, params=None):
        self.server.requests.append((method, params))
        if method in self.server.refuse:
            raise protocol.Refused(f"{method} refused: scripted")
        if method in self.server.lose:
            raise protocol.ProtocolError(f"{method} lost the connection: scripted")
        if method == "hooks/list":
            return {"data": [{"hooks": self.server.hooks}]}
        if method == "config/batchWrite":
            self.server.hooks = [{**hook, "trustStatus": self.server.trust_to} for hook in self.server.hooks]
            return {}
        if method == "account/read":
            return {"account": {"type": self.server.account}}
        if method == "thread/start":
            return {"thread": self.server.thread}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "sessionId": "session-resumed"}}
        if method == "thread/name/set":
            return {}
        if method == "turn/start":
            self.events = self.server.turn_events.pop(0) if self.server.turn_events else []
            return {"turn": {"id": "turn-opening"}}
        raise AssertionError(f"unexpected method {method}")

    def close(self):
        self.closed = True


class Server:
    def __init__(self, codex_home):
        Path(codex_home).mkdir(parents=True, exist_ok=True)
        self.codex_home = codex_home
        self.runtime_home = codex_home
        self.info = {"codexHome": codex_home}
        self.source = f"{codex_home}/hooks.json"
        self.hooks = hooks(self.source)
        self.trust_to = "trusted"
        self.account = "chatgpt"
        self.thread = {"id": "thread-new", "sessionId": "session-new"}
        self.refuse = set()
        self.lose = set()
        self.turn_events = []
        self.answers = True
        self.exits = False
        self.server_start = SERVER_START
        self.requests = []
        self.processes = []
        self.commands = []
        self.spawns = []
        self.starts = {os.getpid(): OWN_START}
        self.connects = []
        self.connections = []

    def popen(self, command, **kwargs):
        home = kwargs["env"]["CODEX_HOME"]
        if self.info.get("codexHome") == self.runtime_home:
            self.info = {"codexHome": home}
        self.hooks = [
            {**hook, "sourcePath": str(Path(home) / "hooks.json")}
            if hook.get("sourcePath") == str(Path(self.runtime_home) / "hooks.json")
            else hook
            for hook in self.hooks
        ]
        self.runtime_home = home
        self.commands.append(command)
        self.spawns.append(kwargs)
        Path(command[command.index("--listen") + 1].removeprefix("unix://")).touch()
        process = Process(FIRST_PID + len(self.processes))
        process.returncode = 1 if self.exits else None
        self.processes.append(process)
        self.starts[process.pid] = self.server_start
        return process

    def start_identity(self, pid):
        return self.starts.get(pid)

    def connect(self, path, timeout):
        self.connects.append((path, timeout))
        if not self.answers:
            raise TimeoutError("no reply to initialize")
        connection = Connection(self)
        self.connections.append(connection)
        return connection, self.info

    def methods(self):
        return [method for method, _ in self.requests]

    def params(self, method):
        return [params for name, params in self.requests if name == method]
