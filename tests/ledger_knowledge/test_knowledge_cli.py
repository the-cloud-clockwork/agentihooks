import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import cli_delegates
from scripts.ledger_knowledge import cli


def ledger() -> dict:
    return {
        "title": "Ledger é",
        "phases": [{"id": "p1", "title": "Phase"}],
        "tasks": [{"id": "t1", "title": "Index", "phase": "p1"}],
    }


class Reader:
    def __init__(self, document: dict | None = None) -> None:
        self.document = document or ledger()
        self.slugs: list[str] = []

    def __call__(self, slug: str) -> dict:
        self.slugs.append(slug)
        return self.document


def run(argv: list[str], environ: dict[str, str], reader: Reader) -> int:
    return cli.main(argv, environ, reader, lambda: 1.5)


def test_knowledge_is_a_delegated_command() -> None:
    assert cli_delegates.DELEGATED_CLIS["knowledge"] == "scripts.ledger_knowledge.cli"
    assert cli_delegates.delegated_cli(["knowledge", "ask"]) is cli.main


def test_ask_prints_the_answer_for_the_named_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reader = Reader()
    assert run(["ask", "index", "--ledger", "rig", "--limit", "1"], {"AGENTIHOOKS_HOME": str(tmp_path)}, reader) == 0
    output = capsys.readouterr().out
    answer = json.loads(output)
    assert output == json.dumps(answer, ensure_ascii=False, indent=1) + "\n"
    assert reader.slugs == ["rig"]
    assert answer["ledger_slug"] == "rig"
    assert answer["ledger"]["title"] == "Ledger é"
    assert answer["refreshed_at"] == 1500
    assert [(hit["ref"], hit["parents"][0]["ref"]) for hit in answer["hits"]] == [("tasks/t1", "phases/p1")]
    assert (tmp_path / "knowledge" / "rig.sqlite3").is_file()


def test_ask_defaults_to_the_swarm_ledger_and_five_hits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reader = Reader({"notes": [{"id": f"n{i}", "text": "shared"} for i in range(7)]})
    environ = {"AGENTIHOOKS_HOME": str(tmp_path), "AGENTIHOOKS_SWARM": "own"}
    assert run(["ask", "shared"], environ, reader) == 0
    answer = json.loads(capsys.readouterr().out)
    assert (reader.slugs, answer["ledger_slug"], len(answer["hits"])) == (["own"], "own", 5)


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main([], {}, Reader(), lambda: 0.0)
    assert "usage: agentihooks knowledge " in capsys.readouterr().err


def test_refresh_prints_its_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["refresh", "--ledger", "rig"], {"AGENTIHOOKS_HOME": str(tmp_path)}, Reader()) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ledger_slug": "rig",
        "added": 3,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
    }


def test_a_missing_ledger_name_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reader = Reader()
    assert run(["ask", "q"], {"AGENTIHOOKS_HOME": str(tmp_path)}, reader) == 2
    captured = capsys.readouterr()
    assert (captured.out, captured.err, reader.slugs) == ("", "knowledge: name a ledger with --ledger\n", [])


def test_read_ledger_reads_through_the_shared_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm_ledger import storage

    asked: list[str] = []
    repository = SimpleNamespace(get_document=lambda slug: asked.append(slug) or {"title": slug})
    monkeypatch.setattr(storage, "default", lambda: repository)
    assert cli.read_ledger("rig") == {"title": "rig"}
    assert asked == ["rig"]
