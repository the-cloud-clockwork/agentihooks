"""The NATS bus settings of the workstation root hive, minted with the hub signer for the hive service alone."""

from pathlib import Path

from dotenv import dotenv_values

from scripts.events.config import BACKEND_ENV, BIND_ENV, CREDENTIALS_ENV, SCOPED_ENV, SWARMS_ENV, URL_ENV, BusConfig
from scripts.hive import auth, nats_grant

FOLDER = "root-bus"
ENV_FILE = "bus.env"
CREDS_FILE = "bus.creds"
CONTROL_LINE = 65536
CONNECT_ROOM = 4096


def env_text(url: str, creds: Path, swarms: tuple[str, ...] | None) -> str:
    """The bus env lines: nats at the hub url, bound to streams the hub made, with the creds file path."""
    text = f"{BACKEND_ENV}=nats\n{URL_ENV}={url}\n{CREDENTIALS_ENV}={creds}\n{BIND_ENV}=1\n"
    if swarms is not None:
        text += f"{SCOPED_ENV}=1\n{SWARMS_ENV}={','.join(swarms)}\n"
    return text


def write(home: Path, signer: nats_grant.Signer, member_id: str) -> Path:
    """Mint the member credential of member_id, write it and the bus env under home, and return the env path."""
    minted = nats_grant.mint(signer, member_id)
    swarms = None if signer.member_swarms is None else signer.member_swarms[member_id]
    if len(minted) > CONTROL_LINE - CONNECT_ROOM:
        raise auth.HiveError(
            f"the {member_id} credential is {len(minted)} bytes for {len(swarms or ())} swarms, past the "
            f"{CONTROL_LINE} byte NATS control line; name fewer swarms with --swarm"
        )
    folder = Path(home) / FOLDER
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    creds = auth.write_private(folder / CREDS_FILE, minted)
    return auth.write_private(folder / ENV_FILE, env_text(signer.url, creds, swarms))


def verify(path: Path) -> None:
    """Connect to the hub once with the bus env at path, raising HiveError when the hub refuses it."""
    from scripts.events import nats

    values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    try:
        nats.open_bus(BusConfig.from_environ(values)).close()
    except Exception as exc:
        raise auth.HiveError(
            f"the hub refused the root hive credential in {path} ({type(exc).__name__}: {exc})"
        ) from exc


def remove(home: Path) -> bool:
    """Delete the root hive bus env and credential under home, returning whether any existed."""
    folder = Path(home) / FOLDER
    found = [path for path in (folder / ENV_FILE, folder / CREDS_FILE) if path.is_file()]
    for path in found:
        path.unlink()
    return bool(found)
