import argparse
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import fakeredis

VERSION = re.compile(r'"page_version": "[0-9a-f]*"')
SLUG = "replay"


def exchange(server, token, body=None, route=f"/api/{SLUG}", method=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}{route}",
        data=data,
        headers={
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Content-Type": "application/json",
            "X-Ledger-Token": token,
        },
        method=method or ("GET" if body is None else "PUT"),
    )
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = VERSION.sub('"page_version": ""', response.read().decode())
        return {"status": response.status, "type": response.headers["Content-Type"], "body": body}


def record(root: Path, folder: Path) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    os.environ["LEDGER_DIR"] = str(folder)
    for name in ("AGENTIHOOKS_SWARM_DATABASE_URL", "SWARM_AUTH_MODE"):
        os.environ.pop(name, None)
    sys.path.insert(0, str(root / "scripts/swarm_ledger"))
    sys.path.insert(1, str(root))
    import ledger_core as core
    import ledger_server
    import new_ledger

    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import RedisStore, SwarmConfig

    clock = [1700000000000]
    box = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    RedisStore(box.redis).create(SwarmConfig(SLUG, "/repo", 1, 0))
    results = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(core, "now_ms", lambda: clock[0]))
        stack.enter_context(patch.object(new_ledger.secrets, "token_hex", lambda n: "1" * (2 * n)))
        stack.enter_context(patch.object(new_ledger.secrets, "token_urlsafe", lambda n: "1" * 64))
        stack.enter_context(patch("hooks._redis.get_redis", lambda: box.redis))
        stack.enter_context(patch("scripts.inbox.store.connect", lambda environ=None: box))
        stack.enter_context(patch("scripts.inbox.store.now_ms", lambda: clock[0]))
        stack.enter_context(
            patch("scripts.gates.talk.Budget.apply", lambda self, doc, op, ctx, apply: apply(doc, op, ctx))
        )
        stack.enter_context(patch.object(ledger_server, "swarm_status", lambda slug, state=None: None))
        stack.enter_context(patch.object(ledger_server, "ALLOWED_HOSTS", {"127.0.0.1:8765"}))
        stack.enter_context(patch.object(ledger_server, "ALLOWED_ORIGINS", {"http://127.0.0.1:8765"}))
        content = {
            "title": "Replay",
            "overview": "Recorded requests",
            "sources": [],
            "phases": [{"title": "one", "description": "d"}],
        }
        stored = ledger_server.stored
        results.append({"created": new_ledger.create(SLUG, content), "stored": stored.export_document(SLUG)})
        token = stored.token(SLUG)
        server = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            requests = json.loads(Path(__file__).with_name("repository_requests.json").read_text())
            for body in requests:
                clock[0] += 1000
                results.append(
                    {
                        "request": body,
                        "response": exchange(server, token, body),
                        "stored": stored.export_document(SLUG),
                    }
                )
            results.append({"summaries": ledger_server.all_summaries()})
            for action in ("delete", "restore", "restore"):
                results.append(
                    {
                        "bin": exchange(server, token, {"action": action, "slug": SLUG}, "/api/bin", "POST"),
                        "summaries": ledger_server.all_summaries(),
                    }
                )
            results.append(
                {
                    "inbox": [
                        {"sender": i.sender, "address": i.address, "text": i.text, "state": i.state}
                        for i in box.pending_items(f"master@{SLUG}")
                    ]
                }
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("folder", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(record(args.root, args.folder), indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
