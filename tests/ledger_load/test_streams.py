import json
from pathlib import Path

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
