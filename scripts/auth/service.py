"""Auth service HTTP server: exchanges today's swarm credentials and hive member credentials for signed tokens."""

import argparse
import json
import os
import re
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from scripts.auth import tokens
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger import ports
from scripts.swarm_ledger.api.errors import APIError

PORT = 8768
ROUTE = re.compile(r"/v1/ledgers/([a-z0-9][a-z0-9-]*)/(token|agent-token)")
NOT_FOUND = APIError(404, "not_found", "No such route")


def signed(principal: str, slug: str) -> str:
    """A signed token for the principal, or a 503 when this service holds no signing key."""
    secret = tokens.key(os.environ)
    if not secret:
        raise APIError(503, "unavailable", "The auth service has no signing key")
    return tokens.sign(secret, principal, slug)


def exchange(headers: Mapping[str, str], slug: str) -> dict:
    """Verify an operator, agent, tick or controller credential and answer with a signed token for its principal.

    The issuer keeps verifying today's credentials whatever AGENTIHOOKS_AUTH_LEGACY says: that switch governs the
    services that verify signed tokens, and this exchange is how a legacy holder obtains one.
    """
    principal = authority.credential_caller(headers, "", slug)
    if principal is None and authority.controller_credential(headers.get("X-Controller-Credential")):
        principal = ""
    if principal is None:
        raise APIError(403, "forbidden", "Missing or wrong credential")
    return {"principal": principal, "token": signed(principal, slug), "expires_in": tokens.TTL_S}


def agent_token(headers: Mapping[str, str], slug: str) -> dict:
    """Issue a hive agent token and a signed token for an agent placed on the calling member's hive."""
    agent = headers.get("X-Ledger-Agent")
    if not agent:
        raise APIError(403, "forbidden", "An agent token names its agent in X-Ledger-Agent")
    member = authority.hive_member(headers.get("X-Hive-Credential"))
    if member is None:
        raise APIError(403, "forbidden", "Missing or wrong hive credential")
    if not authority.hive_agent(member, slug, agent):
        raise APIError(403, "forbidden", "Agent is not placed on this member's hive")
    try:
        token = authority.hive_agent_token(member, slug, agent)
    except ports.hive().auth.HiveError as exc:
        raise APIError(403, "forbidden", str(exc)) from exc
    return {"agent": agent, "token": token, "auth": signed(agent, slug), "expires_in": tokens.TTL_S}


def send(request: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    """Write the JSON body with its status on the request."""
    payload = json.dumps(body).encode()
    request.send_response(status)
    request.send_header("Content-Type", "application/json")
    request.send_header("Content-Length", str(len(payload)))
    request.end_headers()
    request.wfile.write(payload)


def get(request: BaseHTTPRequestHandler) -> None:
    """Answer the health check; every other GET is not found."""
    if request.path == "/healthz":
        send(request, 200, {"ok": True})
        return
    send(request, NOT_FOUND.status, NOT_FOUND.envelope())


def post(request: BaseHTTPRequestHandler) -> None:
    """Route token exchange and agent token issue."""
    match = ROUTE.fullmatch(urlsplit(request.path).path)
    try:
        if match is None:
            raise NOT_FOUND
        slug, kind = match.groups()
        data = exchange(request.headers, slug) if kind == "token" else agent_token(request.headers, slug)
    except APIError as refused:
        send(request, refused.status, refused.envelope())
        return
    send(request, 200, {"data": data})


Handler = type("Handler", (BaseHTTPRequestHandler,), {"do_GET": get, "do_POST": post, "send": send})


def server(address: tuple[str, int]) -> ThreadingHTTPServer:
    """An auth service bound to the address."""
    return ThreadingHTTPServer(address, Handler)


def main(argv: list[str] | None = None) -> int:
    """Run the auth service: serve [--host HOST] [--port PORT]."""
    parser = argparse.ArgumentParser(prog="python -m scripts.auth")
    commands = parser.add_subparsers(required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)
    server((args.host, args.port)).serve_forever()
    return 0
