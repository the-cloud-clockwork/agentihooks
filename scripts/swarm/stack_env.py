"""The local swarm stack's settings: one generated .env kept across runs and the client settings derived from it."""

import os
import secrets
import shlex
import socket
import urllib.parse
from pathlib import Path

BIND = "127.0.0.1"


def free_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind((BIND, preferred))
        except OSError:
            probe.bind((BIND, 0))
        return probe.getsockname()[1]


def write_private(path: Path, text: str) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    lines = (line.partition("=") for line in path.read_text().splitlines() if not line.startswith("#"))
    return {key.strip(): value.strip() for key, sep, value in lines if sep}


def settings(env_file: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    values = read_env(env_file)
    selected = {
        key: environ[key]
        for key in ("AGENTIHOOKS_SWARM_DATABASE_URL", "AGENTIHOOKS_SWARM_REDIS_URL")
        if environ and environ.get(key) and values.get(key) != environ[key]
    }
    values.update(selected)
    defaults = {
        "AGENTIHOOKS_DEPLOYMENT": lambda: "compose",
        "SWARM_BIND": lambda: BIND,
        "SWARM_HIVE_ID": lambda: "compose-controller",
        "SWARM_IMAGE_TAG": lambda: "release",
        "SWARM_REDIS_PASSWORD": secrets.token_urlsafe,
        "SWARM_PORT": lambda: str(free_port(8765)),
        "SWARM_REDIS_PORT": lambda: str(free_port(6379)),
    }
    missing = {key: make() for key, make in defaults.items() if not values.get(key)}
    if missing or selected:
        values.update(missing)
        write_private(env_file, "".join(f"{key}={value}\n" for key, value in values.items()))
    env_file.chmod(0o600)
    return values


def client_env(values: dict[str, str], home: Path) -> str:
    scheme = values.get("SWARM_REDIS_SCHEME", "redis")
    redis_url = values.get("AGENTIHOOKS_SWARM_REDIS_URL") or values.get("SWARM_ADOPT_REDIS_URL")
    if not redis_url:
        netloc = f"swarm:{values['SWARM_REDIS_PASSWORD']}@{BIND}:{values['SWARM_REDIS_PORT']}"
        redis_url = urllib.parse.urlunsplit((scheme, netloc, "/0", "", ""))
    text = (
        "AGENTIHOOKS_DEPLOYMENT=compose\n"
        f"LEDGER_URL=http://{BIND}:{values['SWARM_PORT']}\n"
        f"AGENTIHOOKS_SWARM_REDIS_URL={shlex.quote(redis_url)}\n"
    )
    if database := values.get("AGENTIHOOKS_SWARM_DATABASE_URL"):
        text += f"AGENTIHOOKS_SWARM_DATABASE_URL={shlex.quote(database)}\n"
    if scheme == "rediss" and not values.get("AGENTIHOOKS_SWARM_REDIS_URL"):
        tls = home / values.get("SWARM_TLS_DIR", "./deploy/compose/tls") / "ca.pem"
        text += f"SSL_CERT_FILE={os.path.normpath(tls)}\n"
    return text
