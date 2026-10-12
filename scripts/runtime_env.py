from collections.abc import Callable
from pathlib import Path
from shlex import quote

KEY = "LANGFUSE_MCP_URL"
START = "# BEGIN agentihooks sampler environment"
END = "# END agentihooks sampler environment"


def seed(seeder: Callable[[], list[str]], destination: Path, settings: dict) -> list[str]:
    added = seeder()
    environment = settings.get("env", settings.get("_agentihooks", {}).get("env", {}))
    endpoint = environment.get(KEY, "")
    original = destination.read_text()
    body = original
    if START in body:
        before, rest = body.split(START, 1)
        _, after = rest.split(END, 1)
        body = before.rstrip() + "\n" + after.lstrip("\n")
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
