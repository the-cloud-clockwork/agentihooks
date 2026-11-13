"""The swarm reads the CI lab capacity into its lab block, its status and the lab_capacity metrics table."""

import json
import threading
from typing import Any

import pytest

from scripts.ci_lab import metrics as lab_metrics
from scripts.ci_lab.capacity import Load
from scripts.swarm import cli, lab_capacity, status
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_cli import env as env

NOW = 1_800_000_000_000
LOAD = Load(
    pending={"queue": (30.0, 30.0)},
    running={"queue": 1},
    slots_total=4,
    slots_busy=1,
    budget_fraction=0.5,
    shed=("backfill",),
)
DOC = {
    "classes": {
        "queue": {"pending": 2, "running": 1, "predicted_wait_s": 20.0},
        "hotfix": {"pending": 1, "running": 0, "predicted_wait_s": None},
    },
    "slots": {"total": 4, "busy": 4, "free": 0},
    "utilisation": 1.0,
    "budget_fraction": 0.5,
    "shed": ["hotfix"],
}


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def lab() -> Any:
    server = lab_metrics.serve("127.0.0.1", 0, lambda: LOAD, lab_metrics.Registry())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


class Reply:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "Reply":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_read_returns_the_capacity_document_the_lab_serves(lab: str) -> None:
    doc = lab_capacity.read(lab)
    assert doc["classes"]["queue"] == {"pending": 2, "running": 1, "predicted_wait_s": 20.0}
    assert (doc["slots"], doc["utilisation"], doc["budget_fraction"], doc["shed"]) == (
        {"total": 4, "busy": 1, "free": 3},
        0.25,
        0.5,
        ["backfill"],
    )


def test_read_asks_the_capacity_route_within_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, float]] = []

    def answer(url: str, timeout: float) -> Reply:
        asked.append((url, timeout))
        return Reply(json.dumps(DOC).encode())

    monkeypatch.setattr(lab_capacity, "urlopen", answer)
    assert lab_capacity.read("http://lab:8800/") == DOC
    assert lab_capacity.read("http://lab-X") == DOC
    assert asked == [("http://lab:8800/capacity", 3), ("http://lab-X/capacity", 3)]


@pytest.mark.parametrize("body", [b"[]", b'{"slots": {}}', b'{"classes": []}'], ids=["list", "missing", "not-a-map"])
def test_read_refuses_a_document_without_classes(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    monkeypatch.setattr(lab_capacity, "urlopen", lambda url, timeout: Reply(body))
    with pytest.raises(ValueError) as refused:
        lab_capacity.read("http://lab:8800")
    assert str(refused.value) == "lab capacity document carries no classes"


def test_rows_carry_one_checked_row_per_class() -> None:
    found = lab_capacity.rows("sw", DOC, NOW)
    path = {"plan": "", "phase": "", "slice": "", "task": ""}
    shared = {"ledger": "sw", "ts_ms": NOW, **path, "slots_total": 4, "slots_busy": 4}
    assert found == [
        {
            "event_id": f"lab-capacity:sw:{NOW}:hotfix",
            **shared,
            "class": "hotfix",
            "pending": 1,
            "running": 0,
            "predicted_wait_s": -1.0,
            "shed": 1,
            "utilisation": 1.0,
            "budget_fraction": 0.5,
        },
        {
            "event_id": f"lab-capacity:sw:{NOW}:queue",
            **shared,
            "class": "queue",
            "pending": 2,
            "running": 1,
            "predicted_wait_s": 20.0,
            "shed": 0,
            "utilisation": 1.0,
            "budget_fraction": 0.5,
        },
    ]
    for row in found:
        lab_capacity.TABLE.check(row)
    assert lab_capacity.TABLE.name == "lab_capacity"


def test_status_is_none_before_any_read(store: RedisStore) -> None:
    assert lab_capacity.status(store, "sw") is None


def test_a_config_stored_before_the_lab_url_field_reads_no_lab(store: RedisStore) -> None:
    store.create(SwarmConfig("sw", "/repo", 1, 1))
    store.redis.hdel(store.key("sw", "config"), "lab_url")
    assert store.config("sw").lab_url == ""


def test_capture_without_a_lab_url_reads_nothing(store: RedisStore) -> None:
    assert lab_capacity.capture(store, "sw", "", NOW) == []
    assert store.redis.exists(store.key("sw", "lab-capacity")) == 0


def test_capture_keeps_the_capacity_in_the_lab_block_and_returns_its_rows(store: RedisStore, lab: str) -> None:
    found = lab_capacity.capture(store, "sw", lab, NOW)
    assert [row["class"] for row in found] == sorted(lab_capacity.read(lab)["classes"])
    block = json.loads(store.redis.get(store.key("sw", "lab-capacity")))
    assert block == {"url": lab, "at": NOW, "capacity": lab_capacity.read(lab), "error": "", "error_at": 0}


def test_a_failed_capture_keeps_the_last_capacity_and_records_the_error(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab_capacity, "urlopen", lambda url, timeout: Reply(json.dumps(DOC).encode()))
    lab_capacity.capture(store, "sw", "http://lab:8800", NOW)
    monkeypatch.setattr(lab_capacity, "urlopen", lambda url, timeout: Reply(b"[]"))
    assert lab_capacity.capture(store, "sw", "http://lab:8800", NOW + 60_000) == []
    assert lab_capacity.status(store, "sw") == {
        "url": "http://lab:8800",
        "at": NOW,
        "capacity": DOC,
        "error": "lab capacity document carries no classes",
        "error_at": NOW + 60_000,
    }


def test_a_first_failed_capture_records_only_the_error(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(url: str, timeout: float) -> Reply:
        raise OSError("connection refused")

    monkeypatch.setattr(lab_capacity, "urlopen", refuse)
    assert lab_capacity.capture(store, "sw", "http://lab:8800", NOW) == []
    assert lab_capacity.status(store, "sw") == {
        "url": "http://lab:8800",
        "error": "connection refused",
        "error_at": NOW,
    }


def test_a_failed_capture_of_a_new_lab_drops_the_old_labs_capacity(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab_capacity, "urlopen", lambda url, timeout: Reply(json.dumps(DOC).encode()))
    lab_capacity.capture(store, "sw", "http://old-lab:8800", NOW)
    monkeypatch.setattr(lab_capacity, "urlopen", lambda url, timeout: Reply(b"[]"))
    assert lab_capacity.capture(store, "sw", "http://new-lab:8800", NOW + 60_000) == []
    assert lab_capacity.status(store, "sw") == {
        "url": "http://new-lab:8800",
        "error": "lab capacity document carries no classes",
        "error_at": NOW + 60_000,
    }


def test_status_json_carries_the_lab_block_of_the_configured_lab(env: Any, lab: str) -> None:
    store, _, _ = env
    cli.main(["sw", "create", "--repo", "/repo"])
    assert status.status_report(store, "sw", {"tasks": []})["lab"] is None
    cli.main(["sw", "set", f"lab-url={lab}"])
    assert store.config("sw").lab_url == lab
    lab_capacity.capture(store, "sw", store.config("sw").lab_url, NOW)
    report = status.status_report(store, "sw", {"tasks": []})
    assert report["lab"]["capacity"]["slots"] == {"total": 4, "busy": 1, "free": 3}
    assert report["config"]["lab_url"] == lab


def test_set_lab_url_refuses_an_address_that_is_not_http(env: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["sw", "create", "--repo", "/repo"])
    capsys.readouterr()
    assert cli.main(["sw", "set", "lab-url=ftp://lab"]) == 1
    assert (
        capsys.readouterr().err.strip()
        == "swarm: lab-url takes an http or https address with a host and no credentials"
    )
    assert env[0].config("sw").lab_url == ""
