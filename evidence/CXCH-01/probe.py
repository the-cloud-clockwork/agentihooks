"""Live qualification probe for the Codex app-server over a Unix socket (CXCH-01).

Usage: python probe.py <socket> <repo> <scenario> <out.json>
Each scenario writes one sanitized record: methods, ids replaced by stable labels,
agent text kept only when it is a fixture echo.
"""

import json
import queue
import sys
import threading
import time
from pathlib import Path

from websockets.sync.client import unix_connect

TURN_TIMEOUT_S = 240


class Client:
    def __init__(self, socket_path: str, name: str):
        self.name = name
        self.ws = unix_connect(socket_path, uri="ws://localhost/", max_size=None)
        self.next_id = 1
        self.responses: dict[int, dict] = {}
        self.events: queue.Queue = queue.Queue()
        self.log: list[dict] = []
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.request("initialize", {"clientInfo": {"name": name, "version": "0"}})
        self.notify("initialized")

    def _read(self) -> None:
        try:
            for raw in self.ws:
                msg = json.loads(raw)
                stamp = time.monotonic()
                if "id" in msg and "method" not in msg:
                    with self.lock:
                        self.responses[msg["id"]] = msg
                else:
                    self.log.append({"t": stamp, "method": msg.get("method"), "server_request": "id" in msg})
                    self.events.put(msg)
        except Exception as exc:
            self.events.put({"method": "_closed", "error": type(exc).__name__})

    def notify(self, method: str, params: dict | None = None) -> None:
        self.ws.send(json.dumps({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})}))

    def send(self, method: str, params: dict) -> int:
        rid = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
        return rid

    def wait_response(self, rid: int, timeout: float = 60) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if rid in self.responses:
                    return self.responses.pop(rid)
            time.sleep(0.02)
        raise TimeoutError(f"{self.name}: no response to {rid}")

    def request(self, method: str, params: dict, timeout: float = 60) -> dict:
        return self.wait_response(self.send(method, params), timeout)

    def answer(self, rid, result: dict) -> None:
        self.ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}))

    def until(self, pred, timeout: float = TURN_TIMEOUT_S) -> tuple[dict | None, list[dict]]:
        seen = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                msg = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            seen.append(msg)
            if pred(msg):
                return msg, seen
        return None, seen

    def close(self) -> None:
        self.ws.close()


def text(s: str) -> list[dict]:
    return [{"type": "text", "text": s}]


def method_is(name: str):
    return lambda m: m.get("method") == name


def agent_texts(seen: list[dict]) -> list[str]:
    out = []
    for m in seen:
        if m.get("method") == "item/completed":
            item = m["params"].get("item", {})
            if item.get("type") == "agentMessage":
                out.append(item.get("text", ""))
    return out


def start_thread(c: Client, repo: str, **extra) -> str:
    params = {"cwd": repo, "approvalPolicy": "never", "sandbox": "read-only", **extra}
    r = c.request("thread/start", params)
    return r["result"]["thread"]["id"]


def run_turn(c: Client, thread: str, prompt: str, **extra) -> dict:
    r = c.request("turn/start", {"threadId": thread, "input": text(prompt), **extra})
    if "error" in r:
        return {"start_error": r["error"]}
    turn = r["result"]["turn"]["id"]
    done, seen = c.until(lambda m: m.get("method") == "turn/completed" and m["params"]["turn"]["id"] == turn)
    return {
        "turn": turn,
        "completed": done is not None,
        "status": done["params"]["turn"].get("status") if done else None,
        "agent_text": agent_texts(seen),
        "methods": sorted({m.get("method") for m in seen}),
    }


def conversation(sock: str, repo: str) -> dict:
    c = Client(sock, "probe-conversation")
    thread = start_thread(c, repo)
    first = run_turn(c, thread, "Fixture one. Reply with exactly the word ALPHA and nothing else.")
    idle = run_turn(c, thread, "Fixture two. The thread is idle now. Reply with exactly the word BRAVO.")
    hist = c.request("thread/read", {"threadId": thread, "includeTurns": True})["result"]["thread"]
    turns = hist.get("turns", [])
    user_texts = [
        part.get("text")
        for t in turns
        for it in t.get("items", [])
        if it.get("type") == "userMessage"
        for part in it.get("content", [])
        if part.get("type") == "text"
    ]
    c.close()
    return {
        "thread": thread,
        "first_turn": first,
        "idle_turn_start": idle,
        "thread_read": {
            "turn_count": len(turns),
            "turn_ids_match": [t.get("id") for t in turns] == [first["turn"], idle["turn"]],
            "user_texts": user_texts,
        },
    }


SLOW_PROMPT = (
    "Fixture three. Run the shell command `sleep 25` exactly once, wait for it to finish, "
    "then reply with the word CHARLIE. Follow any later instruction in this turn too."
)


def steer(sock: str, repo: str) -> dict:
    c = Client(sock, "probe-steer")
    thread = start_thread(c, repo)
    r = c.request("turn/start", {"threadId": thread, "input": text(SLOW_PROMPT)})
    turn = r["result"]["turn"]["id"]
    started, _ = c.until(
        lambda m: m.get("method") == "item/started" and m["params"]["item"].get("type") == "commandExecution", 120
    )
    stale = c.request("turn/steer", {"threadId": thread, "expectedTurnId": "stale-turn-id", "input": text("x")})
    good = c.request(
        "turn/steer",
        {
            "threadId": thread,
            "expectedTurnId": turn,
            "input": text("Fixture four, steering. Also include the word DELTA in your final reply."),
        },
    )
    busy_start = c.request("turn/start", {"threadId": thread, "input": text("Fixture five. Reply ECHO.")})
    done, seen = c.until(lambda m: m.get("method") == "turn/completed" and m["params"]["turn"]["id"] == turn)
    after = c.request(
        "turn/steer", {"threadId": thread, "expectedTurnId": turn, "input": text("Fixture six after completion.")}
    )
    rest, seen2 = c.until(method_is("turn/completed"), 90)
    cmd_status = [
        m["params"]["item"].get("status")
        for m in seen
        if m.get("method") == "item/completed" and m["params"]["item"].get("type") == "commandExecution"
    ]
    hist = c.request("thread/read", {"threadId": thread, "includeTurns": True})["result"]["thread"]
    c.close()
    return {
        "thread": thread,
        "busy_turn": turn,
        "command_started_before_steer": started is not None,
        "steer_stale_expected_turn": stale.get("error") or stale.get("result"),
        "steer_matching_expected_turn": good.get("error") or good.get("result"),
        "turn_start_while_busy": busy_start.get("error") or busy_start.get("result"),
        "busy_turn_status": done["params"]["turn"].get("status") if done else None,
        "command_item_status": cmd_status,
        "agent_text": agent_texts(seen),
        "steer_after_completion": after.get("error") or after.get("result"),
        "later_turn_completed": rest["params"]["turn"].get("status") if rest else None,
        "later_agent_text": agent_texts(seen2),
        "turns_in_history": len(hist.get("turns", [])),
    }


APPROVAL_PROMPT = (
    "Fixture seven. Run the shell command `touch approval-probe.txt` in the current folder, then reply FOXTROT."
)


def approvals(sock: str, repo: str) -> dict:
    a = Client(sock, "probe-owner-a")
    thread = start_thread(a, repo, approvalPolicy="untrusted")
    b = Client(sock, "probe-viewer-b")
    early = b.request("thread/resume", {"threadId": thread})
    a.request("turn/start", {"threadId": thread, "input": text(APPROVAL_PROMPT)})
    a.until(method_is("turn/started"), 30)
    resumed = b.request("thread/resume", {"threadId": thread})
    is_req = lambda m: "id" in m and str(m.get("method", "")).endswith("requestApproval")  # noqa: E731
    req_a, _ = a.until(is_req, 120)
    req_b, _ = b.until(is_req, 10)
    owner, other = (a, b) if req_a else (b, a)
    req = req_a or req_b
    result = {
        "thread": thread,
        "viewer_resume_before_first_turn": early.get("error") or "ok",
        "viewer_resume": resumed.get("error") or "ok",
        "request_method": req.get("method") if req else None,
        "delivered_to_a": req_a is not None,
        "delivered_to_b": req_b is not None,
        "same_request_id": bool(req_a and req_b and req_a["id"] == req_b["id"]),
    }
    if req:
        owner.answer(req["id"], {"decision": "decline"})
        resolved_other, _ = other.until(method_is("serverRequest/resolved"), 20)
        if req_a and req_b:
            other.answer(req_b["id"], {"decision": "accept"})
        done, seen = owner.until(method_is("turn/completed"), 180)
        result.update(
            {
                "answered_by": owner.name,
                "resolved_seen_by_other": resolved_other is not None,
                "turn_status": done["params"]["turn"].get("status") if done else None,
                "file_created": (Path(repo) / "approval-probe.txt").exists(),
                "agent_text": agent_texts(seen),
            }
        )
    a.close()
    b.close()
    return result


def detach(sock: str, repo: str) -> dict:
    a = Client(sock, "probe-bridge")
    thread = start_thread(a, repo)
    r = a.request("turn/start", {"threadId": thread, "input": text(SLOW_PROMPT.replace("CHARLIE", "GOLF"))})
    turn = r["result"]["turn"]["id"]
    a.until(method_is("turn/started"), 30)
    v = Client(sock, "probe-viewer")
    v.request("thread/resume", {"threadId": thread})
    v_started, _ = v.until(method_is("item/started"), 60)
    v.close()
    done, seen = a.until(lambda m: m.get("method") == "turn/completed" and m["params"]["turn"]["id"] == turn)
    v2 = Client(sock, "probe-viewer-reopen")
    resumed = v2.request("thread/resume", {"threadId": thread})
    loaded = v2.request("thread/loaded/list", {})
    second = run_turn(v2, thread, "Fixture eight from the reopened viewer. Reply HOTEL.")
    a_saw, _ = a.until(
        lambda m: m.get("method") == "turn/completed" and m["params"]["turn"]["id"] == second.get("turn"), 30
    )
    hist = v2.request("thread/read", {"threadId": thread, "includeTurns": True})["result"]["thread"]
    a.close()
    v2.close()
    return {
        "thread": thread,
        "viewer_saw_live_item": v_started is not None,
        "turn_after_viewer_closed": done["params"]["turn"].get("status") if done else None,
        "agent_text": agent_texts(seen),
        "reopen_resume_same_thread": resumed.get("result", {}).get("thread", {}).get("id") == thread,
        "loaded_threads": loaded.get("result"),
        "reopened_viewer_turn": second,
        "bridge_saw_viewer_turn": a_saw is not None,
        "turns_in_history": len(hist.get("turns", [])),
    }


def compaction(sock: str, repo: str) -> dict:
    c = Client(sock, "probe-compact")
    thread = start_thread(c, repo)
    first = run_turn(c, thread, "Fixture nine. Reply INDIA.")
    r = c.request("thread/compact/start", {"threadId": thread})
    compacted, seen = c.until(method_is("thread/compacted"), 180)
    after = run_turn(c, thread, "Fixture ten after compaction. Reply JULIET.")
    c.close()
    return {
        "thread": thread,
        "first": first,
        "compact_response": r.get("error") or r.get("result"),
        "compacted_notification": compacted is not None,
        "methods": sorted({m.get("method") for m in seen}),
        "after": after,
    }


SANDBOX_CMD = [
    "bash",
    "-c",
    "touch repo-write-probe 2>/dev/null && echo write=yes || echo write=no; "
    "curl -s -o /dev/null -m 5 -w 'ledger_http=%{http_code}\\n' http://127.0.0.1:8765/ || echo ledger_http=fail; "
    "(exec 3<>/dev/tcp/127.0.0.1/6379 && printf 'PING\\r\\n' >&3 && head -c 7 <&3) 2>/dev/null | tr -d '\\r\\n' "
    "| sed 's/^/redis=/' ; echo; "
    "curl -s -o /dev/null -m 5 -w 'external_https=%{http_code}\\n' https://pypi.org/simple/ || echo external_https=fail",
]

SANDBOX_POLICIES = {
    "readOnly": {"type": "readOnly"},
    "readOnly+network": {"type": "readOnly", "networkAccess": True},
    "workspaceWrite(repo)": {"type": "workspaceWrite"},
    "workspaceWrite(repo)+network": {"type": "workspaceWrite", "networkAccess": True},
    "workspaceWrite(scratch only)+network": {"type": "workspaceWrite", "networkAccess": True, "writableRoots": []},
    "workspaceWrite(writableRoots=repo)": {"type": "workspaceWrite", "writableRoots": ["REPO"]},
    "dangerFullAccess": {"type": "dangerFullAccess"},
}


def sandbox(sock: str, repo: str) -> dict:
    c = Client(sock, "probe-sandbox")
    out = {}
    for name, policy in SANDBOX_POLICIES.items():
        cwd = repo if "scratch only" not in name else str(Path(repo).parent / "outside")
        Path(cwd).mkdir(exist_ok=True)
        policy = {**policy, **({"writableRoots": [repo]} if policy.get("writableRoots") == ["REPO"] else {})}
        r = c.request("command/exec", {"command": SANDBOX_CMD, "cwd": repo, "sandboxPolicy": policy}, 60)
        res = r.get("result") or {}
        out[name] = {
            "error": r.get("error"),
            "exit": res.get("exitCode"),
            "stdout": res.get("stdout", "").strip().splitlines(),
            "repo_file_written": (Path(repo) / "repo-write-probe").exists(),
        }
        (Path(repo) / "repo-write-probe").unlink(missing_ok=True)
    c.close()
    return out


def account(sock: str, repo: str) -> dict:
    c = Client(sock, "probe-account")
    r = c.request("account/read", {})
    acct = (r.get("result") or {}).get("account") or {}
    thread = start_thread(c, repo)
    turn = run_turn(c, thread, "Fixture eleven. Reply KILO.")
    c.close()
    return {
        "account_read_error": r.get("error"),
        "account_type": acct.get("type"),
        "plan_present": bool(acct.get("planType")),
        "requires_openai_auth": (r.get("result") or {}).get("requiresOpenaiAuth"),
        "turn": turn,
    }


def bridge_turn(sock: str, repo: str, thread: str) -> dict:
    c = Client(sock, "probe-bridge-tui")
    c.request("thread/resume", {"threadId": thread})
    turn = run_turn(c, thread, "Fixture twelve from the bridge while the terminal is attached. Reply LIMA.")
    c.close()
    return turn


def new_thread(sock: str, repo: str) -> dict:
    c = Client(sock, "probe-tui-seed")
    thread = start_thread(c, repo)
    turn = run_turn(c, thread, "Fixture thirteen seeds the terminal thread. Reply MIKE.")
    c.close()
    return {"thread": thread, "turn": turn}


def list_hooks(c: Client, repo: str) -> list[dict]:
    entries = c.request("hooks/list", {"cwds": [repo]})["result"]["data"]
    return [h for e in entries for h in e["hooks"]]


def trust_hooks(sock: str, repo: str) -> dict:
    c = Client(sock, "probe-hook-trust")
    before = list_hooks(c, repo)
    edits = [
        {
            "keyPath": f"hooks.state.{json.dumps(h['key'])}.trusted_hash",
            "value": h["currentHash"],
            "mergeStrategy": "replace",
        }
        for h in before
        if h["trustStatus"] != "trusted"
    ]
    written = c.request("config/batchWrite", {"edits": edits, "reloadUserConfig": True}) if edits else {}
    after = list_hooks(c, repo)
    c.close()
    return {
        "before": {h["eventName"]: h["trustStatus"] for h in before},
        "write_error": written.get("error"),
        "after": {h["eventName"]: h["trustStatus"] for h in after},
    }


SCENARIOS = {
    "conversation": conversation,
    "steer": steer,
    "approvals": approvals,
    "detach": detach,
    "compaction": compaction,
    "sandbox": sandbox,
    "account": account,
    "new_thread": new_thread,
    "trust_hooks": trust_hooks,
}


def main(argv: list[str]) -> int:
    sock, repo, scenario, out = argv[1:5]
    if scenario == "bridge_turn":
        record = bridge_turn(sock, repo, argv[5])
    else:
        record = SCENARIOS[scenario](sock, repo)
    Path(out).write_text(json.dumps(record, indent=2, default=str) + "\n")
    print(json.dumps(record, default=str)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
