"""SV2-SES-05: backfill historical provider sessions with provenance."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from hooks.memory import importer as importer_module
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog
from hooks.memory.importer import (
    IMPORTER_VERSION,
    ImportLedger,
    ImportPlan,
    SourceRoot,
    import_sessions,
    read_source,
    resolve_project,
)
from scripts.swarm_v2 import import_sessions as cli
from tests import sv2_ses05_cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-SES-05"
PROJECT = "github.com/acme/app"
GRANT = Grant("importer", "corpus-a", frozenset({PROJECT, "github.com/acme/other"}))
ALIASES = {"/work/app": PROJECT}


def line(**entry):
    return (json.dumps(entry, separators=(",", ":")) + "\n").encode()


def claude(text, cwd="/work/app", session="s-1", **extra):
    return line(type="user", sessionId=session, cwd=cwd, message={"role": "user", "content": text}, **extra)


@pytest.fixture
def stores(tmp_path):
    catalog, imports = Catalog(tmp_path / "catalog.sqlite3"), ImportLedger(tmp_path / "imports.sqlite3")
    yield catalog, imports
    catalog.close()
    imports.close()


def root(tmp_path, provider="claude", files=None, corpus="corpus-a", expected=(), installation="host-a"):
    folder = tmp_path / f"{provider}-{installation}"
    for relative, data in (files or {}).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return SourceRoot(provider, installation, folder, corpus, tuple(expected))


def run(stores, *roots, aliases=None, dry_run=False, grant=GRANT):
    catalog, imports = stores
    plan = ImportPlan(tuple(roots), ALIASES if aliases is None else aliases)
    return import_sessions(catalog, imports, grant, plan, dry_run=dry_run)


def only(report):
    assert report["state"] == "completed", report
    (source,) = report["sources"]
    return source


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/work/app", PROJECT),
        ("/work/app/hooks/deep", PROJECT),
        ("/work/app/../app", None),
        ("/elsewhere/app", None),
        ("/work/app2", None),
        ("/work", None),
        ("work/app", None),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_a_folder_resolves_only_through_an_absolute_alias_or_its_descendants(cwd, expected):
    assert resolve_project(cwd, ALIASES) == expected


def test_the_longest_aliased_folder_wins():
    aliases = {"/work": "github.com/acme/other", "/work/app": PROJECT}
    assert resolve_project("/work/app/x", aliases) == PROJECT
    assert resolve_project("/work/lib", aliases) == "github.com/acme/other"
    assert resolve_project("/work/app/x", dict(reversed(list(aliases.items())))) == PROJECT


def test_a_claude_source_is_split_into_line_ranges_with_the_project_in_effect_at_each_line():
    first = claude("one")
    second = line(
        type="assistant",
        cwd="/scratch",
        timestamp="2025-01-01T00:00:01Z",
        message={"role": "assistant", "content": [{"type": "text", "text": "two"}, {"type": "text", "text": "três"}]},
    )
    read = read_source("claude", first + b"\n" + second, ALIASES)
    end = len(first) + 1
    assert [(e["sequence"], e["byte_start"], e["byte_end"], e["project_id"]) for e in read.events] == [
        (1, 0, len(first) - 1, PROJECT),
        (2, end, end + len(second) - 1, None),
        (3, end, end + len(second) - 1, None),
    ]
    assert read.events[0]["record"] == {"kind": "user_text", "text": "one", "source_time": None}
    assert read.events[2]["record"] == {"kind": "assistant_text", "text": "três", "source_time": "2025-01-01T00:00:01Z"}
    assert read.losses == []


def test_truncated_unparseable_and_non_object_lines_are_reported_as_losses_with_their_ranges():
    good, bad, listed, tail = claude("ok"), b'{"type":\n', b"[1, 2]\n", b'{"type":"user"'
    data = good + bad + listed + tail
    read = read_source("claude", data, ALIASES)
    assert len(read.events) == 1
    start = len(good)
    assert read.losses == [
        {"byte_start": start, "byte_end": start + len(bad) - 1, "reason": "unparseable line"},
        {"byte_start": start + len(bad), "byte_end": start + len(bad) + len(listed) - 1, "reason": "unparseable line"},
        {"byte_start": len(data) - len(tail), "byte_end": len(data), "reason": "truncated final line"},
    ]
    assert read_source("claude", b"\xff\xfe\n", ALIASES).losses == [
        {"byte_start": 0, "byte_end": 2, "reason": "unparseable line"}
    ]


def test_a_final_line_that_parses_but_is_not_an_object_is_unparseable_rather_than_truncated():
    assert read_source("claude", claude("x") + b"[1]", ALIASES).losses == [
        {"byte_start": len(claude("x")), "byte_end": len(claude("x")) + 3, "reason": "unparseable line"}
    ]


def test_a_complete_final_line_without_a_newline_is_an_event_and_not_a_loss():
    first, last = claude("one"), claude("two").rstrip(b"\n")
    read = read_source("claude", first + last, ALIASES)
    assert [event["record"]["text"] for event in read.events] == ["one", "two"]
    assert read.events[1]["byte_start"] == len(first) and read.events[1]["byte_end"] == len(first + last)
    assert read.losses == []


def test_codex_metadata_and_working_folder_come_from_session_meta_and_turn_context():
    data = (
        line(type="session_meta", timestamp="t0", payload={"id": "cx-1", "cwd": "/work/app", "cli_version": "0.1"})
        + line(type="turn_context", timestamp="t1", payload={"cwd": "/other"})
        + line(
            type="response_item",
            timestamp="t2",
            payload={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        )
    )
    read = read_source("codex", data, ALIASES)
    assert [(e["record"]["kind"], e["project_id"]) for e in read.events] == [("meta", PROJECT), ("user_text", None)]
    assert read.metadata == {
        "native_session_id": "cx-1",
        "provider_version": "0.1",
        "working_folders": ["/work/app", "/other"],
        "first_event_time": "t0",
        "last_event_time": "t2",
    }


def test_claude_metadata_keeps_the_first_identity_and_version_and_every_folder_in_order():
    data = (
        claude("a", session="s-1", version="1.0", timestamp="t1")
        + claude("b", cwd="/x", session="s-2", version="2.0", timestamp="t2")
        + claude("c", timestamp="t3")
    )
    assert read_source("claude", data, ALIASES).metadata == {
        "native_session_id": "s-1",
        "provider_version": "1.0",
        "working_folders": ["/work/app", "/x"],
        "first_event_time": "t1",
        "last_event_time": "t3",
    }
    assert read_source("claude", b"", ALIASES).metadata == {
        "native_session_id": None,
        "provider_version": None,
        "working_folders": [],
        "first_event_time": None,
        "last_event_time": None,
    }


def test_non_text_metadata_values_are_ignored():
    data = line(type="user", sessionId=5, cwd=["/work/app"], version=1, timestamp=2, message={"content": "x"})
    read = read_source("claude", data, ALIASES)
    assert read.metadata["native_session_id"] is None
    assert read.metadata["provider_version"] is None
    assert read.metadata["working_folders"] == []
    assert read.metadata["first_event_time"] is None
    assert read.events[0]["project_id"] is None


def test_an_import_commits_each_source_with_full_provenance_and_explicit_unknowns(stores, tmp_path):
    data = claude("hello", timestamp="t1") + claude("bye", cwd="/work/app/sub", timestamp="t2")
    source = only(run(stores, root(tmp_path, files={"proj/s-1.jsonl": data})))
    digest = hashlib.sha256(data).hexdigest()
    assert source == {
        "provider": "claude",
        "installation": "host-a",
        "relative_path": "proj/s-1.jsonl",
        "source_sha256": digest,
        "outcome": "committed",
        "session_uid": "corpus-a:claude:host-a:s-1",
        "events": 2,
        "unknown_attribution_events": 0,
    }
    catalog, imports = stores
    (session,) = imports.sessions("corpus-a")
    assert session["session_uid"] == "corpus-a:claude:host-a:s-1"
    assert session["source_generation"] == f"import:{digest}"
    assert session["provenance"] == {
        "provider": "claude",
        "installation": "host-a",
        "relative_path": "proj/s-1.jsonl",
        "source_sha256": digest,
        "source_bytes": len(data),
        "importer_version": IMPORTER_VERSION,
        "native_session_id": "s-1",
        "session_id_source": "transcript",
        "provider_version": None,
        "working_folders": ["/work/app", "/work/app/sub"],
        "first_event_time": "t1",
        "last_event_time": "t2",
        "projects": [PROJECT],
        "unknown_attribution_events": 0,
        "ambiguous_basenames": [],
        "losses": [],
        "unknown": ["provider_version"],
    }
    events = catalog.events("corpus-a", session["session_uid"], session["source_generation"])
    assert [event["record"]["text"] for event in events] == ["hello", "bye"]


def test_the_first_version_seen_is_kept_and_a_file_name_stands_in_for_a_missing_session_id(stores, tmp_path):
    data = line(type="user", cwd="/x", version="2.1", message={"content": "q"})
    only(run(stores, root(tmp_path, files={"p/abc-123.jsonl": data})))
    (session,) = stores[1].sessions("corpus-a")
    provenance = session["provenance"]
    assert session["session_uid"] == "corpus-a:claude:host-a:abc-123"
    assert provenance["native_session_id"] == "abc-123"
    assert provenance["session_id_source"] == "file name"
    assert provenance["provider_version"] == "2.1"
    assert provenance["projects"] == []
    assert provenance["unknown_attribution_events"] == 1
    assert provenance["unknown"] == ["first_event_time", "last_event_time", "project"]


def test_a_folder_sharing_an_aliased_basename_stays_unattributed_and_is_named_ambiguous(stores, tmp_path):
    data = claude("mine", cwd="/home/me/app") + claude("theirs", cwd="/home/me/lib")
    source = only(run(stores, root(tmp_path, files={"p/s.jsonl": data})))
    assert source["unknown_attribution_events"] == 2
    provenance = stores[1].sessions("corpus-a")[0]["provenance"]
    assert provenance["ambiguous_basenames"] == ["/home/me/app"]
    assert provenance["projects"] == []
    assert stores[1].unknown_attribution("corpus-a") == 2


def test_a_rerun_skips_imported_sources_by_hash_and_a_changed_source_is_a_new_generation(stores, tmp_path):
    source = root(tmp_path, files={"p/s-1.jsonl": claude("one")})
    first = only(run(stores, source))
    again = only(run(stores, source))
    assert again == {**first, "outcome": "already_imported", "events": 0, "unknown_attribution_events": 0}
    with open(source.path / "p/s-1.jsonl", "ab") as handle:
        handle.write(claude("two"))
    grown = only(run(stores, source))
    assert grown["outcome"] == "committed" and grown["events"] == 2
    assert [s["source_generation"] for s in stores[1].sessions("corpus-a")] == sorted(
        f"import:{h}" for h in (first["source_sha256"], grown["source_sha256"])
    )
    current = stores[1].sessions("corpus-a", current=True)
    assert [s["source_generation"] for s in current] == [f"import:{grown['source_sha256']}"]
    hits = stores[1].search(stores[0], GRANT, "one")
    assert [(hit["source_generation"], hit["sequence"]) for hit in hits] == [(current[0]["source_generation"], 1)]
    assert stores[1].export(stores[0], "corpus-a")["events"] == 3


def test_a_source_committed_before_its_mark_is_replayed_without_duplicates(stores, tmp_path):
    catalog, imports = stores
    source = root(tmp_path, files={"p/s-1.jsonl": claude("one")})
    data = (source.path / "p/s-1.jsonl").read_bytes()
    read = read_source("claude", data, ALIASES)
    request_events = [{**event} for event in read.events]
    digest = hashlib.sha256(data).hexdigest()
    importer_module.ingest(
        catalog,
        GRANT,
        importer_module.ingest_request("corpus-a", "corpus-a:claude:host-a:s-1", digest, request_events),
    )
    before = catalog.snapshot()
    replayed = only(run(stores, source))
    assert replayed["outcome"] == "replayed" and replayed["events"] == 1
    assert catalog.snapshot()["session_event"] == before["session_event"]
    assert imports.has("corpus-a", digest)


def test_a_dry_run_reports_what_would_import_and_writes_nothing(stores, tmp_path):
    report = run(stores, root(tmp_path, files={"p/s-1.jsonl": claude("one")}), dry_run=True)
    assert report["dry_run"] is True
    assert only(report)["outcome"] == "would_import" and only(report)["events"] == 1
    catalog, imports = stores
    assert catalog.snapshot() == {table: [] for table in catalog.snapshot()}
    assert imports.snapshot() == []


def test_a_source_with_no_records_is_reported_and_not_marked(stores, tmp_path):
    source = only(run(stores, root(tmp_path, files={"p/empty.jsonl": b'{"type":"progress"}\n{bad\n'})))
    assert source["outcome"] == "no_records" and source["events"] == 0
    assert stores[1].snapshot() == []


def test_a_refused_commit_is_reported_with_its_class_and_not_marked(stores, tmp_path, monkeypatch):
    def unavailable(batch):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(stores[0], "commit", unavailable)
    source = only(run(stores, root(tmp_path, files={"p/s-1.jsonl": claude("one")})))
    assert source["outcome"] == "refused" and source["error"] == "dependency_unavailable"
    assert stores[1].snapshot() == []


def test_missing_roots_absent_expected_sources_and_losses_are_reported(stores, tmp_path):
    present = root(
        tmp_path, files={"p/s.jsonl": claude("x") + b'{"cut'}, expected=("p/s.jsonl", "p/lost.jsonl", "q/gone.jsonl")
    )
    absent = SourceRoot("codex", "retired", tmp_path / "nowhere", "corpus-a")
    report = run(stores, present, absent)
    assert report["missing_roots"] == [{"provider": "codex", "installation": "retired"}]
    assert report["missing_sources"] == [
        {"provider": "claude", "installation": "host-a", "relative_path": path, "reason": "expected source is absent"}
        for path in ("p/lost.jsonl", "q/gone.jsonl")
    ]
    assert report["losses"] == [
        {
            "provider": "claude",
            "installation": "host-a",
            "relative_path": "p/s.jsonl",
            "byte_start": len(claude("x")),
            "byte_end": len(claude("x")) + 5,
            "reason": "truncated final line",
        }
    ]
    assert report["historical_import_unknown_attribution"] == 0
    assert report["importer_version"] == IMPORTER_VERSION


def test_a_source_that_vanishes_before_it_is_read_is_reported_missing(stores, tmp_path, monkeypatch):
    source = root(tmp_path, files={"p/s.jsonl": claude("x")})
    monkeypatch.setattr(importer_module, "_discover", lambda folder: [folder / "p/gone.jsonl"])
    report = run(stores, source)
    assert report["sources"] == []
    assert report["missing_sources"] == [
        {
            "provider": "claude",
            "installation": "host-a",
            "relative_path": "p/gone.jsonl",
            "reason": "source vanished before it was read",
        }
    ]


def test_the_import_ledger_creates_its_folder_and_reopens(tmp_path):
    path = tmp_path / "a/b/imports.sqlite3"
    ImportLedger(path).close()
    reopened = ImportLedger(path)
    assert reopened.snapshot() == []
    reopened.close()


def test_discovery_walks_nested_transcripts_in_order_and_skips_other_files(tmp_path):
    for name in ("b/2.jsonl", "a/sub/1.jsonl", "a/notes.txt", "c.jsonl"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("{}\n")
    (tmp_path / "d.jsonl").mkdir()
    found = importer_module._discover(tmp_path)
    assert [path.relative_to(tmp_path).as_posix() for path in found] == ["a/sub/1.jsonl", "b/2.jsonl", "c.jsonl"]


def test_discovery_skips_symlinks_and_anything_that_resolves_outside_the_root(tmp_path):
    folder, outside = tmp_path / "root", tmp_path / "personal"
    (folder / "p").mkdir(parents=True)
    outside.mkdir()
    (outside / "notes.jsonl").write_bytes(claude("private"))
    (folder / "p/own.jsonl").write_bytes(claude("own"))
    (folder / "p/linked.jsonl").symlink_to(outside / "notes.jsonl")
    (folder / "inner.jsonl").symlink_to(folder / "p/own.jsonl")
    (folder / "away").symlink_to(outside, target_is_directory=True)
    assert importer_module._discover(folder) == [folder / "p/own.jsonl"]


def test_the_report_counts_unknown_attribution_and_names_codex_sources(stores, tmp_path):
    data = line(type="session_meta", payload={"id": "cx", "cwd": "/nope"}) + line(
        type="response_item",
        payload={"type": "message", "role": "user", "content": [{"type": "input_text", "text": "q"}]},
    )
    report = run(stores, root(tmp_path, "codex", files={"r.jsonl": data}))
    assert report["historical_import_unknown_attribution"] == 2
    assert only(report)["session_uid"] == "corpus-a:codex:host-a:cx"


@pytest.mark.parametrize(
    ("roots", "aliases", "expected"),
    [
        (lambda p: [root(p, "copilot")], ALIASES, ("invalid_plan", "root 0 provider is not claude or codex")),
        (lambda p: [root(p)], {"app": PROJECT}, ("invalid_plan", "alias folders must be absolute paths")),
        (lambda p: [root(p)], {"/work/../app": PROJECT}, ("invalid_plan", "alias folders must be absolute paths")),
        (
            lambda p: [root(p)],
            {"/work/app": "github.com/x/y"},
            ("forbidden_scope", "an alias names a project outside the grant"),
        ),
        (
            lambda p: [root(p), root(p, corpus="personal", installation="laptop")],
            ALIASES,
            ("forbidden_scope", "root 1 is not approved for the granted corpus; nothing was imported"),
        ),
        (lambda p: [], ALIASES, ("invalid_plan", "a plan must name at least one source root")),
    ],
)
def test_an_invalid_or_out_of_scope_plan_is_refused_before_any_read_or_write(
    stores, tmp_path, roots, aliases, expected
):
    personal = root(tmp_path, files={"p/s.jsonl": claude("private taxes")}, corpus="personal", installation="laptop")
    assert personal.path.exists()
    report = run(stores, *roots(tmp_path), aliases=aliases)
    assert report == {"state": "rejected", "error": {"class": expected[0], "detail": expected[1]}}
    assert stores[0].snapshot() == {table: [] for table in stores[0].snapshot()}
    assert stores[1].snapshot() == []


def test_search_returns_imported_events_with_their_source_attribution(stores, tmp_path):
    data = claude("Find The Breaker", timestamp="t1") + claude("unrelated")
    only(run(stores, root(tmp_path, files={"p/s-1.jsonl": data})))
    catalog, imports = stores
    (hit,) = imports.search(catalog, GRANT, "the breaker")
    assert hit == {
        "session_uid": "corpus-a:claude:host-a:s-1",
        "source_generation": imports.sessions("corpus-a")[0]["source_generation"],
        "event_id": hit["event_id"],
        "sequence": 1,
        "byte_start": 0,
        "byte_end": len(claude("Find The Breaker", timestamp="t1")) - 1,
        "project_id": PROJECT,
        "record": {"kind": "user_text", "text": "Find The Breaker", "source_time": "t1"},
        "source": {
            "provider": "claude",
            "installation": "host-a",
            "relative_path": "p/s-1.jsonl",
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "importer_version": IMPORTER_VERSION,
            "unknown": ["provider_version"],
        },
    }
    assert imports.search(catalog, Grant("importer", "corpus-b", GRANT.projects), "breaker") == []
    assert imports.search(catalog, GRANT, "absent words") == []


def test_search_returns_only_unattributed_events_and_granted_projects(stores, tmp_path):
    data = claude("breaker here") + claude("breaker there", cwd="/work/lib") + claude("breaker free", cwd="/nope")
    only(
        run(
            stores, root(tmp_path, files={"p/s.jsonl": data}), aliases={**ALIASES, "/work/lib": "github.com/acme/other"}
        )
    )
    catalog, imports = stores
    assert [hit["project_id"] for hit in imports.search(catalog, GRANT, "breaker")] == [
        PROJECT,
        "github.com/acme/other",
        None,
    ]
    narrow = Grant("reader", "corpus-a", frozenset({"github.com/acme/other"}))
    assert [hit["record"]["text"] for hit in imports.search(catalog, narrow, "breaker")] == [
        "breaker there",
        "breaker free",
    ]
    assert [
        hit["record"]["text"] for hit in imports.search(catalog, Grant("r", "corpus-a", frozenset()), "breaker")
    ] == ["breaker free"]


def test_export_lists_every_projection_with_its_events_and_digest(stores, tmp_path):
    only(run(stores, root(tmp_path, files={"p/s-1.jsonl": claude("one") + claude("two")})))
    export = stores[1].export(stores[0], "corpus-a")
    assert export["access_domain"] == "corpus-a"
    assert export["sessions"] == 1 and export["events"] == 2
    assert export["digest"] == importer_module.digest(export["records"])
    assert export["records"][0]["provenance"]["relative_path"] == "p/s-1.jsonl"
    assert [event["sequence"] for event in export["records"][0]["events"]] == [1, 2]


def test_retire_writes_a_full_export_first_and_removes_only_import_projections(stores, tmp_path):
    only(run(stores, root(tmp_path, files={"p/s-1.jsonl": claude("one") + claude("two")})))
    catalog, imports = stores
    expected = imports.export(catalog, "corpus-a")
    events = catalog.snapshot()["session_event"]
    destination = tmp_path / "rollback/deep/export.json"
    assert imports.retire(catalog, "corpus-a", destination) == {
        "state": "retired",
        "catalog_events_kept": 2,
        "export": {"digest": expected["digest"], "sessions": 1, "events": 2},
    }
    assert json.loads(destination.read_text(encoding="utf-8")) == expected
    assert imports.sessions("corpus-a") == [] and imports.search(catalog, GRANT, "one") == []
    assert catalog.snapshot()["session_event"] == events
    assert imports.unknown_attribution("corpus-a") == 0


def test_retire_holds_the_import_ledger_write_lock_from_export_to_delete(stores, tmp_path, monkeypatch):
    only(run(stores, root(tmp_path, files={"p/s-1.jsonl": claude("one")})))
    catalog, imports = stores
    seen = []
    write = importer_module._write_durably

    def observed(destination, text):
        seen.append(imports.db.in_transaction)
        write(destination, text)

    monkeypatch.setattr(importer_module, "_write_durably", observed)
    assert imports.retire(catalog, "corpus-a", tmp_path / "export.json")["state"] == "retired"
    assert seen == [True] and not imports.db.in_transaction
    monkeypatch.setattr(importer_module, "_write_durably", lambda destination, text: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        imports.retire(catalog, "corpus-a", tmp_path / "other.json")
    assert not imports.db.in_transaction


def test_retire_refuses_an_existing_destination_and_a_mismatched_written_export(stores, tmp_path, monkeypatch):
    only(run(stores, root(tmp_path, files={"p/s-1.jsonl": claude("one")})))
    catalog, imports = stores
    taken = tmp_path / "taken.json"
    taken.write_text("keep")
    assert imports.retire(catalog, "corpus-a", taken) == {
        "state": "rejected",
        "error": {"class": "export_exists", "detail": "the export destination already exists; nothing was retired"},
    }
    assert taken.read_text() == "keep"
    monkeypatch.setattr(Path, "read_bytes", lambda self: b'{"records": []}')
    assert imports.retire(catalog, "corpus-a", tmp_path / "fresh.json") == {
        "state": "rejected",
        "error": {
            "class": "export_mismatch",
            "detail": "the written export does not match the current import projections; it was kept and nothing was retired",
        },
    }
    monkeypatch.undo()
    assert len(imports.sessions("corpus-a")) == 1


def test_import_ledgers_keep_access_domains_apart(stores, tmp_path):
    digest = only(run(stores, root(tmp_path, files={"p/s.jsonl": claude("x", cwd="/nope")})))["source_sha256"]
    imports = stores[1]
    assert imports.has("corpus-a", digest) and imports.unknown_attribution("corpus-a") == 1
    assert not imports.has("corpus-b", digest)
    assert imports.sessions("corpus-b") == [] and imports.unknown_attribution("corpus-b") == 0


def cli_args(tmp_path, folder):
    return [
        "--catalog", str(tmp_path / "c.sqlite3"),
        "--imports", str(tmp_path / "i.sqlite3"),
        "--corpus", "corpus-a",
        "--principal", "operator",
        "--project", PROJECT,
        "--project", "p=q",
        "--root", f"claude:host-a:{folder}",
        "--alias", f"/work/app={PROJECT}",
        "--alias", "/work/lib=p=q",
        "--expected", "claude:host-a:p/lost.jsonl",
    ]  # fmt: skip


def printed(capsys):
    out = capsys.readouterr().out
    report = json.loads(out)
    assert out == json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    return report


def test_the_cli_imports_explicit_roots_and_prints_the_report(tmp_path, capsys):
    folder = tmp_path / "claude:roots"
    (folder / "p").mkdir(parents=True)
    (folder / "p/sé-1.jsonl").write_bytes(claude("one") + claude("two", cwd="/work/lib"))
    args = cli_args(tmp_path, folder)
    assert cli.main([*args, "--dry-run"]) == 0
    assert only(printed(capsys))["outcome"] == "would_import"
    assert cli.main(args) == 0
    report = printed(capsys)
    assert report["principal"] == "operator"
    assert {key: only(report)[key] for key in ("provider", "installation", "relative_path", "outcome")} == {
        "provider": "claude",
        "installation": "host-a",
        "relative_path": "p/sé-1.jsonl",
        "outcome": "committed",
    }
    assert report["missing_sources"] == [
        {
            "provider": "claude",
            "installation": "host-a",
            "relative_path": "p/lost.jsonl",
            "reason": "expected source is absent",
        }
    ]
    imports = ImportLedger(tmp_path / "i.sqlite3")
    assert imports.sessions("corpus-a")[0]["provenance"]["projects"] == [PROJECT, "p=q"]
    imports.close()
    assert cli.main([*args[:-4], "--alias", f"app={PROJECT}"]) == 2
    assert printed(capsys)["error"]["class"] == "invalid_plan"
    retire = [*args[:8], "--retire", str(tmp_path / "export.json")]
    assert cli.main(retire) == 0
    assert printed(capsys)["export"]["sessions"] == 1
    assert cli.main(retire) == 2
    assert printed(capsys)["error"]["class"] == "export_exists"


def test_the_cli_exits_two_when_a_source_is_refused(tmp_path, capsys, monkeypatch):
    folder = tmp_path / "claude"
    (folder / "p").mkdir(parents=True)
    (folder / "p/s-1.jsonl").write_bytes(claude("one"))

    def unavailable(self, batch):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Catalog, "commit", unavailable)
    base = ["--catalog", str(tmp_path / "c"), "--imports", str(tmp_path / "i"), "--corpus", "corpus-a"]
    assert cli.main([*base, "--principal", "p", "--root", f"claude:h:{folder}"]) == 2
    assert only(printed(capsys))["outcome"] == "refused"


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        (["--root", "claude:host-a"], "--root must be PROVIDER:INSTALLATION:PATH"),
        (["--root", "claude::folder"], "--root must be PROVIDER:INSTALLATION:PATH"),
        (["--expected", "claude:x"], "--expected must be PROVIDER:INSTALLATION:PATH"),
        (["--expected", "codex:h:lost.jsonl"], "--expected names a root that no --root declares"),
        (["--expected", "claude:other:lost.jsonl"], "--expected names a root that no --root declares"),
        (["--alias", "noequals"], "--alias must be FOLDER=PROJECT"),
        (["--alias", "=project"], "--alias must be FOLDER=PROJECT"),
        (["--alias", "/folder="], "--alias must be FOLDER=PROJECT"),
        (["--retire", "export.json"], "--retire takes no --dry-run or --root"),
    ],
)
def test_the_cli_refuses_malformed_arguments(tmp_path, capsys, bad, message):
    base = ["--catalog", "c", "--imports", "i", "--corpus", "c", "--principal", "p", "--root", f"claude:h:{tmp_path}"]
    with pytest.raises(SystemExit) as raised:
        cli.main([*base, *bad])
    assert raised.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith(f"error: {message}")


def test_the_cli_refuses_a_retire_with_a_dry_run(capsys):
    with pytest.raises(SystemExit):
        cli.main(
            ["--catalog", "c", "--imports", "i", "--corpus", "c", "--principal", "p", "--retire", "e", "--dry-run"]
        )
    assert capsys.readouterr().err.splitlines()[-1].endswith("error: --retire takes no --dry-run or --root")


@pytest.mark.parametrize("missing", ["--catalog", "--imports", "--corpus", "--principal"])
def test_the_cli_requires_its_stores_corpus_and_principal(missing, capsys):
    values = {"--catalog": "c", "--imports": "i", "--corpus": "k", "--principal": "p"}
    argv = [part for name, value in values.items() if name != missing for part in (name, value)]
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 2
    assert missing in capsys.readouterr().err


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_ses05_cases.run_case(case), sv2_ses05_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, digest in sv2_ses05_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == digest
    assert manifest["cases"] == ["T-SV2-SES-05-A", "T-SV2-SES-05-B", "T-SV2-SES-05-C"]
    assert result["package"] == "SV2-SES-05"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
