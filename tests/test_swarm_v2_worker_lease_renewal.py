import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scripts.swarm_v2 import contracts
from scripts.swarm_v2.broadcast_bridge import grant_path
from scripts.swarm_v2.worker import lease_renewal

pytestmark = pytest.mark.unit

EXECUTION = "exe-" + "c" * 32
CONTROL = "http://controller.swarm.invalid:8780"
GRANT = "sv2.grant-fixture"
WALL = 1_800_000_000.0
ADMISSION = {
    "schema_version": "2.1",
    "execution_id": EXECUTION,
    "task_id": "t1",
    "task_generation": 4,
    "controller_epoch": 7,
    "owner_identity": "engineer@fixture-1",
    "lease_deadline_ms": int(WALL * 1000) + 60_000,
    "lease_deadline": "2027-01-15T08:01:00.000Z",
    "command_watermark": 0,
    "archive_watermark": 0,
    "grant_id": "lgr-" + "d" * 32,
    "registered_at": "2027-01-15T08:00:00Z",
}


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.wall = WALL

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds


class Controller:
    def __init__(self, clock: Clock, *answers: tuple[int, object]) -> None:
        self.clock, self.answers, self.calls = clock, list(answers), []

    def __call__(self, method: str, url: str, grant: str, body: dict) -> tuple[int, object]:
        self.calls.append((method, url, grant, body))
        if self.answers:
            return self.answers.pop(0)
        if method == "POST":
            return 200, {"execution_id": EXECUTION, "credential": "sv2.grant-renewed", "expires_at": "x"}
        return 200, acknowledged(self.clock)


def acknowledged(clock: Clock, **changes) -> dict:
    return {**ADMISSION, "lease_deadline_ms": int(clock.wall * 1000) + 60_000, **changes}


def attempt_folder(tmp_path: Path, admission: dict | None = ADMISSION) -> Path:
    attempt = tmp_path / EXECUTION
    (attempt / "run").mkdir(parents=True)
    grant_path(attempt).write_text(GRANT)
    (attempt / "launch.json").write_text(json.dumps({"control_url": CONTROL + "/"}))
    if admission is not None:
        (attempt / lease_renewal.ADMISSION).write_text(json.dumps(admission))
    return attempt


def renewer(tmp_path: Path, clock: Clock, controller: Controller) -> lease_renewal.Renewer:
    found = lease_renewal.load(attempt_folder(tmp_path), controller, clock.monotonic, clock.time)
    assert found is not None
    return found


def heartbeats(controller: Controller) -> list[tuple]:
    return [call for call in controller.calls if call[0] == "PUT"]


def events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def renew_until_the_credential_is_due(lease: lease_renewal.Renewer, clock: Clock) -> None:
    while clock.now < 100.0 + lease_renewal.CREDENTIAL_SECONDS:
        lease.tick()
        clock.advance(lease_renewal.INTERVAL_SECONDS)
    assert [lease.lost, clock.now] == [None, 100.0 + lease_renewal.CREDENTIAL_SECONDS]


def test_the_interval_sits_well_inside_the_admission_lease():
    assert (lease_renewal.INTERVAL_SECONDS, lease_renewal.RETRY_SECONDS, lease_renewal.CREDENTIAL_SECONDS) == (
        15.0,
        2.0,
        120.0,
    )


def test_the_first_tick_renews_at_once_and_then_on_the_fixed_interval(tmp_path):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)

    lease.tick()
    clock.advance(lease_renewal.INTERVAL_SECONDS - 0.1)
    lease.tick()
    assert len(heartbeats(controller)) == 1
    clock.advance(0.1)
    lease.tick()

    assert len(heartbeats(controller)) == 2
    assert lease.lost is None


def test_a_worker_keeps_its_lease_past_five_minutes(tmp_path):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)

    for _ in range(int(360 / lease_renewal.INTERVAL_SECONDS) + 1):
        lease.tick()
        clock.advance(lease_renewal.INTERVAL_SECONDS)

    assert lease.lost is None
    assert len(heartbeats(controller)) == int(360 / lease_renewal.INTERVAL_SECONDS) + 1


def test_the_renewal_carries_the_admitted_execution_and_task_generation_in_a_contract_body(tmp_path):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)

    lease.tick()
    clock.advance(lease_renewal.INTERVAL_SECONDS)
    lease.tick()

    first, second = heartbeats(controller)
    method, url, grant, body = first
    assert (method, url, grant) == ("PUT", f"{CONTROL}/v2/executions/{EXECUTION}/heartbeat", GRANT)
    assert body["authority"] == {
        "execution_id": EXECUTION,
        "task_id": "t1",
        "task_generation": 4,
        "controller_epoch": 7,
        "owner_identity": "engineer@fixture-1",
    }
    assert body["state"] == "working"
    assert contracts.check(contracts.load(), "heartbeat", body) is None
    assert second[3]["authority"] == body["authority"]
    assert second[3]["renewal_sequence"] > body["renewal_sequence"]
    assert second[3]["operation_id"] != body["operation_id"]


def test_the_renewal_sequence_rises_even_when_the_wall_clock_stands_still(tmp_path):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)

    lease.tick()
    clock.now += lease_renewal.INTERVAL_SECONDS
    lease.tick()

    first, second = (call[3]["renewal_sequence"] for call in heartbeats(controller))
    assert first == int(WALL * 1000)
    assert second == first + 1


def test_the_first_renewal_sequence_is_one_at_the_wall_clock_origin(tmp_path):
    clock = Clock()
    clock.wall = 0.0
    controller = Controller(clock, (200, acknowledged(clock)))
    lease = lease_renewal.Renewer(
        lease_renewal.Lease(tmp_path, CONTROL, {**ADMISSION, "lease_deadline_ms": 60_000}, GRANT),
        controller,
        clock.monotonic,
        clock.time,
    )

    lease.tick()

    assert heartbeats(controller)[0][3]["renewal_sequence"] == 1
    assert heartbeats(controller)[0][3]["observed_at"] == "1970-01-01T00:00:00Z"


def test_a_renewal_names_the_wall_clock_time_it_was_observed(tmp_path):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)

    lease.tick()

    assert heartbeats(controller)[0][3]["observed_at"] == "2027-01-15T08:00:00Z"


def test_a_successful_renewal_logs_its_sequence_and_deadline(tmp_path, capsys):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)

    lease.tick()

    assert events(capsys) == [
        {
            "lease": "renewed",
            "renewal_sequence": int(WALL * 1000),
            "lease_deadline_ms": int(WALL * 1000) + 60_000,
            "execution_id": EXECUTION,
            "task_generation": 4,
        }
    ]


def test_every_lease_event_is_flushed_at_once(tmp_path, monkeypatch):
    printed = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(kwargs))
    clock = Clock()
    lease = renewer(tmp_path, clock, Controller(clock))

    lease.tick()

    assert printed == [{"flush": True}]


def test_a_control_url_is_trimmed_of_its_trailing_slash_only():
    lease = lease_renewal.Lease(Path("/attempt"), "http://controller.invalid/apiX/", ADMISSION, GRANT)

    assert lease_renewal.Renewer(lease).url == "http://controller.invalid/apiX"


def test_a_failed_renewal_is_logged_and_retried_on_the_short_interval(tmp_path, capsys):
    clock = Clock()
    controller = Controller(clock, (503, {}))
    lease = renewer(tmp_path, clock, controller)

    lease.tick()
    clock.advance(lease_renewal.RETRY_SECONDS - 0.1)
    lease.tick()
    assert len(heartbeats(controller)) == 1
    clock.advance(0.1)
    lease.tick()

    assert len(heartbeats(controller)) == 2
    assert lease.lost is None
    failed, renewed = events(capsys)
    assert failed == {
        "lease": "renewal_failed",
        "call": "heartbeat",
        "status": 503,
        "execution_id": EXECUTION,
        "task_generation": 4,
        "retry_seconds": lease_renewal.RETRY_SECONDS,
    }
    assert renewed["lease"] == "renewed"
    assert renewed["execution_id"] == EXECUTION
    assert renewed["task_generation"] == 4


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_a_refused_renewal_loses_the_lease_and_stops_every_further_call(tmp_path, capsys, status):
    clock = Clock()
    controller = Controller(clock, (status, {}))
    lease = renewer(tmp_path, clock, controller)

    lease.tick()
    clock.advance(lease_renewal.INTERVAL_SECONDS * 3)
    lease.tick()

    assert lease.lost == f"heartbeat_refused_{status}"
    assert len(controller.calls) == 1
    assert events(capsys) == [
        {"lease": "lost", "reason": f"heartbeat_refused_{status}", "execution_id": EXECUTION, "task_generation": 4}
    ]


def test_a_lease_that_lapses_through_failures_is_lost_without_another_call(tmp_path):
    clock = Clock()
    controller = Controller(clock, *[(503, {})] * 100)
    lease = renewer(tmp_path, clock, controller)

    while clock.now < 100.0 + 60 - lease_renewal.RETRY_SECONDS:
        lease.tick()
        clock.advance(lease_renewal.RETRY_SECONDS)
    calls = len(controller.calls)
    clock.advance(lease_renewal.RETRY_SECONDS)
    lease.tick()

    assert lease.lost == "lease_expired"
    assert len(controller.calls) == calls


def test_a_lease_deadline_comes_from_the_last_acknowledgement(tmp_path):
    clock = Clock()
    controller = Controller(clock, (200, acknowledged(clock)), *[(503, {})] * 100)
    lease = renewer(tmp_path, clock, controller)

    lease.tick()
    clock.advance(59.0)
    lease.tick()
    assert lease.lost is None
    clock.advance(1.0)
    lease.tick()

    assert lease.lost == "lease_expired"


def test_an_acknowledgement_for_another_task_generation_loses_the_lease(tmp_path):
    clock = Clock()
    controller = Controller(clock, (200, acknowledged(clock, task_generation=5)))
    lease = renewer(tmp_path, clock, controller)

    lease.tick()

    assert lease.lost == "generation_changed"


def test_the_renewal_adopts_the_new_controller_epoch_after_a_failover(tmp_path):
    clock = Clock()
    controller = Controller(clock, (200, acknowledged(clock, controller_epoch=8)))
    lease = renewer(tmp_path, clock, controller)

    lease.tick()
    adopted = lease.record()["controller_epoch"]
    clock.advance(lease_renewal.INTERVAL_SECONDS)
    lease.tick()

    first, second = heartbeats(controller)
    assert first[3]["authority"]["controller_epoch"] == 7
    assert second[3]["authority"]["controller_epoch"] == 8
    assert second[3]["authority"]["task_generation"] == 4
    assert adopted == 8
    assert lease.lost is None


def test_an_acknowledgement_without_a_deadline_is_retried(tmp_path):
    clock = Clock()
    controller = Controller(clock, (200, {}))
    lease = renewer(tmp_path, clock, controller)

    lease.tick()
    clock.advance(lease_renewal.RETRY_SECONDS)
    lease.tick()

    assert len(heartbeats(controller)) == 2
    assert lease.lost is None


def test_the_launch_grant_is_renewed_before_it_expires_and_used_from_then_on(tmp_path):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)

    renew_until_the_credential_is_due(lease, clock)
    lease.tick()

    credentials = [call for call in controller.calls if call[0] == "POST"]
    assert credentials == [("POST", f"{CONTROL}/v2/executions/{EXECUTION}/credential", GRANT, {})]
    assert controller.calls[-2][0] == "POST"
    assert controller.calls[-1][2] == "sv2.grant-renewed"
    assert grant_path(tmp_path / EXECUTION).read_text() == "sv2.grant-renewed"


def test_a_renewed_grant_is_logged_and_not_renewed_again_until_its_interval(tmp_path, capsys):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)
    renew_until_the_credential_is_due(lease, clock)
    capsys.readouterr()

    lease.tick()
    clock.advance(lease_renewal.INTERVAL_SECONDS)
    lease.tick()

    assert [call[0] for call in controller.calls[-3:]] == ["POST", "PUT", "PUT"]
    assert events(capsys)[0] == {
        "lease": "credential_renewed",
        "expires_at": "x",
        "execution_id": EXECUTION,
        "task_generation": 4,
    }


@pytest.mark.parametrize("answer", [(503, {}), (200, {}), (200, {"credential": ""}), (200, [])])
def test_a_failed_credential_renewal_is_retried_and_keeps_the_grant(tmp_path, answer):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)
    renew_until_the_credential_is_due(lease, clock)
    controller.answers.append(answer)

    lease.tick()
    clock.advance(lease_renewal.RETRY_SECONDS)
    lease.tick()

    posts = [call for call in controller.calls if call[0] == "POST"]
    assert len(posts) == 2
    assert lease.lost is None
    assert grant_path(tmp_path / EXECUTION).read_text() == "sv2.grant-renewed"


def test_a_refused_credential_renewal_loses_the_lease(tmp_path):
    clock = Clock()
    controller = Controller(clock)
    lease = renewer(tmp_path, clock, controller)
    renew_until_the_credential_is_due(lease, clock)
    controller.answers.append((401, {}))

    lease.tick()

    assert lease.lost == "credential_refused_401"
    assert grant_path(tmp_path / EXECUTION).read_text() == GRANT


def test_an_attempt_without_an_admission_has_no_lease_to_renew(tmp_path):
    clock = Clock()
    attempt = attempt_folder(tmp_path, admission=None)

    controller = Controller(clock)
    unleased = lease_renewal.load(attempt, controller, clock.monotonic, clock.time)

    unleased.tick()
    assert isinstance(unleased, lease_renewal.Unleased)
    assert (unleased.lost, controller.calls) == (None, [])


def test_a_renewed_grant_waits_in_memory_until_the_run_folder_exists(tmp_path):
    clock = Clock()
    controller = Controller(clock)
    attempt = tmp_path / EXECUTION
    lease = lease_renewal.Lease(attempt, CONTROL, ADMISSION, GRANT)
    renewer = lease_renewal.Renewer(lease, controller, clock.monotonic, clock.time)

    renew_until_the_credential_is_due(renewer, clock)
    renewer.tick()

    assert renewer.grant == "sv2.grant-renewed"
    assert not attempt.exists()
    assert renewer.record() == {
        "execution_id": EXECUTION,
        "task_id": "t1",
        "task_generation": 4,
        "controller_epoch": 7,
        "owner_identity": "engineer@fixture-1",
        "lease_deadline_ms": int(clock.wall * 1000) + 60_000,
    }


class Ticking:
    def __init__(self, lose_at: int = 0) -> None:
        self.lost, self.ticks, self.lose_at = None, 0, lose_at

    def lose(self, reason: str) -> None:
        self.lost = reason

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks == self.lose_at:
            self.lost = "lease_expired"


def eventually(condition) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline
        time.sleep(0.001)


def test_a_kept_lease_ticks_until_the_block_ends():
    renewer = Ticking()

    with lease_renewal.kept(renewer, pause=0.001) as held:
        assert held is renewer
        eventually(lambda: renewer.ticks >= 3)
    ticks = renewer.ticks
    time.sleep(0.01)

    assert renewer.ticks == ticks


def test_a_kept_lease_stops_ticking_once_it_is_lost():
    renewer = Ticking(lose_at=2)

    with lease_renewal.kept(renewer, pause=0.001):
        eventually(lambda: renewer.lost is not None)
        time.sleep(0.01)

    assert renewer.ticks == 2


def test_a_kept_lease_waits_its_pause_between_ticks():
    renewer = Ticking()

    with lease_renewal.kept(renewer, pause=60):
        eventually(lambda: renewer.ticks == 1)
        time.sleep(0.01)

    assert renewer.ticks == 1
    assert lease_renewal.PAUSE_SECONDS == 1.0


class Crashing(Ticking):
    def tick(self) -> None:
        super().tick()
        raise OSError("disk full")


def test_a_renewal_that_crashes_loses_the_lease_instead_of_dying_silently():
    renewer = Crashing()

    with lease_renewal.kept(renewer, pause=0.001):
        eventually(lambda: renewer.lost is not None)
        time.sleep(0.01)

    assert renewer.lost == "renewal_crashed_OSError: disk full"
    assert renewer.ticks == 1


def test_a_crashed_renewal_is_logged_as_a_lost_lease(tmp_path, capsys):
    def crashing(*args):
        raise OSError("disk full")

    renewer = lease_renewal.load(attempt_folder(tmp_path), crashing)

    with lease_renewal.kept(renewer, pause=0.001):
        eventually(lambda: renewer.lost is not None)

    assert events(capsys) == [
        {
            "lease": "lost",
            "reason": "renewal_crashed_OSError: disk full",
            "execution_id": EXECUTION,
            "task_generation": 4,
        }
    ]


class Answer(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_send_puts_json_with_the_grant_as_bearer(monkeypatch):
    seen = []

    def opened(request, timeout):
        seen.append((request, timeout))
        return Answer(b'{"ok": 1}')

    monkeypatch.setattr(urllib.request, "urlopen", opened)

    assert lease_renewal.send("PUT", CONTROL + "/v2/x", GRANT, {"a": 1}) == (200, {"ok": 1})
    request, timeout = seen[0]
    assert request.get_method() == "PUT"
    assert request.full_url == CONTROL + "/v2/x"
    assert request.get_header("Authorization") == f"Bearer {GRANT}"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"a": 1}
    assert timeout == lease_renewal.TIMEOUT_SECONDS


def test_send_reports_refusals_and_unreachable_controllers(monkeypatch):
    def refused(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 409, "conflict", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert lease_renewal.send("PUT", CONTROL, GRANT, {}) == (409, {})

    def unreachable(request, timeout):
        raise OSError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    assert lease_renewal.send("PUT", CONTROL, GRANT, {}) == (lease_renewal.UNREACHABLE, {})

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Answer(b"not json"))
    assert lease_renewal.send("PUT", CONTROL, GRANT, {}) == (200, {})
