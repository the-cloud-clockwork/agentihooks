import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace


def serve_thread(ws, messages, thread_id):
    resumed = False
    for raw in ws:
        message = json.loads(raw)
        messages.append(message)
        method = message["method"]
        if "id" not in message:
            continue
        if method == "initialize":
            result = {}
        elif method == "thread/resume":
            resumed = True
            result = {"thread": {"id": thread_id, "turns": []}}
        elif method == "thread/read":
            result = {"thread": {"id": thread_id, "turns": []}}
        elif method == "turn/start" and resumed:
            result = {"turn": {"id": "inbox-turn"}}
        else:
            ws.send(json.dumps({"id": message["id"], "error": {"message": "thread was not resumed"}}))
            continue
        ws.send(json.dumps({"id": message["id"], "result": result}))


def test_served_execution_runs_a_pump_that_resumes_and_delivers_then_retires(tmp_path, monkeypatch):
    import fakeredis
    import redis
    from websockets.sync.server import unix_serve

    from scripts.codex_session import serve, supervisor
    from scripts.codex_session.binding import BindingStore, TransportRecord
    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    slug, execution, recipient, thread_id = "pump-proof", "execution-1", "engineer@proof", "thread-bound"
    tcp = fakeredis.TcpFakeServer(("127.0.0.1", 0))
    tcp_thread = threading.Thread(target=tcp.serve_forever, daemon=True)
    tcp_thread.start()
    url = f"redis://127.0.0.1:{tcp.server_address[1]}/0"
    client = redis.Redis.from_url(url, decode_responses=True)
    swarm = RedisStore(client)
    swarm.create(SwarmConfig(slug, "agentihooks", 1, 0))
    agent = AgentRecord(recipient, "eng", "task", seat=f"eng-1@{slug}", execution_id=execution, generation=1)
    client.hset(swarm.key(slug, "executions"), execution, json.dumps(asdict(agent)))
    endpoint = str(tmp_path / "s")
    record = TransportRecord(execution, 1, agent.seat, recipient, "task", f"unix://{endpoint}", thread_id=thread_id)
    bindings = BindingStore(swarm)
    bindings.reserve(slug, record)
    inbox = InboxStore(client)
    item = inbox.send("sender", recipient, "Delivered by the separate pump")
    messages, pumps = [], []
    popen = subprocess.Popen
    child_env = dict(os.environ)
    child_env.update(
        AGENTIHOOKS_SWARM_REDIS_URL=url,
        AGENTIHOOKS_DEPLOYMENT="local",
        AGENTIHOOKS_HOME=str(tmp_path / "home"),
        PYTHONPATH=str(Path(__file__).resolve().parents[2]),
    )
    spec = SimpleNamespace(slug=slug, cwd=str(tmp_path), environ=child_env)

    def spawn(command, **kwargs):
        assert command == [sys.executable, "-m", "scripts.inbox.codex", slug, execution]
        assert kwargs == {"cwd": spec.cwd, "env": child_env}
        process = popen(command, **kwargs)
        pumps.append(process)
        return process

    class ServerProcess:
        returncode = None
        deadline = time.monotonic() + 15

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            assert len(pumps) == 1, "serve never launched the push pump"
            assert time.monotonic() < self.deadline, "push pump never delivered the item"
            deadline = time.monotonic() + (timeout or 10)
            while time.monotonic() < deadline:
                assert pumps[0].poll() is None, "push pump exited before delivery"
                if inbox.get(item.id).state == "delivered":
                    self.returncode = 0
                    return 0
                time.sleep(0.01)
            raise subprocess.TimeoutExpired("app-server", timeout)

    server_process = ServerProcess()
    started = SimpleNamespace(record=record, process=server_process)
    monkeypatch.setattr(serve, "prepare", lambda *args: spec)
    monkeypatch.setattr(serve, "connect", lambda environ: swarm)
    monkeypatch.setattr(supervisor, "launch", lambda *args: started)
    monkeypatch.setattr(supervisor, "finish", lambda *args: None)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(serve.signal, "signal", lambda *args: None)
    try:
        with unix_serve(lambda ws: serve_thread(ws, messages, thread_id), endpoint) as server:
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                assert (
                    serve.main(["serve", "--cwd", spec.cwd, "--profile", "engineer", "--seat", agent.seat], child_env)
                    == 0
                )
            finally:
                server.shutdown()
                server_thread.join(timeout=5)
        assert len(pumps) == 1
        assert pumps[0].poll() is not None
        resume = [message for message in messages if message.get("method") == "thread/resume"]
        assert [message["params"] for message in resume] == [{"threadId": thread_id}]
        [delivery] = [message for message in messages if message.get("method") == "turn/start"]
        assert delivery["params"]["threadId"] == thread_id
        assert item.text in delivery["params"]["input"][0]["text"]
        assert inbox.get(item.id).state == "delivered"
    finally:
        for process in pumps:
            supervisor.stop(process)
        client.close()
        tcp.shutdown()
        tcp.server_close()
        tcp_thread.join(timeout=5)
