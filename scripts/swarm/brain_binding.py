"""The one brain a managed swarm launch connects to, from the operator's binding file and never personal config.

The binding file is the swarm-brain-binding document Anton publishes: an instance name, an API and an MCP origin per
site, and for each the header, scheme and credential the client sends. That operator owned file is the authority for
which origins are authorized; this module only refuses origins no brain client may use. A launch carries only the
resolved origins and the names of the variables holding each credential, so no value is written to a launcher, a
profile home or a record.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

VARIABLE = "AGENTIHOOKS_SWARM_BRAIN"
FILE_VARIABLE = "AGENTIHOOKS_SWARM_BRAIN_BINDING"
REPORT = "brain_binding"
SERVER = "agentibrain"
BOUND, REFUSED = "bound", "refused"
KINDS = ("api", "mcp")
REFERENCES = {"api": "SWARM_BRAIN_API_TOKEN", "mcp": "SWARM_BRAIN_MCP_KEY"}
SCHEMES = ("", "Bearer")
CLUSTER_HOSTS = (".svc", ".svc.cluster.local")
_NAME = re.compile(r"SWARM_BRAIN_[A-Z0-9_]+")
_HEADER = re.compile(r"[A-Za-z0-9-]+")


class Refused(ValueError):
    pass


def path(environ: Mapping[str, str]) -> Path:
    if environ.get(FILE_VARIABLE):
        return Path(environ[FILE_VARIABLE]).expanduser()
    home = Path(environ.get("AGENTIHOOKS_HOME") or Path(environ.get("HOME") or Path.home()) / ".agentihooks")
    return home / "swarm" / "brain-binding.json"


def site(environ: Mapping[str, str]) -> str:
    return "cluster" if environ.get("KUBERNETES_SERVICE_HOST") else "lan"


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def _authorized(url: object, where: str) -> str:
    if not isinstance(url, str):
        raise Refused("the binding names no endpoint")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or parts.username or parts.password or parts.query or parts.fragment:
        raise Refused(f"endpoint {origin(url)} is not a plain origin")
    if parts.scheme == "https" or (parts.scheme == "http" and where == "cluster" and host.endswith(CLUSTER_HOSTS)):
        return url.removesuffix("/")
    raise Refused(f"endpoint {origin(url)} is not an authorized brain origin")


def _operator_file(found: Path) -> dict:
    try:
        info = found.lstat()
    except OSError:
        raise Refused(f"no swarm brain binding at {found}") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Refused(f"swarm brain binding {found} is not an operator owned file")
    try:
        doc = json.loads(found.read_bytes())
    except (OSError, ValueError):
        raise Refused(f"swarm brain binding {found} is not readable JSON") from None
    if not isinstance(doc, dict):
        raise Refused(f"swarm brain binding {found} is not a JSON object")
    return doc


def _credential(doc: dict, kind: str) -> dict:
    spec = (doc.get("credentials") or {}).get(kind)
    if not isinstance(spec, dict):
        raise Refused(f"the binding names no {kind} credential")
    header, scheme = spec.get("header"), spec.get("scheme") or ""
    reference = spec.get("env") or REFERENCES[kind]
    if not isinstance(header, str) or not _HEADER.fullmatch(header) or scheme not in SCHEMES:
        raise Refused(f"the {kind} credential header is malformed")
    if not isinstance(reference, str) or not _NAME.fullmatch(reference):
        raise Refused(f"the {kind} credential reference must be a SWARM_BRAIN_ variable")
    return {f"{kind}_header": header, f"{kind}_scheme": scheme, f"{kind}_env": reference}


def resolve(environ: Mapping[str, str], credentials: Mapping[str, str] | None = None) -> dict:
    """The bound connection for this launch; Refused names why there is none.

    ``credentials`` is where the credential variables must be set, when that is not ``environ`` itself.
    """
    doc = _operator_file(path(environ))
    where = site(environ)
    origins = (doc.get("origins") or {}).get(where)
    if not isinstance(origins, dict):
        raise Refused(f"the binding has no {where} origins")
    api, mcp = (_authorized(origins.get(kind), where) for kind in KINDS)
    if origin(api) == origin(mcp):
        raise Refused("the API and MCP endpoints share one origin")
    bound = {"status": BOUND, "instance": str(doc.get("instance") or ""), "site": where, "api": api, "mcp": mcp}
    for kind in KINDS:
        bound.update(_credential(doc, kind))
    if (bound["api_header"].lower(), bound["api_scheme"]) != ("authorization", "Bearer"):
        raise Refused("the API credential must be an Authorization Bearer token")
    held = environ if credentials is None else credentials
    unset = [bound[f"{kind}_env"] for kind in KINDS if not held.get(bound[f"{kind}_env"])]
    if unset:
        raise Refused(f"credential {', '.join(unset)} is unset")
    return bound


def decide(environ: Mapping[str, str], credentials: Mapping[str, str] | None = None) -> dict:
    try:
        return resolve(environ, credentials)
    except Refused as exc:
        return {"status": REFUSED, "reason": str(exc)}


def current(environ: Mapping[str, str]) -> dict | None:
    raw = environ.get(VARIABLE)
    return json.loads(raw) if raw else None


def apply(servers: dict, environ: Mapping[str, str]) -> dict:
    """The rendered servers with the managed brain as the only agentibrain; unchanged outside a managed launch."""
    binding = current(environ)
    if binding is None:
        return servers
    kept = {name: spec for name, spec in servers.items() if name != SERVER}
    if binding["status"] != BOUND:
        return kept
    value = f"${{{binding['mcp_env']}}}"
    header = f"{binding['mcp_scheme']} {value}" if binding["mcp_scheme"] else value
    return {**kept, SERVER: {"type": "http", "url": binding["mcp"], "headers": {binding["mcp_header"]: header}}}


def exports(environ: Mapping[str, str]) -> str:
    """Launcher lines that make the binding authoritative over whatever the operator's env files exported.

    A bound launch is re-checked in the launcher shell, the environment the harness receives: a credential missing
    there refuses the brain instead of mounting it with an empty key.
    """
    binding = current(environ)
    if binding is None:
        return ""
    common = "export BRAIN_SOURCE_TYPE=none\nunset KB_ROUTER_TOKEN\n"
    refused = "unset BRAIN_URL BRAIN_HTTP_TOKEN\nexport BRAIN_ENABLED=false BRAIN_WRITER_ENABLED=false\n"
    if binding["status"] != BOUND:
        return f"export {VARIABLE}={shlex.quote(environ[VARIABLE])}\n{common}{refused}"
    missing = {"status": REFUSED, "reason": "a brain credential is unset in the launch shell"}
    present = " && ".join(f'[ -n "${{{binding[f"{kind}_env"]}:-}}" ]' for kind in KINDS)
    return (
        f"{common}if {present}; then\n"
        f"export {VARIABLE}={shlex.quote(environ[VARIABLE])}\n"
        f"export BRAIN_URL={shlex.quote(binding['api'])}\n"
        f'export BRAIN_HTTP_TOKEN="${{{binding["api_env"]}}}"\n'
        "export BRAIN_ENABLED=true BRAIN_WRITER_ENABLED=true\n"
        "else\n"
        f"export {VARIABLE}={shlex.quote(json.dumps(missing))}\n{refused}"
        "fi\n"
    )


def report(environ: Mapping[str, str]) -> list[str]:
    raw = environ.get(VARIABLE)
    return [f"{REPORT}={raw}"] if raw else []


def record(fields: Mapping[str, str]) -> dict:
    return {"brain": json.loads(fields[REPORT])} if fields.get(REPORT) else {}


def audience(environ: Mapping[str, str]) -> bool:
    """True when a managed session may send its brain credential to BRAIN_URL: the operator's file still binds it."""
    binding = current(environ)
    if binding is None or binding.get("status") != BOUND:
        return False
    try:
        bound = resolve(environ)
    except Refused:
        return False
    url = environ.get("BRAIN_URL")
    return bool(url) and origin(url) == origin(bound["api"]) == origin(binding["api"])
