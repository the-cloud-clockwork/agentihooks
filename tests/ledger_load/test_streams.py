import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

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


@pytest.mark.parametrize("after, verdict", [(0.2, 0), (0.5, 1), (0.8, 1)])
def test_cpu_gate_rejects_unchanged_or_increased_cpu_and_keeps_both_measurements(monkeypatch, tmp_path, after, verdict):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    result = {
        "cases": [
            {"before": {"cpu_cores": 0.5}, "after": {"cpu_cores": 0.2}},
            {"before": {"cpu_cores": 0.5}, "after": {"cpu_cores": after}},
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
