import json

import pytest

from hooks.mcp import recall as recall_mcp
from scripts.recall import tools
from scripts.recall.cli import main
from scripts.recall.models import RecallRecord
from scripts.recall.search import DAY, Filters
from scripts.recall.store import SQLiteRecallStore, default_path
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("mcp-sdk")]

T0 = 1_700_000_000_000


def record(slug, ref, text="body", index=0, **fields):
    values = dict(
        key=f"{slug}/{ref}#{index}",
        ledger_slug=slug,
        swarm_slug=slug,
        kind="task",
        ref=ref,
        parent_ref="",
        author="engineer@1",
        time=T0,
        title=f"title of {ref}",
        text=text,
        chunk_index=index,
    )
    values.update(fields)
    return RecallRecord(**values)


ALPHA = [
    record("alpha", "ledger", "alpha overview", kind="ledger", title="Alpha", time=T0 - 3 * DAY),
    record("alpha", "phases/p1", "phase words", kind="phase", parent_ref="ledger", time=T0 - 2 * DAY),
    record("alpha", "tasks/t1", "first part of shared words", parent_ref="phases/p1", time=T0 - DAY),
    record("alpha", "tasks/t1", "second part", index=1, parent_ref="phases/p1", time=T0 - DAY),
    record(
        "alpha",
        "tasks/t1/comments/c1",
        "a comment on it",
        kind="comment",
        parent_ref="tasks/t1",
        author="operator",
        time=T0,
    ),
    record(
        "alpha",
        "tasks/t1/comments/c1/comments/c2",
        "a reply",
        kind="comment",
        parent_ref="tasks/t1/comments/c1",
        time=T0 + 1,
    ),
    record("alpha", "tasks/t1/answers/a1", "an answer", kind="answer", parent_ref="tasks/t1", time=T0 + 2),
]
BETA = [record("beta", "tasks/t1", "beta shared words", time=T0 - 5 * DAY)]


def bin_entries(folder, entries):
    repository = SQLiteLedgerRepository(folder / DATABASE)
    with repository.connect() as connection, connection:
        repository.save_registry(connection, "bin", entries)


@pytest.fixture
def environ(tmp_path):
    ledgers = tmp_path / "ledgers"
    ledgers.mkdir()
    bin_entries(ledgers, {"beta": 5})
    values = {"LEDGER_DIR": str(ledgers), "AGENTIHOOKS_HOME": str(tmp_path / "home")}
    store = SQLiteRecallStore(default_path(values))
    store.sync("ledger/alpha", ALPHA)
    store.sync("ledger/beta", BETA)
    return values


def in_swarm(environ, swarm="alpha"):
    return {**environ, "AGENTIHOOKS_SWARM": swarm}


def items(result):
    return [hit["item"] for hit in result["hits"]]


def test_the_registry_serves_the_four_recall_tools():
    from hooks.mcp import build_server
    from hooks.mcp._registry import CATEGORY_MODULES

    assert CATEGORY_MODULES["recall"] == "hooks.mcp.recall"
    names = set(build_server(["recall"])._tool_manager._tools)
    assert names == {"recall_search", "recall_get", "recall_timeline", "recall_sources"}


def test_search_defaults_to_the_callers_swarm(environ):
    result = tools.search("shared", environ=in_swarm(environ))
    assert result["success"] is True
    assert result["scope"] == "alpha"
    assert items(result) == ["alpha/tasks/t1"]


def test_search_with_scope_all_crosses_swarms_and_a_slug_narrows(environ):
    assert set(items(tools.search("shared", tools.filters("all"), environ=in_swarm(environ)))) == {
        "alpha/tasks/t1",
        "beta/tasks/t1",
    }
    assert items(tools.search("shared", tools.filters("beta"), environ=in_swarm(environ))) == ["beta/tasks/t1"]


def test_search_outside_a_swarm_searches_every_source(environ):
    result = tools.search("shared", environ=environ)
    assert result["scope"] == "all"
    assert len(result["hits"]) == 2


def test_search_hits_carry_parents_and_filters_apply(environ):
    hit = tools.search("shared", environ=in_swarm(environ))["hits"][0]
    assert [parent["ref"] for parent in hit["parents"]] == ["phases/p1", "ledger"]
    kinds = tools.search("words", tools.filters(kinds="phase, ledger"), environ=in_swarm(environ))
    assert items(kinds) == ["alpha/phases/p1"]
    assert items(tools.search("comment", tools.filters(author="operator"), environ=in_swarm(environ))) == [
        "alpha/tasks/t1/comments/c1"
    ]
    assert len(tools.search("words", tools.filters("all"), 1, environ=environ)["hits"]) == 1


def test_search_refuses_an_unreadable_time(environ):
    result = tools.search("words", tools.filters(period="yesterday-ish"), environ=environ)
    assert result["success"] is False
    assert "yesterday-ish" in result["error"]


def test_filters_read_kinds_and_a_period():
    assert tools.filters("alpha", " task, ,comment ", "operator", "eng-1", "2d..1d") == Filters(
        "alpha", ("task", "comment"), "operator", "eng-1", "2d", "1d"
    )
    assert tools.filters(period="..2026-10-01") == Filters("", (), "", "", None, "2026-10-01")
    assert tools.filters(period="3h") == Filters("", (), "", "", "3h", None)


def test_get_returns_the_whole_item_with_parents_and_comments(environ):
    result = tools.get("alpha/tasks/t1", environ=environ)
    assert result["success"] is True
    item = result["item"]
    assert item["item"] == "alpha/tasks/t1"
    assert item["text"] == "first part of shared words\n\nsecond part"
    assert [parent["ref"] for parent in item["parents"]] == ["phases/p1", "ledger"]
    comments = item["comments"]
    assert [(c["ref"], c["kind"], c["author"], c["text"]) for c in comments] == [
        ("tasks/t1/comments/c1", "comment", "operator", "a comment on it"),
        ("tasks/t1/answers/a1", "answer", "engineer@1", "an answer"),
    ]
    assert [reply["text"] for reply in comments[0]["comments"]] == ["a reply"]


def test_get_resolves_a_bare_ref_inside_the_scope(environ):
    assert tools.get("tasks/t1", environ=in_swarm(environ, "beta"))["item"]["text"] == "beta shared words"


def test_get_refuses_an_ambiguous_or_unknown_ref(environ):
    ambiguous = tools.get("tasks/t1", scope="all", environ=environ)
    assert ambiguous["success"] is False
    assert "alpha/tasks/t1" in ambiguous["error"] and "beta/tasks/t1" in ambiguous["error"]
    missing = tools.get("tasks/none", environ=environ)
    assert missing == {"success": False, "error": "no recall item tasks/none in scope all"}


def test_timeline_lists_items_in_time_order_within_scope(environ):
    result = tools.timeline(environ=in_swarm(environ))
    assert result["scope"] == "alpha"
    assert [entry["ref"] for entry in result["entries"]] == [
        "ledger",
        "phases/p1",
        "tasks/t1",
        "tasks/t1/comments/c1",
        "tasks/t1/comments/c1/comments/c2",
        "tasks/t1/answers/a1",
    ]
    narrowed = tools.timeline(tools.filters("all", "task", period=f"..{T0 - 2 * DAY}"), environ=environ)
    assert [entry["item"] for entry in narrowed["entries"]] == ["beta/tasks/t1"]


def test_sources_list_counts_indexed_time_and_bin(environ):
    before = tools.sources(environ=environ)
    by_source = {entry["source"]: entry for entry in before["sources"]}
    assert set(by_source) == {"ledger/alpha", "ledger/beta"}
    alpha = by_source["ledger/alpha"]
    assert (alpha["kind"], alpha["slug"], alpha["swarm_slug"]) == ("ledger", "alpha", "alpha")
    assert (alpha["records"], alpha["items"], alpha["archived"], alpha["binned"]) == (7, 6, 0, False)
    assert by_source["ledger/beta"]["binned"] is True
    assert alpha["last_indexed"] > 0
    SQLiteRecallStore(default_path(environ)).sync("ledger/alpha", ALPHA[:2])
    after = {entry["source"]: entry for entry in tools.sources(environ=environ)["sources"]}["ledger/alpha"]
    assert after["archived"] == 4
    assert after["last_indexed"] >= alpha["last_indexed"]
    SQLiteRecallStore(default_path(environ)).sync("ledger/alpha", ALPHA[:2])
    again = {entry["source"]: entry for entry in tools.sources(environ=environ)["sources"]}["ledger/alpha"]
    assert again == after


def test_a_missing_archive_is_an_error_and_never_created(tmp_path):
    environ = {"AGENTIHOOKS_HOME": str(tmp_path / "empty"), "LEDGER_DIR": str(tmp_path)}
    for result in (
        tools.search("words", environ=environ),
        tools.get("tasks/t1", environ=environ),
        tools.timeline(environ=environ),
        tools.sources(environ=environ),
    ):
        assert result["success"] is False
        assert "agentihooks recall reindex" in result["error"]
    assert not default_path(environ).exists()


def test_the_cli_prints_the_same_json(environ, capsys):
    assert main(["search", "shared", "--scope", "beta"], in_swarm(environ)) == 0
    expected = json.loads(json.dumps(tools.search("shared", tools.filters("beta"), environ=environ)))
    assert json.loads(capsys.readouterr().out) == expected
    assert main(["get", "alpha/tasks/t1"], environ) == 0
    assert json.loads(capsys.readouterr().out)["item"]["text"].endswith("second part")
    assert main(["timeline", "--kind", "task", "--kind", "phase", "--limit", "1"], in_swarm(environ)) == 0
    assert [entry["ref"] for entry in json.loads(capsys.readouterr().out)["entries"]] == ["phases/p1"]
    assert main(["sources"], environ) == 0
    assert len(json.loads(capsys.readouterr().out)["sources"]) == 2
    assert main(["get", "tasks/none"], environ) == 1
    assert json.loads(capsys.readouterr().out)["success"] is False


def test_the_mcp_tools_answer_json_for_the_session_swarm(environ, monkeypatch):
    for key, value in in_swarm(environ).items():
        monkeypatch.setenv(key, value)
    from hooks.mcp import build_server

    served = {name: tool.fn for name, tool in build_server(["recall"])._tool_manager._tools.items()}
    assert served.keys() == {"recall_search", "recall_get", "recall_timeline", "recall_sources"}
    assert recall_mcp.register
    assert [hit["item"] for hit in json.loads(served["recall_search"]("shared"))["hits"]] == ["alpha/tasks/t1"]
    assert json.loads(served["recall_get"]("tasks/t1"))["item"]["item"] == "alpha/tasks/t1"
    assert json.loads(served["recall_timeline"](kinds="phase"))["entries"][0]["ref"] == "phases/p1"
    assert len(json.loads(served["recall_sources"]())["sources"]) == 2
