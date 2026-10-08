from datetime import UTC, datetime

import pytest

from scripts.recall.models import RecallRecord
from scripts.recall.search import (
    DAY,
    WEIGHTS,
    Filters,
    Parent,
    RankWeights,
    parse_time,
    search,
    timeline,
)
from scripts.recall.store import SQLiteRecallStore

NOW = 1_000 * DAY


def record(ref, text="body", index=0, **fields):
    values = dict(
        key=f"{fields.get('ledger_slug', 'demo')}/{ref}#{index}",
        ledger_slug="demo",
        swarm_slug="swarm",
        kind="comment",
        ref=ref,
        parent_ref="",
        author="engineer@1",
        time=NOW,
        title="",
        text=text,
        chunk_index=index,
    )
    values.update(fields)
    return RecallRecord(**values)


@pytest.fixture
def store(tmp_path):
    return SQLiteRecallStore(tmp_path / "recall.sqlite3")


def put(store, *records, source="ledger/demo"):
    store.sync(source, records)


def refs(store, query, limit=10, **filters):
    return [hit.ref for hit in search(store, query, Filters(**filters), limit, now=NOW)]


def test_parse_time_reads_iso_times_epochs_and_relative_spans():
    assert parse_time(None, NOW) is None
    assert parse_time("", NOW) is None
    assert parse_time(1234, NOW) == 1234
    assert parse_time("1234", NOW) == 1234
    assert parse_time("2026-10-08T12:00:00Z", NOW) == int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp() * 1000)
    assert parse_time("2026-10-08T12:00:00", NOW) == int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp() * 1000)
    assert parse_time("2026-10-08T14:00:00+02:00", NOW) == int(datetime(2026, 10, 8, 12, tzinfo=UTC).timestamp() * 1000)
    assert parse_time("30m", NOW) == NOW - 30 * 60_000
    assert parse_time("6h", NOW) == NOW - 6 * 3_600_000
    assert parse_time("1d", NOW) == NOW - DAY
    assert parse_time("2 days", NOW) == NOW - 2 * DAY
    assert parse_time(" 1 week ", NOW) == NOW - 7 * DAY
    assert parse_time("3w", NOW) == NOW - 21 * DAY


def test_parse_time_refuses_text_that_is_no_time():
    with pytest.raises(ValueError) as caught:
        parse_time("2 months", NOW)
    assert str(caught.value) == "not an ISO time, epoch milliseconds or span such as 1d: 2 months"


def test_scope_narrows_to_a_ledger_or_a_swarm_slug(store):
    put(store, record("chat/a", "alpha words"))
    put(
        store,
        record("chat/b", "alpha words", ledger_slug="other", swarm_slug="other"),
        source="ledger/other",
    )
    put(
        store,
        record("chat/c", "alpha words", ledger_slug="third", swarm_slug="swarm"),
        source="ledger/third",
    )
    assert sorted(refs(store, "alpha")) == ["chat/a", "chat/b", "chat/c"]
    assert sorted(refs(store, "alpha", scope="")) == ["chat/a", "chat/b", "chat/c"]
    assert refs(store, "alpha", scope="other") == ["chat/b"]
    assert sorted(refs(store, "alpha", scope="swarm")) == ["chat/a", "chat/c"]
    assert refs(store, "alpha", scope="demo") == ["chat/a"]


def test_kinds_keep_only_the_named_kinds(store):
    put(store, record("chat/a", "alpha", kind="chat"), record("notes/b", "alpha", kind="note"), record("x/c", "alpha"))
    assert sorted(refs(store, "alpha", kinds=("chat", "note"))) == ["chat/a", "notes/b"]
    assert refs(store, "alpha", kinds=("note",)) == ["notes/b"]


def test_author_keeps_only_that_author(store):
    put(store, record("chat/a", "alpha", author="master@1"), record("chat/b", "alpha", author="master@10"))
    assert refs(store, "alpha", author="master@1") == ["chat/a"]


def test_seat_matches_seat_records_and_handoffs_naming_the_seat(store):
    put(
        store,
        record("seats/eng-1@s/learned/x", "alpha"),
        record("seats/eng-10@s/learned/y", "alpha"),
        record("tasks/t1/handoff/done", '{"seat": "eng-1@s", "task": "t1"}\n\nalpha', kind="handoff"),
        record("tasks/t2/handoff/done", '{"seat": "eng-10@s", "task": "t2"}\n\nalpha', kind="handoff"),
        record("chat/z", "alpha eng-1@s"),
    )
    assert sorted(refs(store, "alpha", seat="eng-1@s")) == ["seats/eng-1@s/learned/x", "tasks/t1/handoff/done"]


def test_since_and_until_bound_the_record_time(store):
    put(
        store,
        record("chat/old", "alpha", time=NOW - 3 * DAY),
        record("chat/mid", "alpha", time=NOW - DAY),
        record("chat/new", "alpha", time=NOW),
    )
    assert refs(store, "alpha", since="2d") == ["chat/new", "chat/mid"]
    assert refs(store, "alpha", since=NOW - DAY) == ["chat/new", "chat/mid"]
    assert refs(store, "alpha", until="2d") == ["chat/old"]
    assert refs(store, "alpha", since="2d", until=NOW - DAY) == ["chat/mid"]


def test_a_query_naming_a_task_id_returns_that_task_first(store):
    put(
        store,
        record("chat/a", "ranking ranking ranking", title="ranking ranking"),
        record("tasks/rc5", "unrelated text", kind="task", time=0),
    )
    hits = search(store, "rc5 ranking", now=NOW)
    assert [(hit.ref, hit.exact) for hit in hits] == [("tasks/rc5", True), ("chat/a", False)]


def test_exact_ids_cover_phases_followups_and_questions_in_query_order(store):
    put(
        store,
        record("phases/p2", "x", kind="phase"),
        record("followups/f3", "x", kind="followup"),
        record("questions/q4", "x", kind="question"),
        record("tasks/q4", "x", kind="task"),
    )
    assert refs(store, "f3 p2") == ["followups/f3", "phases/p2"]
    assert refs(store, "q4") == ["tasks/q4", "questions/q4"]


def test_a_query_naming_a_full_ref_returns_that_item_first(store):
    put(
        store,
        record("tasks/t1", "alpha", kind="task"),
        record("tasks/t1/comments/c9", "nothing", parent_ref="tasks/t1"),
    )
    assert refs(store, "alpha tasks/t1/comments/c9") == ["tasks/t1/comments/c9", "tasks/t1"]


def test_a_pull_request_number_returns_the_task_carrying_it(store):
    put(
        store,
        record("tasks/a", "see https://github.com/o/r/pull/1738", kind="task"),
        record("tasks/b", "see https://github.com/o/r/pull/17380", kind="task"),
        record("chat/c", "merged https://github.com/o/r/pull/1738"),
    )
    assert refs(store, "#1738")[0] == "tasks/a"
    assert refs(store, "pull request 1738")[0] == "tasks/a"
    assert refs(store, "PR 1738")[0] == "tasks/a"
    assert [hit.exact for hit in search(store, "pr #1738", now=NOW)] == [True, False]
    assert refs(store, "1738") == ["tasks/a", "chat/c"]


def test_exact_matches_still_respect_the_filters(store):
    put(store, record("tasks/rc5", "x", kind="task"))
    assert refs(store, "rc5", scope="other") == []
    assert refs(store, "rc5", kinds=("chat",)) == []


def test_chunks_collapse_to_one_hit_per_item_with_the_best_chunk_snippet(store):
    put(
        store,
        record("tasks/t1", "alpha first part", index=0, kind="task"),
        record("tasks/t1", "alpha alpha beta second part", index=1, kind="task"),
        record("tasks/t1", "unrelated third part", index=2, kind="task"),
    )
    hits = search(store, "beta", now=NOW)
    assert [(hit.item, hit.ref) for hit in hits] == [("demo/tasks/t1", "tasks/t1")]
    assert hits[0].snippet == "alpha alpha [beta] second part"
    assert len(search(store, "alpha", now=NOW)) == 1


def test_each_hit_carries_its_parent_chain_and_fields(store):
    put(
        store,
        record("ledger", "Demo ledger", kind="ledger", title="Demo"),
        record("phases/p1", "Phase", kind="phase", title="Memory", parent_ref="ledger"),
        record("tasks/t1", "Task", kind="task", title="Build recall", parent_ref="phases/p1"),
        record("tasks/t1/comments/c1", "alpha comment", parent_ref="tasks/t1", author="worker", time=NOW - DAY),
        record("seats/eng-1@s/learned/x", "beta note", kind="learned", parent_ref="seats/eng-1@s"),
    )
    [hit] = search(store, "alpha", now=NOW)
    assert hit.parents == (
        Parent("tasks/t1", "task", "Build recall"),
        Parent("phases/p1", "phase", "Memory"),
        Parent("ledger", "ledger", "Demo"),
    )
    assert (hit.ledger_slug, hit.swarm_slug, hit.kind, hit.author, hit.time, hit.archived) == (
        "demo",
        "swarm",
        "comment",
        "worker",
        NOW - DAY,
        False,
    )
    assert hit.snippet == "[alpha] comment"
    [seat] = search(store, "beta", now=NOW)
    assert seat.parents == (Parent("seats/eng-1@s", "", ""),)


def test_an_archived_record_is_still_found_and_flagged(store):
    put(store, record("chat/a", "alpha"))
    put(store)
    [hit] = search(store, "alpha", now=NOW)
    assert (hit.ref, hit.archived) == ("chat/a", True)


def test_title_matches_outrank_body_matches(store):
    put(store, record("chat/body", "alpha one two", title="other"), record("chat/title", "one two", title="alpha"))
    assert refs(store, "alpha") == ["chat/title", "chat/body"]


def test_newer_records_outrank_older_equal_matches(store):
    put(store, record("chat/old", "alpha", time=NOW - 30 * DAY), record("chat/new", "alpha", time=NOW - DAY))
    assert refs(store, "alpha") == ["chat/new", "chat/old"]


def test_kind_weight_breaks_equal_matches_and_lives_in_the_config(store):
    put(store, record("chat/a", "alpha", kind="chat"), record("tasks/b", "alpha", kind="task"))
    assert refs(store, "alpha") == ["tasks/b", "chat/a"]
    weights = RankWeights(kinds={"chat": 1.0})
    assert [hit.ref for hit in search(store, "alpha", weights=weights, now=NOW)] == ["chat/a", "tasks/b"]


def test_the_score_combines_relevance_recency_and_kind_weight(store):
    put(store, record("tasks/a", "alpha", kind="task", time=NOW - 7 * DAY))
    [hit] = search(store, "alpha", now=NOW)
    assert hit.score == pytest.approx(WEIGHTS.relevance + WEIGHTS.recency * 0.5 + WEIGHTS.kinds["task"])
    weights = RankWeights(relevance=2.0, recency=1.0, half_life_days=14.0, kinds={})
    [hit] = search(store, "alpha", weights=weights, now=NOW)
    assert hit.score == pytest.approx(2.0 + 2**-0.5)


def test_the_default_weights_favour_titles_and_name_every_kind():
    assert (WEIGHTS.title, WEIGHTS.body) == (4.0, 1.0)
    assert (WEIGHTS.relevance, WEIGHTS.recency, WEIGHTS.half_life_days) == (1.0, 0.3, 7.0)
    assert WEIGHTS.candidates == 200
    assert WEIGHTS.kinds == {
        "task": 0.2,
        "handoff": 0.15,
        "learned": 0.15,
        "recap": 0.1,
        "phase": 0.1,
        "culture": 0.05,
        "followup": 0.05,
        "question": 0.05,
    }


def test_limit_caps_the_hits_after_collapse(store):
    put(store, *(record(f"chat/{n}", "alpha", time=NOW - n * DAY) for n in range(5)))
    assert refs(store, "alpha", limit=2) == ["chat/0", "chat/1"]


def test_a_query_with_no_words_and_no_id_finds_nothing(store):
    put(store, record("chat/a", "alpha"))
    assert search(store, "  ?!  ", now=NOW) == []


def test_query_words_with_fts_syntax_are_taken_as_words(store):
    put(store, record("chat/a", "alpha NEAR beta"))
    assert refs(store, 'alpha" OR "NEAR') == ["chat/a"]


def test_timeline_lists_one_entry_per_item_in_time_order(store):
    put(
        store,
        record("chat/b", "two", time=NOW - DAY, kind="chat"),
        record("tasks/a", "one", time=NOW - 2 * DAY, kind="task", title="Task a"),
        record("tasks/a", "one more", index=1, time=NOW - 2 * DAY, kind="task", title="Task a"),
        record("chat/c", "three", time=NOW, kind="chat"),
        record("chat/d", "three too", time=NOW, kind="chat"),
    )
    entries = timeline(store, now=NOW)
    assert [(entry.ref, entry.time) for entry in entries] == [
        ("tasks/a", NOW - 2 * DAY),
        ("chat/b", NOW - DAY),
        ("chat/c", NOW),
        ("chat/d", NOW),
    ]
    assert (entries[0].item, entries[0].kind, entries[0].title, entries[0].author) == (
        "demo/tasks/a",
        "task",
        "Task a",
        "engineer@1",
    )


def test_timeline_keeps_the_scope_window_and_kinds(store):
    put(
        store,
        record("chat/b", "x", time=NOW - DAY, kind="chat"),
        record("tasks/a", "x", time=NOW - 2 * DAY, kind="task"),
        record("chat/c", "x", time=NOW, kind="chat"),
    )
    put(store, record("chat/z", "x", kind="chat", ledger_slug="other", swarm_slug="other"), source="ledger/other")
    window = Filters(scope="demo", since="2d", until=NOW - 1)
    assert [entry.ref for entry in timeline(store, window, now=NOW)] == ["tasks/a", "chat/b"]
    assert [entry.ref for entry in timeline(store, Filters(kinds=("chat",)), now=NOW)] == ["chat/b", "chat/c", "chat/z"]
    assert [entry.ref for entry in timeline(store, Filters(), limit=2, now=NOW)] == ["tasks/a", "chat/b"]
