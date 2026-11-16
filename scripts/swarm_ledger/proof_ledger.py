import argparse
import json
import os
import sys
import urllib.error
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.swarm_ledger import ledger_link

TIMEOUT = 10


def call(path: str, payload: dict[str, Any] | None, environ: Mapping[str, str]) -> Any:
    """One proof ledger request through the ledger API route code in process, as the controller."""
    from scripts.tick.adapters import database_ledger

    credential = environ.get("AGENTIHOOKS_CONTROLLER_CREDENTIAL")
    if not credential:
        sys.exit("AGENTIHOOKS_CONTROLLER_CREDENTIAL is not set")
    command = "GET" if payload is None else "POST"
    try:
        return database_ledger.exchanged(command, f"/api/v1/{path}", payload, {"X-Controller-Credential": credential})[
            "data"
        ]
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and payload is None:
            return None
        message = json.loads(exc.read())["error"]["message"]
        sys.exit(f"server refused: {exc.code} {message}")


def state(slug: str, environ=os.environ) -> str:
    if call(f"ledgers/{slug}/metadata", None, environ) is None:
        return "missing"
    return "live" if call(f"bin/{slug}", None, environ) is None else "binned"


def create(content: dict, environ=os.environ) -> dict:
    agent, task = environ.get("AGENTIHOOKS_AGENT_NAME"), environ.get("AGENTIHOOKS_SWARM_TASK")
    return call("proof-ledgers", {"agent": agent, "task": task, "content": content}, environ)


def remove(slug: str, environ=os.environ) -> dict:
    return call(f"proof-ledgers/{slug}/remove", {}, environ)


def migrate(slug: str, owner: dict, environ=os.environ) -> dict:
    return call(f"proof-ledgers/{slug}/migrate", owner, environ)


def controller_control(slug: str, owner: dict, request: dict, environ=os.environ) -> dict:
    return call(f"proof-ledgers/{slug}/controller-controls", {"owner": owner, "request": request}, environ)


def _control_call(slug: str, route: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    """One proof control request through the ledger API route code in process, judged by ``headers``."""
    from scripts.tick.adapters import database_ledger

    try:
        return database_ledger.exchanged("POST", f"/api/v1/proof-ledgers/{slug}/{route}", payload, headers)["data"]
    except urllib.error.HTTPError as exc:
        message = json.loads(exc.read())["error"]["message"]
        sys.exit(f"proof control refused: {exc.code} {message}")


def control(slug: str, payload: dict, environ=os.environ) -> dict:
    from scripts.swarm_ledger import ledger

    name, parent = environ.get("AGENTIHOOKS_AGENT_NAME"), environ.get("AGENTIHOOKS_SWARM")
    if not name or not parent:
        sys.exit("proof controls need the creating engineer identity and parent swarm")
    if ledger_link.remote(environ):
        token = environ.get("AGENTIHOOKS_LEDGER_AGENT_TOKEN") or ledger.launch_token(parent, name)
        headers = {"X-Ledger-Agent": name, "X-Ledger-Token": token}
    else:
        headers = ledger.local_credentials(parent, name, False)
    issued = _control_call(slug, "credential", {}, headers)
    return _control_call(slug, "controls", payload, {"X-Proof-Credential": issued["credential"]})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("new").add_argument("--content", required=True)
    sub.add_parser("remove").add_argument("slug")
    args = parser.parse_args(argv)
    if args.action == "new":
        result = create(json.loads(Path(args.content).read_bytes()))
    else:
        result = remove(args.slug)
    print(json.dumps(result))
