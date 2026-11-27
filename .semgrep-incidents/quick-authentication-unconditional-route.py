# ruff: noqa: F811

import subprocess
from collections.abc import Mapping
from pathlib import Path

from scripts.quick import registry


def argv(swarm: registry.Swarm, seat: registry.Seat, prompt_file: Path) -> list[str]:
    # ruleid: quick-authentication-unconditional-route
    return ["--agent", seat.harness, "--routed-only"]


def argv(swarm: registry.Swarm, seat: registry.Seat, prompt_file: Path) -> list[str]:
    policy = ["--routed-only"] if seat.auth == "routed" else ["--auth", seat.auth]
    # ok: quick-authentication-unconditional-route
    return ["--agent", seat.harness, *policy]


def start_all(folder: Path, swarm: registry.Swarm, base: Mapping[str, str]) -> None:
    # ruleid: quick-initial-native-launch-without-take
    subprocess.Popen(["native"], env=dict(base))


def start_all(folder: Path, swarm: registry.Swarm, base: Mapping[str, str]) -> None:
    if registry.take(folder, "eng-1", registry.READY, registry.LAUNCHING) is None:
        return
    # ok: quick-initial-native-launch-without-take
    subprocess.Popen(["native"], env=dict(base))
