import json
import subprocess
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from scripts.hive import auth, machine, registry, remove
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
    reachable: bool = False

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
        if command[:2] == ["herdr", "--machine"]:
            self.commands.append(list(command))
            return subprocess.CompletedProcess(command, 0 if self.reachable else 1, "", "")
        return super().__call__(command)


def setup(tmp_path: Path, beat: int | None) -> tuple[Service, Client, RedisStore]:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    if beat is not None:
        registry.update(store.redis, "hive-three", [f"name={LABEL}"])
        registry.beat(store.redis, "hive-three", {"heartbeat_at": beat})
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


def advance(service: Service, elapsed_ms: int) -> None:
    def clock() -> float:
        return (NOW + elapsed_ms) / 1000

    service.clock = clock


@pytest.mark.parametrize("beat", [None, NOW - LIMIT, NOW - LIMIT - 1])
def test_removed_and_unplugged_hives_leave_herdr_with_their_host_key(
    tmp_path: Path, beat: int | None, capsys: pytest.CaptureFixture[str]
) -> None:
    service, client, _ = setup(tmp_path, beat)

    service.run_pass()
    if beat is not None:
        assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]
        advance(service, LIMIT)
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
    registry.beat(store.redis, "hive-three", {"heartbeat_at": NOW + LIMIT})
    client.reachable = True

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


@pytest.mark.parametrize("label", ["devbox", "swarm-hive-tools", "my-swarm-hive-003"])
def test_unrelated_manual_machine_names_are_never_removed(tmp_path: Path, label: str) -> None:
    service, client, _ = setup(tmp_path, None)
    client.machines[0]["label"] = label

    service.run_pass()

    assert len(client.machines) == 2
    assert not client.mutating()


@pytest.mark.parametrize("elapsed", [LIMIT - 1, LIMIT, LIMIT + 1])
def test_a_hive_that_never_reports_has_a_bounded_startup_grace(tmp_path: Path, elapsed: int) -> None:
    service, client, _ = setup(tmp_path, 0)
    service.run_pass()
    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]

    advance(service, elapsed)
    service.run_pass()

    expected = ["devbox"] if elapsed >= LIMIT else [LABEL, "devbox"]
    assert [entry["label"] for entry in client.machines] == expected


def test_an_unreadable_catalog_reports_failure_without_removing_machines(tmp_path: Path) -> None:
    service, client, _ = setup(tmp_path, None)
    client.listing = "unreadable"

    report = service.run_pass()

    assert report is not None
    assert "hives" in report.refused
    assert len(client.machines) == 2
    assert not client.mutating()


def test_a_reachable_hive_is_kept_even_when_its_heartbeat_daemon_is_stale(tmp_path: Path) -> None:
    service, client, _ = setup(tmp_path, NOW - LIMIT - 1)
    client.reachable = True

    service.run_pass()

    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]
    assert ["herdr", "--machine", LABEL, "status", "server", "--json"] in client.commands


def test_an_outage_after_a_stale_heartbeat_still_gets_the_full_restart_grace(tmp_path: Path) -> None:
    service, client, _ = setup(tmp_path, NOW - LIMIT - 1)
    service.run_pass()
    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]

    advance(service, LIMIT - 1)
    service.run_pass()

    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]


def test_startup_failure_expires_even_when_the_member_has_no_hive_record(tmp_path: Path) -> None:
    service, client, store = setup(tmp_path, None)
    auth.mint(store.redis, LABEL, "redis://localhost")
    service.run_pass()
    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]

    advance(service, LIMIT)
    service.run_pass()

    assert [entry["label"] for entry in client.machines] == ["devbox"]


def test_outage_deadlines_survive_scheduled_service_recreation(tmp_path: Path) -> None:
    service, client, _ = setup(tmp_path, 0)
    service.run_pass()
    saved = json.loads(json.dumps(service.saved()))
    replacement = Service(Upkeep(service.upkeep.terminal), service.connect)
    replacement.resume(saved)

    advance(replacement, LIMIT)
    replacement.run_pass()

    assert [entry["label"] for entry in client.machines] == ["devbox"]


def test_a_successful_connection_resets_the_outage_deadline(tmp_path: Path) -> None:
    service, client, _ = setup(tmp_path, 0)
    service.run_pass()
    advance(service, LIMIT - 1)
    client.reachable = True
    service.run_pass()

    advance(service, LIMIT + 1)
    client.reachable = False
    service.run_pass()

    assert [entry["label"] for entry in client.machines] == [LABEL, "devbox"]
    advance(service, 2 * LIMIT + 1)
    service.run_pass()
    assert [entry["label"] for entry in client.machines] == ["devbox"]
