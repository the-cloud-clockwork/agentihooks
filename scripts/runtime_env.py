import re
from collections.abc import Callable
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
