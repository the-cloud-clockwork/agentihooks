import re
from collections.abc import Callable, Mapping
from pathlib import Path
from shlex import quote

KEY = "LANGFUSE_MCP_URL"
START = "# BEGIN agentihooks sampler environment"
END = "# END agentihooks sampler environment"


def seed(seeder: Callable[[], list[str]], destination: Path, settings: dict) -> list[str]:
    added = seeder()
    environment = settings.get("env", {})
    endpoint = environment.get(KEY, settings.get("_agentihooks", {}).get("env", {}).get(KEY, ""))
    original = destination.read_text() if destination.exists() else ""
    body = original
    block = re.search(rf"(?ms)^{re.escape(START)}\n.*?^{re.escape(END)}(?:\n|$)", body)
    if block:
        body = body[: block.start()].rstrip() + "\n" + body[block.end() :].lstrip("\n")
    active = {
        line.strip().removeprefix("export ").partition("=")[0].strip()
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if endpoint and KEY not in active:
        body = f"{body.rstrip()}\n\n{START}\n{KEY}={quote(endpoint)}\n{END}\n"
    if body != original:
        destination.write_text(body)
        destination.chmod(0o600)
    return added


def shell_kept(environ: Mapping[str, str]) -> set[str]:
    """The shell values the runtime env files never replace: the scratch Redis setting a proof swarm launcher
    exported, so the session it starts binds on the proof Redis rather than the shared one."""
    from scripts.swarm.naming import PROOF_RE

    swarm = environ.get("AGENTIHOOKS_SWARM")
    return ({"AGENTIHOOKS_SWARM_REDIS_URL"} & set(environ)) if swarm and PROOF_RE.fullmatch(swarm) else set()
