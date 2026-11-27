"""The NATS bus settings of the workstation root hive, minted with the hub signer for the hive service alone."""

from collections.abc import Callable
from pathlib import Path

from dotenv import dotenv_values

from scripts.events import nkey
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
    swarms = nats_grant.require_listed(signer, member_id)
    minted = nats_grant.mint(signer, member_id)
    if len(minted) > CONTROL_LINE - CONNECT_ROOM:
        raise auth.HiveError(
            f"the {member_id} credential is {len(minted)} bytes for {len(swarms)} swarms, past the "
            f"{CONTROL_LINE} byte NATS control line; name fewer swarms with --swarm"
        )
    folder = Path(home) / FOLDER
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    creds = auth.write_private(folder / CREDS_FILE, minted)
    return auth.write_private(folder / ENV_FILE, env_text(signer.url, creds, swarms))


def stale(home: Path, signer: nats_grant.Signer, member_id: str) -> bool:
    """Whether the publish or subscribe grant of the credential stored under home differs from a fresh mint.

    A stored credential that does not read or decode counts as differing, so the re-mint replaces it.
    """
    try:
        stored = nkey.parse_creds((Path(home) / FOLDER / CREDS_FILE).read_text())[0]
        granted = nkey.claims(stored)["nats"]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    except (KeyError, OSError, ValueError):
        return True
    swarms = nats_grant.require_listed(signer, member_id)
    user = nats_grant.member_user(signer, member_id)
    fresh = nats_grant.member_claims(user.public, swarms, member=member_id)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return any(granted.get(key) != fresh[key] for key in ("pub", "sub"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def refresh(home: Path, signer: Callable[[], nats_grant.Signer | None], member_id: str) -> Path | None:
    """Re-mint the stored credential of member_id when its grant is stale, returning the rewritten env path.

    Reads no signer when home stores no credential, and raises HiveError when no signer can mint one.
    """
    if not (Path(home) / FOLDER / CREDS_FILE).is_file():
        return None
    found = signer()
    if found is None:
        raise auth.HiveError(f"{nats_grant.SIGNER_ENV} names no hub NATS signer")
    return write(home, found, member_id) if stale(home, found, member_id) else None


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
