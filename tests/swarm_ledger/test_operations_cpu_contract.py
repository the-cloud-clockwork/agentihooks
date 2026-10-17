from tests.swarm_ledger import operations_cpu_replay as replay


def small_corpus():
    return {
        "state": {
            "title": "Replay",
            "overview": "Recorded operations replay " * 40,
            "size": "small",
            "sources": [],
            "orchestrator": "",
            "phases": [],
            "tasks": [],
            "followups": [],
            "questions": [],
            "chat": [],
            "notifications": [],
            "priorities": [],
            "alerts": [],
            "artifacts": [],
            "artifact_trash": [],
            "notes": [],
            "freezes": [],
            "_meta": {"rev": 1, "members": {}, "stamps": {}, "events": []},
        },
        "ops": [
            {"op": "add", "id": "accepted", "thread": "chat", "text": "Recorded message"},
            {"op": "edit", "id": "missing", "thread": "chat", "text": "Missing message"},
        ],
        "source": {"requests": 2},
    }


def test_small_replay_keeps_acceptance_refusal_and_reports_real_phases(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "corpus", small_corpus)
    with monkeypatch.context() as isolated:
        measured = replay.replay(tmp_path / "measured", isolated)
    with monkeypatch.context() as isolated:
        profiled = replay.replay(tmp_path / "profiled", isolated, profiled=True)
    assert measured["outputs"] == profiled["outputs"]
    assert measured["requests"] == 2
    assert measured["outputs"][0]["reply"]["applied"] == ["accepted"]
    assert measured["outputs"][1]["reply"]["rejected"] == ["missing"]
    assert measured["outputs"][0]["stream"] != replay.fingerprint([])
    assert measured["outputs"][0]["relays"]["relay_to_inbox"]
    assert set(measured["phase_cpu_seconds_per_request"]) == {
        "request_read",
        "current_snapshot",
        "storage_write",
        "stream_publish",
        "write_and_publish",
    }
    assert measured["outputs"][1]["relay_outcomes"]["items"]
    assert measured["outputs"][1]["relay_outcomes"]["receipts"]
    assert measured["outputs"][1]["relay_outcomes"]["unrelayed"] == {}
    assert measured["profile"] == ""
    assert "function calls" in profiled["profile"]
