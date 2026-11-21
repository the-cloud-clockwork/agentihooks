import importlib
import json
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from scripts.scheduler import service
from tests.ledger_load import streams


def test_both_stream_counts_use_identical_full_size_input_and_server_workloads(monkeypatch, tmp_path):
    doc = {"tasks": [{"id": "t0", "text": "same fixture"}]}
    monkeypatch.setattr(streams.gate, "full_size", lambda at: doc)
    seen = []

    def measure(source, folder, fixture, count):
        seen.append((source, fixture, count))
        return {"streams": count, "writes": 60, "cpu_cores": 0.5 if source == Path("base") else 0.2}

    monkeypatch.setattr(streams, "measure", measure)
    result = streams.compare(Path("base"), Path("head"), tmp_path)
    assert seen == [(Path("base"), doc, 1), (Path("head"), doc, 1), (Path("base"), doc, 3), (Path("head"), doc, 3)]
    assert [case["before"]["streams"] for case in result["cases"]] == [1, 3]
    assert [case["after"]["writes"] for case in result["cases"]] == [60, 60]
    assert result["ledger_bytes"] == len(json.dumps(doc, sort_keys=True).encode())


@pytest.mark.parametrize("over, verdict", [(-0.01, 0), (0.01, 1)])
def test_the_verdict_fails_a_head_that_delivers_past_the_follow_interval_and_keeps_both_measurements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, over: float, verdict: int
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    bound = streams.DELIVERY_BOUND_S
    result = {
        "cases": [
            {"before": {"delivery_max_s": bound + 1}, "after": {"delivery_max_s": bound / 2}},
            {"before": {"delivery_max_s": bound / 2}, "after": {"delivery_max_s": bound + over}},
        ]
    }
    monkeypatch.setattr(streams, "compare", lambda *args: result)
    assert streams.main(["--base", "base", "--head", "head", "--folder", str(tmp_path)]) == verdict
    assert json.loads((tmp_path / "streams.json").read_text()) == result


def test_stream_load_refuses_to_run_on_the_shared_machine(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(SystemExit) as error:
        streams.main(["--base", "base", "--head", "head", "--folder", str(tmp_path)])
    assert error.value.code == 2
    assert not (tmp_path / "streams.json").exists()


LAG_S = 0.8


class Lagging:
    """A front and read server pair whose streams count each write LAG_S seconds after it is acknowledged."""

    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    def consume(
        self, base: str, token: str, ready: threading.Event, stop: threading.Event, result: dict[str, Any]
    ) -> None:
        result["snapshots"] += 1
        self.results.append(result)
        ready.set()

    def mutate(self, slug: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
        for result in self.results:
            timer = threading.Timer(LAG_S, self.publish, args=(result,))
            timer.daemon = True
            timer.start()
        return {"data": {"rejected": []}}

    def publish(self, result: dict[str, Any]) -> None:
        result["patches"] += 1
        result["arrivals"].append(time.monotonic())


@contextmanager
def idle(*servers: object) -> Iterator[tuple[SimpleNamespace, SimpleNamespace]]:
    """Stand in for the ledger API service and the read server without starting either."""
    yield SimpleNamespace(pid=0), SimpleNamespace(pid=0)


def test_the_delivery_check_waits_for_a_patch_published_after_the_measurement_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = Lagging()
    monkeypatch.setattr(streams, "SECONDS", 1)
    monkeypatch.setattr(streams, "WRITES_PER_SECOND", 2)
    monkeypatch.setattr(streams, "serving", idle)
    monkeypatch.setattr(streams, "consume", server.consume)
    monkeypatch.setattr(streams.gate, "store", lambda folder, doc: "ledger-token")
    monkeypatch.setattr(streams.gate, "cpu_seconds", lambda pid: 0.0)
    monkeypatch.setattr(streams.gate, "Front", lambda write_base, read_base, credentials: server)

    result = streams.measure(Path("head"), tmp_path / "case", {"tasks": []}, 1)

    assert [consumer["patches"] for consumer in result["consumers"]] == [result["writes"]] == [2]
    assert result["last_write_s"] < streams.SECONDS < result["delivered_s"] < streams.SECONDS + LAG_S
    assert result["delivered_s"] - result["last_write_s"] < streams.FOLLOW_PASS_S
    assert LAG_S / 2 < result["delivery_p95_s"] <= result["delivery_max_s"] < 2 * LAG_S
    assert all("arrivals" not in consumer for consumer in result["consumers"])


def test_the_follow_pass_bound_follows_the_scheduler_pass_period() -> None:
    try:
        with patch.object(service, "PASS_S", 7):
            importlib.reload(streams)
            assert streams.FOLLOW_PASS_S == 7 + streams.FOLLOW_S + streams.POLL_S + streams.PUBLISH_MARGIN_S
    finally:
        importlib.reload(streams)
    assert streams.FOLLOW_PASS_S == service.PASS_S + streams.FOLLOW_S + streams.POLL_S + streams.PUBLISH_MARGIN_S


def listener(variable: str) -> str:
    """A server script that accepts connections on the port named by the environment variable."""
    return (
        "import os, socket\n"
        "listener = socket.socket()\n"
        f"listener.bind(('127.0.0.1', int(os.environ[{variable!r}])))\n"
        "listener.listen()\n"
        "while True:\n"
        "    connection, _ = listener.accept()\n"
        "    connection.close()\n"
    )


def test_the_stream_write_server_trusts_its_checkout_writer_but_refuses_an_unrelated_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    subprocess.run(["git", "init", "-q", str(stranger)], check=True)
    subprocess.run(
        ["git", "-C", str(stranger), "remote", "add", "origin", "https://github.com/stranger/project.git"],
        check=True,
    )
    (stranger / ".agentihooks" / "conditions").mkdir(parents=True)
    checkout = Path(streams.__file__).resolve().parents[2]
    writer = tmp_path / "writer.py"
    writer.write_text(
        "from hooks import config\n"
        "from hooks.context import profile_chain\n"
        "from hooks.filters.check import trusted_folder\n"
        "profile_chain.read_state = lambda: {}\n"
        "assert config.CONDITIONS_ENABLED\n"
        f"trusted_folder({str(checkout)!r})\n"
        "try:\n"
        f"    trusted_folder({str(stranger)!r})\n"
        "except ValueError as exc:\n"
        "    assert 'outside the operator' in str(exc)\n"
        "else:\n"
        "    raise AssertionError('unrelated writer was trusted')\n" + listener("LEDGER_API_PORT")
    )
    reader = tmp_path / "reader.py"
    reader.write_text(listener("LEDGER_PORT"))
    launch = subprocess.Popen

    def popen(argv: list[str], **options: Any) -> subprocess.Popen:
        return launch([sys.executable, str(writer if "-m" in argv else reader)], **options)

    monkeypatch.setattr(streams.subprocess, "Popen", popen)
    monkeypatch.setenv("PYTHONPATH", str(checkout))
    monkeypatch.setenv("CONDITIONS_ENABLED", "true")
    monkeypatch.setenv("CONDITIONS_TRUSTED_OWNERS", "")
    logs = tmp_path / "logs"
    logs.mkdir()
    with streams.serving(checkout, logs, streams.gate.free_port(), streams.gate.free_port()) as servers:
        assert [server.poll() for server in servers] == [None, None]
