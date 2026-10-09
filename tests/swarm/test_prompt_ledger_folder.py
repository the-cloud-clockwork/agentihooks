import pytest

from scripts.swarm import prompt


@pytest.mark.parametrize("lane", ["eng", "ci"])
@pytest.mark.parametrize("kind", ["code", "ci", "ops", "tune", "troubleshoot", "research"])
def test_worker_prompt_reads_the_configured_ledger_folder(monkeypatch, tmp_path, lane, kind):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    text = prompt.build("sw", "/repo", lane, "worker", {"id": "t1", "title": "Task", "kind": kind})

    assert f"read the ledger {tmp_path}/sw.json in full" in text
    assert "~/development-ledger/" not in text
    assert text.index("worker join") < text.index(f"read the ledger {tmp_path}/sw.json in full")
    assert (
        f"Then read the ledger {tmp_path}/sw.json in full: every task and its state, the operator's notes, "
        "answers and comments. It is your starting point; take only your own task." in text
    )


@pytest.mark.parametrize("lane", ["eng", "ci", "master"])
@pytest.mark.parametrize("configured", [None, ""])
def test_prompt_keeps_the_default_ledger_path(monkeypatch, lane, configured):
    if configured is None:
        monkeypatch.delenv("LEDGER_DIR", raising=False)
    else:
        monkeypatch.setenv("LEDGER_DIR", configured)
    text = prompt.build("sw", "/repo", lane, "agent", {"id": "t1", "title": "Task"})

    assert "read the ledger ~/development-ledger/sw.json in full" in text


@pytest.mark.parametrize("configured", ["absolute", "home"])
def test_master_prompt_and_summary_use_the_same_configured_folder(monkeypatch, tmp_path, configured):
    directory = tmp_path / "second-ledger"
    directory.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LEDGER_DIR", str(directory) if configured == "absolute" else "~/second-ledger")
    (directory / "sw.json").write_text('{"overview": "Plan\\n\\nSummary\\nStopped after the proof"}')
    text = prompt.build("sw", "/repo", "master", "master", {"id": "master", "title": "Master"})

    assert f"read the ledger {directory}/sw.json in full" in text
    assert "~/development-ledger/" not in text
    assert "Stopped after the proof" in text
    assert text.index("Stopped after the proof") < text.index(f"{directory}/sw.json")
