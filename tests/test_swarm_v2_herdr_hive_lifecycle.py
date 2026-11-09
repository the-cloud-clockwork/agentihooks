import subprocess
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from scripts.hive import machine, registry, remove
from scripts.swarm.store import RedisStore
from scripts.swarm_v2.herdr.operator_service import Service
from scripts.swarm_v2.herdr.upkeep import Upkeep
from tests import sv2_hdr03_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.feature("herdr-hive-lifecycle")]
NOW = 2_000_000
LIMIT = 15 * 60 * 1000
LABEL = "swarm-hive-003"
TARGET = "ssh://worker@10.10.30.207:2225"


class Client(cases.Herdr):
    def __init__(self) -> None:
        super().__init__(
            [
                {"id": "hive", "label": LABEL, "target": TARGET, "session": "default", "enabled": True},
                {"id": "manual", "label": "devbox", "target": "ssh://user@devbox", "session": "default"},
            ],
            set(),
        )

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        if command[0] == "ssh-keygen":
            self.commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")
        return super().__call__(command)


def setup(tmp_path: Path, beat: int | None) -> tuple[Service, Client, RedisStore]:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    if beat is not None:
        registry.update(store.redis, "hive-three", [f"name={LABEL}", f"heartbeat_at={beat}"])
    client = Client()
    terminal = cases.client_terminal(tmp_path, client, "test-workers")
    known = tmp_path / ".ssh" / "known_hosts"
    known.parent.mkdir(parents=True)
    known.write_text("[10.10.30.207]:2225 ssh-ed25519 fixture\n")

    def connect() -> RedisStore:
        return store

    def clock() -> float:
        return NOW / 1000

    return Service(Upkeep(terminal), connect, clock), client, store


@pytest.mark.parametrize("beat", [None, NOW - LIMIT - 1])
def test_removed_and_unplugged_hives_leave_herdr_with_their_host_key(
    tmp_path: Path, beat: int | None, capsys: pytest.CaptureFixture[str]
) -> None:
    service, client, _ = setup(tmp_path, beat)

    service.run_pass()

    assert [entry["label"] for entry in client.machines] == ["devbox"]
    assert ["ssh-keygen", "-R", "[10.10.30.207]:2225", "-f", str(tmp_path / ".ssh" / "known_hosts")] in client.commands
    reason = "hive removed" if beat is None else "hive unreachable for 900 seconds"
    assert f"removed herdr machine {LABEL}: {reason}" in capsys.readouterr().out


@pytest.mark.parametrize("age", [0, 30_000, LIMIT - 1])
def test_a_brief_restart_keeps_the_saved_machine_and_host_key(tmp_path: Path, age: int) -> None:
    service, client, _ = setup(tmp_path, NOW - age)

    service.run_pass()

    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]
    assert not client.mutating()
    assert not [command for command in client.commands if command[0] == "ssh-keygen"]


def test_lifecycle_cleanup_runs_even_when_worker_attempts_do_not_change(tmp_path: Path) -> None:
    service, client, _ = setup(tmp_path, NOW)
    service.run_pass()

    def clock() -> float:
        return (NOW + LIMIT + 1) / 1000

    service.clock = clock
    service.run_pass()

    assert [entry["label"] for entry in client.machines] == ["devbox"]


def test_a_recovered_hive_keeps_its_machine_past_the_first_outage_deadline(tmp_path: Path) -> None:
    service, client, store = setup(tmp_path, NOW - LIMIT + 1)
    service.run_pass()
    registry.update(store.redis, "hive-three", [f"heartbeat_at={NOW + LIMIT}"])

    def clock() -> float:
        return (NOW + LIMIT + 1) / 1000

    service.clock = clock
    service.run_pass()

    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]


def test_explicit_removal_reaches_another_client_with_a_custom_hive_name(tmp_path: Path) -> None:
    service, client, store = setup(tmp_path, NOW)
    client.machines[0]["label"] = "custom-hive"
    plan = remove.Plan("hive-three", "custom-hive", "")

    remove.unregister(store.redis, plan, tmp_path)
    service.run_pass()

    assert [entry["label"] for entry in client.machines] == ["devbox"]


def test_dry_run_removal_does_not_retire_another_clients_machine(tmp_path: Path) -> None:
    service, client, store = setup(tmp_path, NOW)
    plan = remove.Plan("hive-three", LABEL, "", dry_run=True)

    remove.unregister(store.redis, plan, tmp_path)
    service.run_pass()

    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]


def test_readding_a_removed_name_clears_its_removal_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, store = setup(tmp_path, NOW)
    plan = remove.Plan("hive-three", LABEL, "")
    remove.unregister(store.redis, plan, tmp_path)

    def ui(*args: Any) -> None:
        return None

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(machine, "_ui", ui)
    machine.save(store.redis, "operator", TARGET, LABEL, run)

    assert LABEL not in machine.removed(store.redis)
