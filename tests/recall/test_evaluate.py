import json
import shutil
from pathlib import Path

import pytest

from scripts.recall.cli import main
from scripts.recall.evaluate import GoldenError, evaluate, load_golden
from scripts.recall.store import SQLiteRecallStore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def environ(tmp_path):
    ledgers = tmp_path / "ledgers"
    ledgers.mkdir()
    shutil.copy(FIXTURES / "ledger.json", ledgers / "fixture.json")
    environ = {"LEDGER_DIR": str(ledgers), "AGENTIHOOKS_HOME": str(tmp_path / "home")}
    assert main(["reindex", "--ledger", "fixture"], environ) == 0
    return environ


def run(environ, capsys, *argv):
    capsys.readouterr()
    code = main(["eval", *argv], environ)
    return code, json.loads(capsys.readouterr().out)


def test_eval_prints_the_top_five_hit_rate_of_the_golden_set(environ, capsys):
    code, out = run(environ, capsys, str(FIXTURES / "golden.json"))
    assert code == 0
    assert out == {
        "questions": 5,
        "hits": 4,
        "top5_hit_rate": 0.8,
        "misses": [{"question": "kubernetes rollout", "expect": ["tasks/t1"], "found": []}],
    }


def test_a_miss_lists_what_the_search_found(environ):
    store = SQLiteRecallStore(Path(environ["AGENTIHOOKS_HOME"]) / "recall" / "recall.sqlite3")
    result = evaluate(store, [{"question": "semantic search", "expect": "tasks/t1"}])
    assert result["top5_hit_rate"] == 0.0
    assert result["misses"][0]["found"][0] == "followups/f1"


def test_the_rate_is_rounded_to_four_places(environ):
    store = SQLiteRecallStore(Path(environ["AGENTIHOOKS_HOME"]) / "recall" / "recall.sqlite3")
    entries = [{"question": "t1", "expect": "tasks/t1"}] + [{"question": "nothing", "expect": "tasks/t1"}] * 2
    assert evaluate(store, entries) == {
        "questions": 3,
        "hits": 1,
        "top5_hit_rate": 0.3333,
        "misses": [{"question": "nothing", "expect": ["tasks/t1"], "found": []}] * 2,
    }


def test_eval_refuses_a_missing_golden_file(environ, capsys, tmp_path):
    code, out = run(environ, capsys, str(tmp_path / "absent.json"))
    assert code == 1
    assert out == {"error": f"cannot read golden file {tmp_path / 'absent.json'}"}


def test_load_golden_refuses_an_empty_set_and_entries_without_question_or_expect(tmp_path):
    path = tmp_path / "golden.json"
    path.write_text("[]")
    with pytest.raises(GoldenError) as caught:
        load_golden(path)
    assert str(caught.value) == f"golden file holds no questions: {path}"
    path.write_text('{"question": "x", "expect": "y"}')
    with pytest.raises(GoldenError) as caught:
        load_golden(path)
    assert str(caught.value) == f"golden file holds no questions: {path}"
    for entry in ({"question": "x"}, {"expect": "y"}, {"question": "", "expect": "y"}, "x"):
        path.write_text(json.dumps([{"question": "a", "expect": "b"}, entry]))
        with pytest.raises(GoldenError) as caught:
            load_golden(path)
        assert str(caught.value) == "golden entry 2 needs a question and an expect"
    path.write_text(json.dumps([{"question": "a", "expect": "b"}]))
    assert load_golden(path) == [{"question": "a", "expect": "b"}]
