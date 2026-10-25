import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from hooks.mcp import sessions as mcp_sessions
from hooks.memory import archive
from hooks.memory.api import records
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant
from tests.sv2_idx04_cases import Corpus

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-tools.json"
EVIDENCE_CLASS = (
    "mocked: an in-process caller, a SQLite catalog file and a local content addressed object folder stand in for "
    "the session service, the central database and the archive bucket, and a recording stub stands in for the MCP "
    "server; no model, HTTP server, MCP transport or Pod runs"
)
HIDDEN = ("database is locked", "busy_timeout", "tools-lock.jsonl", "personal-corpus")


def grant(inputs, narrow=False, corpus=None):
    data = {**inputs["grant"], **(inputs["narrow_grant"] if narrow else {})}
    return Grant(data["principal"], corpus or data["corpus"], frozenset(data["projects"]))


def body(inputs, **fields):
    return {"schema_version": SCHEMA_VERSION, "access_domain": inputs["grant"]["corpus"], **fields}


class Bank:
    def __init__(self, inputs, root):
        self.inputs, self.corpus = inputs, Corpus(inputs, root)
        self.open()

    def open(self):
        self.tools = records.SessionTools(
            self.corpus.catalog, archive.ObjectStore(self.corpus.root / "central/objects")
        )

    def restart(self):
        self.corpus.restart()
        self.open()

    def ask(self, name, request, now=None, granted=None):
        clock = self.inputs["clock"]["now"] if now is None else now
        return records.call(self.tools, name, granted or grant(self.inputs), request, clock)

    def uid(self, name):
        return next(item["session_uid"] for item in self.inputs["sessions"] if item["name"] == name)

    def revoke(self, name, state="deleted"):
        catalog, domain = self.corpus.catalog, self.inputs["grant"]["corpus"]
        found = catalog.session(domain, self.uid(name))
        return catalog.set_state(domain, self.uid(name), state, found["revision"])

    def protected(self):
        return {name: rows for name, rows in self.corpus.protected().items() if name != "session_cursor"}

    def counter(self, name):
        return self.tools.counter(self.inputs["grant"]["corpus"], name)

    def short(self, uid):
        return next(item["name"] for item in self.inputs["sessions"] if item["session_uid"] == uid)


def listed(bank, answer):
    return [bank.short(item["session_uid"]) for item in answer["sessions"]]


def walk(bank, request, now=None):
    pages, answer = [], bank.ask("session_list", request, now)
    while True:
        pages.append(listed(bank, answer))
        if answer["next_cursor"] is None:
            return pages
        answer = bank.ask("session_list", {**request, "cursor": answer["next_cursor"]}, now)


def hit(bank):
    answer = bank.ask("session_search", body(bank.inputs, query=bank.inputs["queries"]["hit"], context=0))
    return answer, answer["results"][0]


def authoritative(bank, cited, records_answer):
    domain, store = bank.inputs["grant"]["corpus"], archive.ObjectStore(bank.corpus.root / "central/objects")
    events = bank.corpus.catalog.events(domain, cited["session_uid"], cited["source_generation"])
    by_id = {event["event_id"]: event for event in events}
    for item in records_answer["records"]:
        event = by_id[item["event_id"]]
        reference = {"access_domain": domain, **{key: cited[key] for key in ("session_uid", "source_generation")}}
        reference.update(byte_start=event["byte_start"], byte_end=event["byte_end"])
        raw = archive.source_range(bank.corpus.catalog, store, grant(bank.inputs), reference)
        assert item["record"] == event["record"] and item["source_text"].encode() == raw
        assert json.loads(raw) == event["record"] and item["normalized_hash"] == event["normalized_hash"]
    return True


def outline(answer):
    return [
        {key: item[key] for key in ("sequence", "cited", "kind", "timestamp", "task_id", "project_id")}
        for item in answer["records"]
    ]


def refused(answer):
    return {"state": answer["state"], "error": answer.get("error")}


def positive(inputs, root):
    bank = Bank(inputs, root)
    before = bank.protected()
    pages = walk(bank, body(inputs, limit=2))
    searched, top = hit(bank)
    cited = top["citation"]
    expanded = bank.ask("session_records", body(inputs, citation=cited, after=1))
    got = bank.ask("session_get", body(inputs, session_uid=cited["session_uid"]))
    resume_keys = sorted(key for item in searched["results"] for key in item if "resume" in key)
    assert pages == [["lock", "kernel"], ["readme"]] and bank.short(top["session_uid"]) == "lock"
    assert (
        expanded["complete"]
        and [item["event_id"] for item in expanded["records"] if item["cited"]] == cited["event_ids"]
    )
    assert authoritative(bank, cited, expanded) and not resume_keys and bank.protected() == before
    second = Bank(inputs, root / "second")
    assert walk(second, body(inputs, limit=2)) == pages and hit(second)[1]["citation"] == cited
    return {
        "list_pages": pages,
        "search": {
            "mode": searched["mode"],
            "vector": searched["vector"],
            "top": {
                "session": bank.short(top["session_uid"]),
                "kind": top["kind"],
                "truncated": top["truncated"],
                "omitted_chars": top["omitted_chars"],
                "session_metadata": top["session"],
                "citation_sequences": [cited["first_sequence"], cited["last_sequence"]],
                "citation_events": len(cited["event_ids"]),
            },
            "resume_fields_in_results": resume_keys,
        },
        "expansion": {
            "complete": expanded["complete"],
            "truncated": expanded["truncated"],
            "total": expanded["total"],
            "records": outline(expanded),
        },
        "expansion_matches_catalog_and_archive": True,
        "session_get": {key: value for key, value in got["session"].items() if key != "session_uid"},
        "second_fixture_matches": True,
        "authoritative_state_unchanged": True,
        records.RESOLUTION_FAILURES: bank.counter(records.RESOLUTION_FAILURES),
    }


def rejection(inputs, root):
    bank = Bank(inputs, root)
    _, top = hit(bank)
    cited = top["citation"]
    revision = bank.revoke("lock")
    before = bank.protected()
    stale = {**cited, "first_sequence": cited["first_sequence"] + 1}
    attempts = {
        "records_after_revocation": bank.ask("session_records", body(inputs, citation=cited)),
        "alias_get_session_segment": bank.ask("get_session_segment", body(inputs, citation=cited)),
        "alias_get_session": bank.ask("get_session", body(inputs, session_uid=cited["session_uid"])),
        "session_get_after_revocation": bank.ask("session_get", body(inputs, session_uid=cited["session_uid"])),
    }
    searched = bank.ask("search_sessions", body(inputs, query=inputs["queries"]["hit"]))
    listing = bank.ask("list_sessions", body(inputs))
    first = bank.ask("session_list", body(inputs, limit=1))
    scoped = {
        "foreign_corpus": bank.ask("session_list", {**body(inputs), "access_domain": inputs["foreign_corpus"]}),
        "cursor_from_another_grant": bank.ask(
            "session_list", body(inputs, limit=1, cursor=first["next_cursor"]), granted=grant(inputs, narrow=True)
        ),
        "stale_citation": bank.ask(
            "session_records", body(inputs, citation={**stale, "session_uid": bank.uid("readme")})
        ),
    }
    diagnostics = json.dumps({**attempts, **scoped}, ensure_ascii=False)
    untouched = bank.protected() == before
    assert all(answer["state"] == "rejected" for answer in {**attempts, **scoped}.values())
    assert {answer["error"]["class"] for answer in attempts.values()} == {"evidence_unavailable"}
    assert searched["results"] == [] and "lock" not in listed(bank, listing) and untouched
    assert not any(hidden in diagnostics for hidden in HIDDEN)
    failures = bank.counter(records.RESOLUTION_FAILURES)
    _, fresh = hit(Bank(inputs, root / "fresh"))
    corrected = bank.ask(
        "session_records",
        body(
            inputs,
            citation=bank.ask("session_search", body(inputs, query=inputs["queries"]["readme"]))["results"][0][
                "citation"
            ],
        ),
    )
    assert corrected["state"] == "answered" and bank.counter(records.RESOLUTION_FAILURES) == failures
    return {
        "revoked_revision": revision,
        "rejected": {name: refused(answer) for name, answer in attempts.items()},
        "alias_search_results_after_revocation": len(searched["results"]),
        "alias_list_after_revocation": listed(bank, listing),
        "alias_metadata": listing["alias"],
        "scope_and_cursor": {name: refused(answer) for name, answer in scoped.items()},
        "protected_state_unchanged": untouched,
        "hidden_names_absent_from_diagnostics": True,
        "corrected_with_a_new_citation": {"state": corrected["state"], "records": len(corrected["records"])},
        "unrevoked_fixture_still_cites_the_hit": bank.short(fresh["session_uid"]),
        records.RESOLUTION_FAILURES: failures,
        records.ALIAS_CALLS: bank.counter(records.ALIAS_CALLS),
    }


class Recorder:
    def __init__(self):
        self.names = []

    def tool(self, name=None):
        def register(function):
            self.names.append(name or function.__name__)
            return function

        return register


def registered(environment):
    recorder = Recorder()
    with mock.patch.dict(os.environ, environment, clear=False):
        for key in (mcp_sessions.CATALOG_ENV, mcp_sessions.ALIASES_ENV):
            if key not in environment:
                os.environ.pop(key, None)
        mcp_sessions.register(recorder)
    return sorted(recorder.names)


def rollback(bank, root):
    before = bank.protected()
    shown = registered({mcp_sessions.CATALOG_ENV: str(root / "central/catalog.sqlite3")})
    aliased = registered(
        {mcp_sessions.CATALOG_ENV: str(root / "central/catalog.sqlite3"), mcp_sessions.ALIASES_ENV: "1"}
    )
    hidden = registered({})
    assert hidden == [] and len(shown) == 4 and len(aliased) == 4 + len(records.ALIASES)
    assert bank.protected() == before
    return {
        "tools_with_catalog": shown,
        "tools_with_aliases": aliased,
        "tools_hidden": hidden,
        "archive_unchanged": True,
    }


def expand(bank, request, cursor, now):
    pages = []
    while cursor is not None:
        answer = bank.ask("session_records", {**request, "cursor": cursor}, now)
        pages.append(answer["records"])
        cursor = answer["next_cursor"]
    return pages


def recovery(inputs, root):
    bank = Bank(inputs, root)
    clock = inputs["clock"]
    first = bank.ask("session_list", body(inputs, limit=1))
    page = bank.ask("session_list", body(inputs, limit=1, cursor=first["next_cursor"]))
    _, top = hit(bank)
    expansion = body(inputs, citation=top["citation"], after=1, limit=2)
    head = bank.ask("session_records", expansion)
    bank.restart()
    resumed = bank.ask("session_list", body(inputs, limit=1, cursor=first["next_cursor"]), clock["later"])
    replayed = bank.ask("session_list", body(inputs, limit=1, cursor=first["next_cursor"]), clock["later"])
    tail = expand(bank, expansion, head["next_cursor"], clock["later"])
    expired = bank.ask("session_list", body(inputs, limit=1, cursor=first["next_cursor"]), clock["expired"])
    fresh = bank.ask("session_list", body(inputs, limit=1), clock["expired"])
    bank.revoke("kernel", "restricted")
    moved = bank.ask("session_list", body(inputs, limit=1, cursor=fresh["next_cursor"]), clock["expired"])
    restarted = bank.ask("session_list", body(inputs, limit=1), clock["expired"])
    joined = [item["sequence"] for item in head["records"]] + [item["sequence"] for chunk in tail for item in chunk]
    assert resumed == page == replayed and joined == sorted(set(joined)) and len(joined) == head["total"]
    assert expired["error"]["detail"] == records.CURSOR_EXPIRED and moved["error"]["detail"] == records.CURSOR_MOVED
    assert listed(bank, restarted) == ["lock"]
    return {
        "cursor_page_before_restart": listed(bank, page),
        "cursor_page_after_restart_identical": resumed == page,
        "cursor_replay_identical": replayed == resumed,
        "expansion_across_restart": {"head": outline(head), "tail_pages": len(tail), "sequences": joined},
        "expired_cursor": refused(expired),
        "changed_listing_cursor": refused(moved),
        "listing_restarted_without_cursor": listed(bank, restarted),
        "rollback_rehearsal": rollback(bank, bank.corpus.root),
        records.RESOLUTION_FAILURES: bank.counter(records.RESOLUTION_FAILURES),
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](inputs, Path(folder))
    return {
        "case": f"T-SV2-IDX-05-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
