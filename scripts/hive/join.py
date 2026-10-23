from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from scripts.hive import auth, registry, server
from scripts.swarm_ledger import ledger_ca

SETTINGS = ("AGENTIHOOKS_BUS", "AGENTIHOOKS_BUS_URL", "AGENTIHOOKS_SWARM_URL")
REQUIRED = ("AGENTIHOOKS_HIVE_ID", "AGENTIHOOKS_HIVE_URL", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL")
SCOPED = "AGENTIHOOKS_BUS_SCOPED"


def save(home: Path, url: str, grant: dict, settings: Mapping[str, str]) -> Path:
    path = auth.write_env(home, url, grant)
    with path.open("a") as stream:
        for name in SETTINGS:
            if settings.get(name):
                stream.write(f"{name}={settings[name]}\n")
    return path


def enroll(path: Path, environ: Mapping[str, str]) -> str:
    from scripts.hive import cli

    try:
        with path.open() as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except OSError as exc:
        raise auth.HiveError(f"the invite file could not be read ({exc.strerror})") from exc
    if any(not values.get(name) for name in REQUIRED):
        raise auth.HiveError("the invite needs a hive id, endpoint and ledger credential")
    selected = {name: values[name] for name in (*REQUIRED, *SETTINGS) if values.get(name)}
    if any("\n" in value or "\r" in value for value in selected.values()):
        raise auth.HiveError("invite settings must each fit on one line")
    url = selected["AGENTIHOOKS_HIVE_URL"]
    credential = selected["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"]
    grant = cli._post(url, server.REFRESH_PATH, {"ledger_credential": credential}, "join", environ)
    if grant["id"] != selected["AGENTIHOOKS_HIVE_ID"]:
        raise auth.HiveError("the hive answered for a different invited member")
    if values.get(SCOPED) == "1" and grant.get("nats_credentials") and not grant.get("bus_scoped"):
        raise auth.HiveError("the hive offered an unscoped bus credential; ask its hub to list this member's swarms")
    home = registry.home(environ)
    save(home, url, {**grant, "ledger_credential": credential}, selected)
    ledger_ca.place(home, grant.get("ledger_ca"), environ)
    return grant["id"]
