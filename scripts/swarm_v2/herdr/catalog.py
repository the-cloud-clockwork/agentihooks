import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from scripts.swarm_v2.herdr.endpoints import EndpointRefused

LIST = ("herdr", "machine", "list", "--json")
REMOTE_SESSION = "default"
SAVED = re.compile(r"Saved SSH machine ([0-9A-Za-z_-]+)")
FIELDS = ("id", "label", "target", "session")

Run = Callable[[list[str]], subprocess.CompletedProcess]


@dataclass(frozen=True)
class Machine:
    profile_id: str
    label: str
    target: str
    session: str
    enabled: bool


def _machine(entry: object) -> Machine:
    if not isinstance(entry, dict):
        raise EndpointRefused("catalog_unreadable", "herdr machine list holds an entry that is not an object")
    found = [entry.get(name) for name in FIELDS]
    if not all(isinstance(value, str) and value for value in found) or type(entry.get("enabled")) is not bool:
        raise EndpointRefused("catalog_unreadable", "herdr machine list holds an incomplete machine")
    return Machine(*found, entry["enabled"])


def parse(text: str) -> tuple[Machine, ...]:
    try:
        listing = json.loads(text)
    except ValueError:
        raise EndpointRefused("catalog_unreadable", "herdr machine list is not JSON") from None
    if not isinstance(listing, list):
        raise EndpointRefused("catalog_unreadable", "herdr machine list is not a list")
    return tuple(_machine(entry) for entry in listing)


class Catalog:
    """Changes the client's saved machines only through herdr machine commands, never its catalog file."""

    def __init__(self, run: Run) -> None:
        self.run = run

    def _do(self, command: list[str], reason: str, message: str) -> subprocess.CompletedProcess:
        try:
            done = self.run(command)
        except (OSError, subprocess.TimeoutExpired):
            raise EndpointRefused("catalog_unreachable", f"{message}: herdr did not answer") from None
        if done.returncode != 0:
            raise EndpointRefused(reason, message)
        return done

    def machines(self) -> tuple[Machine, ...]:
        return parse(self._do(list(LIST), "catalog_unreadable", "herdr machine list failed").stdout)

    def add(self, label: str, target: str, session: str) -> str:
        command = ["herdr", "machine", "add", "--label", label, "--remote-session", session, target]
        done = self._do(command, "machine_refused", f"herdr could not save {label}")
        found = SAVED.search(f"{done.stdout}\n{done.stderr}")
        if found is None:
            raise EndpointRefused("catalog_ambiguous", f"herdr saved {label} without naming its profile id")
        return found.group(1)

    def rename(self, profile_id: str, label: str) -> None:
        command = ["herdr", "machine", "rename", profile_id, "--label", label]
        self._do(command, "rename_refused", f"herdr could not rename {profile_id}")

    def remove(self, profile_id: str) -> None:
        self._do(["herdr", "machine", "remove", profile_id], "remove_refused", f"herdr could not remove {profile_id}")
