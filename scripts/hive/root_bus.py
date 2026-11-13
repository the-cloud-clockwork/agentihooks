"""The NATS bus settings of the workstation root hive, minted with the hub signer for the hive service alone."""

from pathlib import Path

from scripts.events.config import BACKEND_ENV, BIND_ENV, CREDENTIALS_ENV, URL_ENV
from scripts.hive import auth, nats_grant

FOLDER = "root-bus"
ENV_FILE = "bus.env"
CREDS_FILE = "bus.creds"


def env_text(url: str, creds: Path) -> str:
    """The bus env lines: nats at the hub url, bound to streams the hub made, with the creds file path."""
    return f"{BACKEND_ENV}=nats\n{URL_ENV}={url}\n{CREDENTIALS_ENV}={creds}\n{BIND_ENV}=1\n"


def write(home: Path, signer: nats_grant.Signer, member_id: str) -> Path:
    """Mint the member credential of member_id, write it and the bus env under home, and return the env path."""
    minted = nats_grant.mint(signer, member_id)
    folder = Path(home) / FOLDER
    creds = auth.write_private(folder / CREDS_FILE, minted)
    return auth.write_private(folder / ENV_FILE, env_text(signer.url, creds))
