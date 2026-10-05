#!/usr/bin/env python3
"""Post a chat message to a ledger as an agent.

Usage: chat_ledger.py <slug> --as <agent-name> <text>      (text "-" reads stdin)

Sends one chat message through the running ledger server; the operator's page shows it
within seconds. Env: LEDGER_DIR, LEDGER_HOST (127.0.0.1), LEDGER_PORT (8765).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core  # noqa: E402
import ledger_link  # noqa: E402

BASE = ledger_link.base()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slug")
    parser.add_argument("--as", dest="author", required=True)
    parser.add_argument("text")
    args = parser.parse_args()
    text = (sys.stdin.read() if args.text == "-" else args.text).strip()
    token = core.read_token(core.paths(args.slug)[0].read_text(encoding="utf-8"))
    op = {"op": "add", "thread": "chat", "id": f"m-{uuid.uuid4().hex[:10]}", "text": text, "by": args.author}
    request = urllib.request.Request(
        f"{BASE}/api/{args.slug}",
        method="PUT",
        data=json.dumps({"ops": [op]}).encode(),
        headers={"Content-Type": "application/json", "X-Ledger-Token": token or ""},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            rejected = json.loads(resp.read()).get("rejected", [])
    except urllib.error.HTTPError as exc:
        sys.exit(f"server refused: {exc.code} {exc.read().decode(errors='replace')}")
    except OSError as exc:
        sys.exit(f"ledger server not answering on {BASE}: {exc}")
    if rejected:
        sys.exit(f"message rejected: {rejected}")
    print(json.dumps({"posted": op["id"]}))


if __name__ == "__main__":
    main()
