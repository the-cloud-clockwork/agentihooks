import json
import re
import sqlite3
from pathlib import Path

import pytest

from scripts.recall.cli import main
from scripts.recall.reindex import binned, deleted_refs, ledger_dir
from scripts.recall.store import SQLiteRecallStore


def document(title, tasks=(), chat=()):
    return {"title": title, "overview": "", "tasks": list(tasks), "chat": list(chat), "_meta": {"events": []}}


@pytest.fixture
def home(tmp_path):
    ledgers = tmp_path / "ledgers"
    ledgers.mkdir()
    agentihooks = tmp_path / "agentihooks"
    (agentihooks / "swarm" / "alpha").mkdir(parents=True)
    write(ledgers, "alpha", document("Alpha", [{"id": "t1", "title": "alpha-task words"}]))
    write(ledgers, "beta", document("Beta", [{"id": "t1", "title": "beta words"}]))
    (ledgers / ".bin.json").write_text(json.dumps({"beta": 1}))
    return {"LEDGER_DIR": str(ledgers), "AGENTIHOOKS_HOME": str(agentihooks)}


def write(folder, slug, doc):
    (folder / f"{slug}.json").write_text(json.dumps(doc))


def run(environ, *argv, capsys):
    code = main(["reindex", *argv], environ)
    return code, json.loads(capsys.readouterr().out)


def store(environ):
    return SQLiteRecallStore(f"{environ['AGENTIHOOKS_HOME']}/recall/recall.sqlite3")


def test_ledger_dir_follows_the_ledger_server_setting(tmp_path):
    assert ledger_dir({"LEDGER_DIR": str(tmp_path)}) == tmp_path
    assert ledger_dir({"HOME": str(tmp_path)}) == Path.home() / "development-ledger"
    assert ledger_dir({"LEDGER_DIR": "~/ledgers"}) == Path.home() / "ledgers"


def test_the_bin_holds_only_slugs_with_a_binned_time(tmp_path):
    assert binned(tmp_path) == set()
    (tmp_path / ".bin.json").write_text("{")
    assert binned(tmp_path) == set()
    (tmp_path / ".bin.json").write_text("[1]")
    assert binned(tmp_path) == set()
    (tmp_path / ".bin.json").write_text(json.dumps({"kept": 5, "odd": "x"}))
    assert binned(tmp_path) == {"kept"}


def test_all_backfills_every_ledger_and_skips_the_bin(home, capsys):
    code, out = run(home, "--all", capsys=capsys)
    assert code == 0
    assert out == {
        "indexed": {"alpha": {"written": 2, "unchanged": 0, "archived": 0, "removed": 0}},
        "skipped_binned": ["beta"],
        "unreadable": [],
    }
    assert store(home).match('"alpha-task"') == ["alpha/tasks/t1#0"]
    assert store(home).match("beta") == []


def test_include_binned_indexes_the_bin_too(home, capsys):
    code, out = run(home, "--all", "--include-binned", capsys=capsys)
    assert code == 0
    assert sorted(out["indexed"]) == ["alpha", "beta"]
    assert out["skipped_binned"] == []
    assert store(home).match("beta") == ["beta/ledger#0", "beta/tasks/t1#0"]


def test_one_ledger_by_slug(home, capsys):
    code, out = run(home, "--ledger", "alpha", capsys=capsys)
    assert code == 0
    assert list(out["indexed"]) == ["alpha"]
    _, again = run(home, "--ledger", "alpha", capsys=capsys)
    assert again["indexed"]["alpha"] == {"written": 0, "unchanged": 2, "archived": 0, "removed": 0}


def test_a_binned_slug_needs_include_binned(home, capsys):
    _, out = run(home, "--ledger", "beta", capsys=capsys)
    assert out["indexed"] == {} and out["skipped_binned"] == ["beta"]
    _, out = run(home, "--ledger", "beta", "--include-binned", capsys=capsys)
    assert list(out["indexed"]) == ["beta"]


def test_an_unknown_slug_fails(home, capsys):
    code, out = run(home, "--ledger", "nope", capsys=capsys)
    assert code == 1
    assert out == {"error": "no ledger file for nope"}


def test_an_unreadable_ledger_is_reported_and_the_rest_indexed(home, capsys):
    (Path(home["LEDGER_DIR"]) / "broken.json").write_text("{")
    code, out = run(home, "--all", capsys=capsys)
    assert code == 0
    assert out["unreadable"] == ["broken"]
    assert list(out["indexed"]) == ["alpha"]


def test_a_malformed_ledger_is_unreadable_and_the_rest_indexed(home, capsys):
    folder = Path(home["LEDGER_DIR"])
    (folder / "listed.json").write_text("[]")
    write(folder, "idless", document("Idless", [{"title": "no id"}]))
    write(folder, "nameless", document("Nameless", [{"id": "t1", "comments": [{"text": "no id"}]}]))
    code, out = run(home, "--all", capsys=capsys)
    assert code == 0
    assert out["unreadable"] == ["idless", "listed", "nameless"]
    assert list(out["indexed"]) == ["alpha"]
    assert store(home).match("Idless") == []


def test_the_swarm_slug_comes_from_the_swarm_folder(home, capsys):
    run(home, "--all", "--include-binned", capsys=capsys)
    with sqlite3.connect(store(home).path) as connection:
        found = dict(connection.execute("SELECT ledger_slug, swarm_slug FROM records"))
    assert found == {"alpha": "alpha", "beta": ""}


def test_reindex_archives_retention_drops_and_removes_deleted_entries(home, capsys):
    folder = Path(home["LEDGER_DIR"])
    chat = [{"id": "m1", "by": "operator", "at": 1, "text": "ancient chat"}]
    tasks = [
        {"id": "t1", "title": "kept"},
        {"id": "t2", "title": "doomed", "comments": [{"id": "c1", "by": "a", "at": 1, "text": "doomed child"}]},
        {"id": "t3", "title": "living", "comments": [{"id": "c2", "by": "a", "at": 1, "text": "struck comment"}]},
    ]
    write(folder, "alpha", document("Alpha", tasks, chat))
    run(home, "--ledger", "alpha", capsys=capsys)
    assert store(home).match("doomed") == ["alpha/tasks/t2#0", "alpha/tasks/t2/comments/c1#0"]

    tasks[1]["deleted"] = True
    tasks[2]["comments"][0]["deleted"] = True
    write(folder, "alpha", document("Alpha", tasks, []))
    _, out = run(home, "--ledger", "alpha", capsys=capsys)
    assert out["indexed"]["alpha"] == {"written": 0, "unchanged": 3, "archived": 1, "removed": 3}
    assert store(home).match("doomed") == []
    assert store(home).match("struck") == []
    assert store(home).match("ancient") == ["alpha/chat/m1#0"]


def test_deleted_refs_walks_items_and_their_threads():
    doc = {
        "phases": [{"id": "p1", "deleted": True}],
        "questions": [{"id": "q1", "answers": [{"id": "a1", "comments": [{"id": "c1", "deleted": True}]}]}],
        "followups": [{"id": "f1", "comments": [{"id": "c2", "deleted": True}, {"id": "c3"}]}],
        "artifacts": [{"id": "r1", "deleted": True}],
        "notes": [{"id": "n1"}],
    }
    assert deleted_refs(doc) == [
        "phases/p1",
        "questions/q1/answers/a1/comments/c1",
        "followups/f1/comments/c2",
        "artifacts/r1",
    ]


def test_agentihooks_dispatches_recall(monkeypatch):
    import scripts.install as install
    from scripts.recall import cli

    seen = []
    monkeypatch.setattr(cli, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["agentihooks", "recall", "reindex", "--all"])
    with pytest.raises(SystemExit) as exc:
        install.main()
    assert (exc.value.code, seen) == (0, [["reindex", "--all"]])


def test_agentihooks_help_lists_recall(monkeypatch, capsys):
    import scripts.install as install

    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr("sys.argv", ["agentihooks", "--help"])
    with pytest.raises(SystemExit):
        install.main()
    assert re.search(r"\n +recall +Recall archive of ledgers and swarms: reindex, eval\n", capsys.readouterr().out)


def test_help_names_the_command_and_its_options(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "200")
    with pytest.raises(SystemExit):
        main(["--help"], {})
    top = capsys.readouterr().out
    assert top.startswith("usage: agentihooks recall [-h] {reindex,eval} ...\n")
    assert "\nRecall archive of ledgers and swarms\n" in top
    assert re.search(r"\n +reindex +Backfill the recall archive from ledger files\n", top)
    assert re.search(r"\n +eval +Print the top five hit rate of a golden question file\n", top)
    with pytest.raises(SystemExit):
        main(["eval", "--help"], {})
    grade = capsys.readouterr().out
    assert grade.startswith("usage: agentihooks recall eval [-h] golden\n")
    assert re.search(r"\n  golden +JSON list of question, expect and optional scope and kinds\n", grade)
    with pytest.raises(SystemExit):
        main(["reindex", "--help"], {})
    sub = capsys.readouterr().out
    assert sub.startswith("usage: agentihooks recall reindex [-h] (--ledger SLUG | --all) [--include-binned]\n")
    assert re.search(r"\n  --ledger SLUG +Index one ledger\n", sub)
    assert re.search(r"\n  --all +Index every ledger file\n", sub)
    assert re.search(r"\n  --include-binned +Also index ledgers in the bin\n", sub)


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        main([], {})
    assert exc.value.code == 2
    assert "the following arguments are required: command" in capsys.readouterr().err


def test_reindex_requires_a_target(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["reindex"], {})
    assert exc.value.code == 2
    assert " one of the arguments --ledger --all is required" in capsys.readouterr().err
