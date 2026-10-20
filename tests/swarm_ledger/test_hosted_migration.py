import base64
import copy
import gzip
import hashlib
import io
import json
import secrets
import sqlite3
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_core
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from scripts.swarm_ledger.storage_migration import hosted
from tests.swarm_ledger.api_fakes import as_services
from tests.swarm_ledger.test_sqlite import document


@pytest.fixture
def stores(tmp_path):
    source = SQLiteLedgerRepository(tmp_path / "source" / DATABASE, ledger_core)
    target = SQLiteLedgerRepository(tmp_path / "target" / DATABASE, ledger_core)
    source.import_document("migration-proof", document())
    with source.connect() as connection, connection:
        source.save_registry(connection, "bin", {"migration-proof": 42})
    data = b"An artifact kept with its ledger."
    name = hashlib.sha256(data).hexdigest() + ".md"
    folder = source.directory / "migration-proof.media"
    folder.mkdir()
    (folder / name).write_bytes(data)
    return source, target


def test_complete_history_bin_and_files_survive_an_idempotent_import(stores):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    result = hosted.restore(target, snapshot)
    assert target.export_document("migration-proof") == document()
    assert target.get_document("migration-proof")["_meta"]["seeds"] == {}
    assert hosted.capture(target, "migration-proof") == snapshot
    assert hosted.restore(target, snapshot) == result
    assert result["tasks"] == 2
    assert result["events"] == 1
    assert result["revisions"] == 2
    assert result["artifacts"] == 1
    assert result["files"] == 1
    with target.connect() as connection:
        assert target.registry("bin", connection)["migration-proof"] == 42


def test_conflicting_existing_ledger_is_never_replaced(stores):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    target.import_document("migration-proof", {**document(), "title": "Hosted work"})
    with pytest.raises(ValueError, match="^hosted ledger conflict$"):
        hosted.restore(target, snapshot)
    assert target.export_document("migration-proof")["title"] == "Hosted work"


@pytest.mark.parametrize("invalid", ["../escape.md", "a" * 64 + ".md"])
def test_invalid_file_identity_cannot_create_a_ledger(stores, invalid):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    snapshot["files"] = {invalid: base64.b64encode(b"different bytes").decode()}
    with pytest.raises(ValueError):
        hosted.restore(target, snapshot)
    assert not target.exists("migration-proof")


def test_interrupted_file_copy_can_resume_without_losing_history(stores, monkeypatch):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    original = hosted.write_files
    with monkeypatch.context() as patch:
        patch.setattr(hosted, "write_files", lambda *args: (_ for _ in ()).throw(OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            hosted.restore(target, snapshot)
    assert hosted.write_files is original
    folder = target.directory / "migration-proof.media"
    folder.mkdir()
    pending = folder / (next(iter(snapshot["files"])) + ".part")
    pending.write_bytes(b"partial copy")
    hosted.restore(target, snapshot)
    assert hosted.capture(target, "migration-proof") == snapshot


def test_fence_blocks_new_connections_and_preserves_other_ledgers(stores):
    source, _ = stores
    before = hosted.capture(source, "migration-proof")
    hosted.fence(source, "migration-proof", "https://hosted.example")
    reopened = SQLiteLedgerRepository(source.path, ledger_core)
    assert hosted.capture(reopened, "migration-proof") == before
    with pytest.raises(sqlite3.IntegrityError, match="hosted"):
        reopened.import_document("migration-proof", document(), replace=True)
    with reopened.connect() as connection, pytest.raises(sqlite3.IntegrityError, match="hosted"):
        connection.execute("DELETE FROM events WHERE slug=?", ("migration-proof",))
    with reopened.connect() as connection, pytest.raises(sqlite3.IntegrityError, match="hosted"):
        reopened.save_registry(connection, "bin", {})
    reopened.import_document("still-local", document())
    assert reopened.export_document("still-local") == document()
    hosted.fence(reopened, "migration-proof", "https://hosted.example")
    with pytest.raises(ValueError, match="^ledger already has a different hosted destination$"):
        hosted.fence(reopened, "migration-proof", "https://other.example")


def request(snapshot, credential):
    encoded = gzip.compress(json.dumps(snapshot).encode())
    return SimpleNamespace(
        path="/api/v1/migrations/migration-proof",
        command="POST",
        headers={
            "Host": "hosted.example",
            "X-Ledger-Migration": credential,
            "Content-Type": "application/gzip",
            "Content-Length": str(len(encoded)),
        },
        rfile=io.BytesIO(encoded),
    )


def test_expiring_import_authority_is_bound_to_the_ledger_and_scope(stores, monkeypatch):
    source, target = stores
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    credential = hosted.token(secret, "migration-proof", 1100)
    snapshot = hosted.capture(source, "migration-proof")
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    assert routes.dispatch(request(snapshot, credential), as_services(server))["data"] == hosted.manifest(snapshot)
    for invalid in (
        hosted.token(secret, "other-ledger", 1100),
        hosted.token(secret, "migration-proof", 999),
        hosted.token(secret, "migration-proof", 10000),
        "invalid",
        "1100.é",
    ):
        with pytest.raises(APIError) as failure:
            routes.dispatch(request(snapshot, invalid), as_services(server))
        assert failure.value.status == 403
    wrong = copy.deepcopy(snapshot)
    wrong["slug"] = "other-ledger"
    with pytest.raises(APIError) as failure:
        routes.dispatch(request(wrong, credential), as_services(server))
    assert failure.value.status == 400


def test_manifest_changes_when_history_or_media_changes(stores):
    source, _ = stores
    snapshot = hosted.capture(source, "migration-proof")
    expected = hosted.manifest(snapshot)
    changed = copy.deepcopy(snapshot)
    changed["state"]["_meta"]["events"][0]["unknown"]["x"] = False
    assert hosted.manifest(changed)["digest"] != expected["digest"]
    changed = copy.deepcopy(snapshot)
    changed["files"].clear()
    assert hosted.manifest(changed)["digest"] != expected["digest"]


def test_frozen_uploads_and_cleanup_preserve_fallback_files(stores, monkeypatch):
    from scripts.swarm_ledger import ledger_artifacts, ledger_media

    source, _ = stores
    monkeypatch.setattr(ledger_media.core, "LEDGER_DIR", source.directory)
    hosted.fence(source, "migration-proof", "https://hosted.example")
    folder = source.directory / "migration-proof.media"
    existing = next(folder.iterdir())
    import os

    os.utime(existing, (0, 0))
    with pytest.raises(ledger_media.Refused) as refusal:
        ledger_media.write_file(folder / ("a" * 64 + ".md"), b"new content")
    assert refusal.value.status == 409
    assert str(refusal.value) == "ledger moved to hosted service"
    with pytest.raises(ledger_artifacts.media.Refused, match="hosted"):
        ledger_artifacts.store("migration-proof", "new.md", b"new content")
    ledger_media.sweep("migration-proof", set(), 10**12)
    ledger_media.purge("migration-proof")
    doc = {
        **{name: [] for name in ledger_artifacts.core.THREADS},
        "notes": [],
        "chat": [],
        "artifacts": [],
        "artifact_trash": [{"file": {"id": existing.name}, "deleted_at": 0}],
    }
    before = copy.deepcopy(doc)
    ledger_artifacts.sweep("migration-proof", doc, SimpleNamespace(at=10**12, dropped=[], dirty=False))
    assert doc == before
    assert existing.read_bytes() == b"An artifact kept with its ledger."
    ledger_media.write_file(source.directory / "another-ledger.media" / ("b" * 64 + ".md"), b"allowed")


def test_hosted_command_requires_source_fencing_before_transfer(monkeypatch, tmp_path):
    import sys

    monkeypatch.setattr(sys, "argv", ["migration", "--source", str(tmp_path), "--url", "https://hosted.example"])
    with pytest.raises(SystemExit, match="2"):
        hosted.main()


def test_migration_inventory_prints_only_the_source_manifest(stores, monkeypatch, capsys):
    import sys

    source, _ = stores
    monkeypatch.setattr(sys, "argv", ["migration", "--source", str(source.directory)])
    with source.connect() as connection:
        before = connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
    hosted.main()
    assert json.loads(capsys.readouterr().out) == hosted.manifest(hosted.capture(source, "migration-proof"))
    with source.connect() as connection:
        assert connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall() == before


def test_transfer_verifies_a_separate_read_and_blocks_redirects(stores, monkeypatch):
    source, _ = stores
    snapshot = hosted.capture(source, "migration-proof")
    secret = secrets.token_hex(32)
    expected = hosted.manifest(snapshot)
    calls = []
    answers = [expected, expected]

    def opened(req, timeout):
        calls.append((req, timeout))
        return io.BytesIO(json.dumps({"data": answers.pop(0)}).encode())

    def opener(handler):
        assert handler is hosted.NoRedirect
        assert handler().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.example") is None
        return SimpleNamespace(open=opened)

    monkeypatch.setattr(hosted.urllib.request, "build_opener", opener)
    monkeypatch.setenv("SWARM_ACCESS_CLIENT_ID", "$FIXTURE_ACCESS_ID")
    monkeypatch.setenv("SWARM_ACCESS_CLIENT_SECRET", "$FIXTURE_ACCESS_SECRET")
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    result = hosted.transfer("https://hosted.example/prefixX/", snapshot, secret)
    assert result == {"source": expected, "hosted": expected, "match": True}
    assert [req.get_method() for req, _ in calls] == ["POST", "GET"]
    assert all(timeout == 300 for _, timeout in calls)
    assert all(req.full_url == "https://hosted.example/prefixX/api/v1/migrations/migration-proof" for req, _ in calls)
    first = calls[0][0]
    assert json.loads(gzip.decompress(first.data)) == snapshot
    assert first.get_header("Cf-access-client-id") == "$FIXTURE_ACCESS_ID"
    assert first.get_header("Cf-access-client-secret") == "$FIXTURE_ACCESS_SECRET"
    assert first.get_header("User-agent") == "agentihooks-swarm"
    assert all(req.get_header("Content-type") == "application/gzip" for req, _ in calls)
    assert all(req.get_header("X-ledger-migration") == first.get_header("X-ledger-migration") for req, _ in calls)
    assert hosted.authorize(secret, "migration-proof", first.get_header("X-ledger-migration"), 1000)
    wrong = {**expected, "events": 999}
    for received in ([expected, wrong], [wrong, expected], [wrong, wrong]):
        answers.extend(received)
        with pytest.raises(ValueError, match="^hosted comparison differs; do not cut over$"):
            hosted.transfer("https://hosted.example", snapshot, secret)
    monkeypatch.delenv("SWARM_ACCESS_CLIENT_ID")
    monkeypatch.delenv("SWARM_ACCESS_CLIENT_SECRET")
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "$FALLBACK_ACCESS_ID")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "$FALLBACK_ACCESS_SECRET")
    answers.extend([expected, expected])
    hosted.transfer("https://hosted.example", snapshot, secret)
    assert calls[-1][0].get_header("Cf-access-client-id") == "$FALLBACK_ACCESS_ID"
    assert calls[-1][0].get_header("Cf-access-client-secret") == "$FALLBACK_ACCESS_SECRET"
    monkeypatch.delenv("CF_ACCESS_CLIENT_ID")
    monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET")
    answers.extend([expected, expected])
    hosted.transfer("https://hosted.example", snapshot, secret)
    assert calls[-1][0].get_header("Cf-access-client-id") == ""
    assert calls[-1][0].get_header("Cf-access-client-secret") == ""


def test_command_freezes_source_before_transferring_and_reports_comparison(stores, monkeypatch, capsys):
    import sys

    source, _ = stores
    source.import_document("still-local", document())
    snapshot = hosted.capture(source, "migration-proof")
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migration",
            "--source",
            str(source.directory),
            "--url",
            "https://hosted.example",
            "--freeze",
            "--slug",
            "migration-proof",
        ],
    )

    def transfer(url, actual, supplied):
        assert url == "https://hosted.example"
        assert actual == snapshot
        assert supplied == secret
        with pytest.raises(sqlite3.IntegrityError):
            source.import_document("migration-proof", document(), replace=True)
        return {"match": True}

    monkeypatch.setattr(hosted, "transfer", transfer)
    hosted.main()
    assert json.loads(capsys.readouterr().out) == {"match": True}
    source.import_document("still-local", document(), replace=True)


def test_each_ledger_comparison_is_visible_before_the_next_transfer(stores, monkeypatch):
    import sys

    source, _ = stores
    source.import_document("second-proof", document())
    monkeypatch.setenv("SWARM_SECRET", secrets.token_hex(32))
    monkeypatch.setattr(
        sys, "argv", ["migration", "--source", str(source.directory), "--url", "https://hosted.example", "--freeze"]
    )
    transport = io.BytesIO()
    output = io.TextIOWrapper(transport, encoding="utf-8")
    seen = []

    def transfer(url, snapshot, secret):
        assert len(transport.getvalue().splitlines()) == len(seen)
        seen.append(snapshot["slug"])
        return {"slug": snapshot["slug"], "match": True}

    monkeypatch.setattr(hosted, "transfer", transfer)
    with monkeypatch.context() as capture:
        capture.setattr(sys, "stdout", output)
        hosted.main()
        rows = [json.loads(line) for line in transport.getvalue().splitlines()]
    assert seen == ["migration-proof", "second-proof"]
    assert rows == [{"slug": slug, "match": True} for slug in seen]


def test_whole_store_migration_makes_the_local_fallback_read_only(stores, monkeypatch):
    import sys

    source, _ = stores
    original_fence = hosted.fence
    transferred = []

    def fence(repo, slug, destination):
        if slug == "*":
            source.import_document("racing-ledger", document())
        original_fence(repo, slug, destination)

    monkeypatch.setattr(hosted, "fence", fence)
    monkeypatch.setenv("SWARM_SECRET", secrets.token_hex(32))
    monkeypatch.setattr(
        sys, "argv", ["migration", "--source", str(source.directory), "--url", "https://hosted.example", "--freeze"]
    )

    def transfer(url, snapshot, secret):
        with pytest.raises(sqlite3.IntegrityError, match="hosted"):
            source.import_document("late-ledger", document())
        with source.connect() as connection, pytest.raises(sqlite3.IntegrityError, match="hosted"):
            source.save_registry(connection, "bin", {**source.registry("bin", connection), "late-ledger": 42})
        with hosted.media_mutation(source.directory, "late-ledger") as writable:
            assert not writable
        transferred.append(snapshot["slug"])
        return {"match": True}

    monkeypatch.setattr(hosted, "transfer", transfer)
    hosted.main()
    assert transferred == ["migration-proof", "racing-ledger"]


def test_whole_store_freeze_refuses_any_foreign_destination(stores):
    source, _ = stores
    source.import_document("second-proof", document())
    hosted.fence(source, "migration-proof", "https://hosted.example")
    hosted.fence(source, "second-proof", "https://another.example")
    with pytest.raises(ValueError, match="^ledger already has a different hosted destination$"):
        hosted.fence(source, "*", "https://hosted.example")
    source.import_document("still-local", document())
    assert source.export_document("still-local") == document()


def test_server_secret_is_loaded_from_openbao_only_in_memory(monkeypatch, capsys):
    secret = secrets.token_hex(32)
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.setenv("OPENBAO_URL", "https://vault.example/prefixX/")
    monkeypatch.setenv("OPENBAO_TOKEN", "$FIXTURE_OPENBAO_TOKEN")
    calls = []

    def opened(req, timeout):
        calls.append((req, timeout))
        return io.BytesIO(json.dumps({"data": {"data": {"SWARM_SECRET": secret}}}).encode())

    def opener(handler):
        assert handler is hosted.NoRedirect
        return SimpleNamespace(open=opened)

    monkeypatch.setattr(hosted.urllib.request, "build_opener", opener)
    assert hosted.server_secret() == secret
    assert "SWARM_SECRET" not in hosted.os.environ
    assert calls[0][0].full_url == "https://vault.example/prefixX/v1/secret/data/k8s/swarm"
    assert calls[0][0].get_header("X-vault-token") == "$FIXTURE_OPENBAO_TOKEN"
    assert calls[0][1] == 30
    assert capsys.readouterr().out == ""


def test_missing_trusted_credentials_never_freezes_the_source(stores, monkeypatch):
    import sys

    source, _ = stores
    monkeypatch.setattr(
        sys, "argv", ["migration", "--source", str(source.directory), "--url", "https://hosted.example", "--freeze"]
    )

    def missing():
        raise ValueError("trusted credentials unavailable")

    monkeypatch.setattr(hosted, "server_secret", missing, raising=False)
    with pytest.raises(ValueError, match="trusted credentials"):
        hosted.main()
    source.import_document("migration-proof", document(), replace=True)


def test_inventory_pins_every_count_and_the_complete_content_digest(stores):
    source, _ = stores
    snapshot = hosted.capture(source, "migration-proof")
    digest = hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert hosted.manifest(snapshot) == {
        "slug": "migration-proof",
        "revision": 1,
        "tasks": 2,
        "events": 1,
        "revisions": 2,
        "artifacts": 1,
        "artifact_trash": 1,
        "files": 1,
        "registries": 1,
        "digest": digest,
    }


def test_import_authority_expires_at_the_boundary_and_cannot_be_extended():
    secret = secrets.token_hex(32)
    assert hosted.authorize(secret, "ledger", hosted.token(secret, "ledger", 1900), 1000)
    for expires in (1000, 1901):
        assert not hosted.authorize(secret, "ledger", hosted.token(secret, "ledger", expires), 1000)
    assert not hosted.authorize("", "ledger", hosted.token("", "ledger", 1100), 1000)
    assert not hosted.authorize(secret, "ledger", None, 1000)


@pytest.mark.parametrize(
    "case,status",
    [("method", 405), ("type", 415), ("length", 400), ("large", 400), ("gzip", 400), ("json", 400), ("slug", 404)],
)
def test_migration_api_rejects_invalid_inputs_without_creating_a_ledger(stores, monkeypatch, case, status):
    source, target = stores
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    incoming = request(hosted.capture(source, "migration-proof"), hosted.token(secret, "migration-proof", 1100))
    if case == "method":
        incoming.command = "DELETE"
    elif case == "type":
        incoming.headers["Content-Type"] = "text/plain"
    elif case == "length":
        incoming.headers["Content-Length"] = "invalid"
    elif case == "large":
        incoming.headers["Content-Length"] = str(hosted.MAX_COMPRESSED + 1)
    elif case == "gzip":
        incoming.rfile = io.BytesIO(b"invalid gzip")
    elif case == "json":
        incoming.rfile = io.BytesIO(gzip.compress(b"{}"))
    else:
        incoming.path += "/unexpected"
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    with pytest.raises(APIError) as failure:
        routes.dispatch(incoming, as_services(server))
    assert failure.value.status == status
    codes = {400: "migration_invalid", 404: "resource_missing", 405: "method_not_allowed", 415: "content_type"}
    messages = {
        400: "Migration refused: invalid data or existing conflict",
        404: "No such migration",
        405: "Use GET or POST for migration",
        415: "Migration requires application/gzip",
    }
    assert failure.value.envelope() == {"error": {"code": codes[status], "message": messages[status]}}
    assert not target.exists("migration-proof")


def test_migration_readback_reports_the_actual_hosted_document(stores, monkeypatch):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    hosted.restore(target, snapshot)
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    incoming = request(snapshot, hosted.token(secret, "migration-proof", 1100))
    incoming.command = "GET"
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    assert routes.dispatch(incoming, as_services(server)) == {"data": hosted.manifest(snapshot)}


def test_source_inventory_refuses_symbolic_links_and_missing_databases(stores, tmp_path):
    source, _ = stores
    folder = source.directory / "migration-proof.media"
    existing = next(folder.iterdir())
    (folder / ("a" * 64 + ".md")).symlink_to(existing)
    with pytest.raises(ValueError, match="^ledger media must contain regular files$"):
        hosted.capture(source, "migration-proof")
    with pytest.raises(ValueError, match="^no ledger database$"):
        hosted.capture(SQLiteLedgerRepository(tmp_path / "missing" / DATABASE, ledger_core), "migration-proof")


def test_import_refuses_corrupt_files_and_cross_ledger_registry_entries(stores):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    name = next(iter(snapshot["files"]))
    corrupt = copy.deepcopy(snapshot)
    corrupt["files"][name] = "!bad-base64!"
    with pytest.raises(ValueError):
        hosted.restore(target, corrupt)
    corrupt = copy.deepcopy(snapshot)
    corrupt["registries"][0][1] = "another-ledger"
    with pytest.raises(ValueError, match="registry"):
        hosted.restore(target, corrupt)
    folder = target.directory / "migration-proof.media"
    folder.mkdir(parents=True)
    (folder / name).write_bytes(b"hosted file must remain")
    with pytest.raises(ValueError, match="conflict"):
        hosted.restore(target, snapshot)
    assert (folder / name).read_bytes() == b"hosted file must remain"
    assert not target.exists("migration-proof")


def test_unknown_source_ledger_cannot_be_fenced(stores):
    source, _ = stores
    with pytest.raises(ValueError, match="^no such ledger$"):
        hosted.fence(source, "absent", "https://hosted.example")


def test_fences_cover_tables_added_after_previous_migrations(stores):
    source, _ = stores
    hosted.fence(source, "migration-proof", "https://hosted.example")
    source.import_document("another-ledger", document())
    with source.connect() as connection, connection:
        connection.execute("CREATE TABLE metadata_without_slug (value TEXT)")
        connection.execute("CREATE TABLE later_rows (slug TEXT, value TEXT)")
        connection.execute("INSERT INTO later_rows VALUES (?,?)", ("another-ledger", "kept"))
    hosted.fence(source, "another-ledger", "https://hosted.example")
    with source.connect() as connection, pytest.raises(sqlite3.IntegrityError, match="hosted"):
        connection.execute("DELETE FROM later_rows WHERE slug=?", ("another-ledger",))


def test_tick_import_credentials_are_bound_to_the_signed_expiry():
    value = secrets.token_hex(32)
    credential = hosted.token(value, "ledger", 1100)
    changed = "1200." + credential.split(".")[1]
    assert not hosted.authorize(value, "ledger", changed, 1000)


def test_base64_transport_rejects_noise_even_when_decoded_content_has_the_right_hash(stores):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    name = next(iter(snapshot["files"]))
    snapshot["files"][name] += "!"
    with pytest.raises(ValueError):
        hosted.restore(target, snapshot)
    assert not target.exists("migration-proof")


def test_import_identity_rejects_independently_invalid_versions_and_slugs(stores):
    source, target = stores
    for key, value in (("version", 2), ("slug", "../escape")):
        snapshot = hosted.capture(source, "migration-proof")
        snapshot[key] = value
        with pytest.raises(ValueError, match="^invalid migration identity$"):
            hosted.restore(target, snapshot)


def test_history_verification_rolls_back_a_corrupt_import(stores, monkeypatch):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    original = target._insert

    def corrupt(connection, slug, state, token, seeds):
        original(connection, slug, {**state, "title": "Corrupt"}, token, seeds)

    monkeypatch.setattr(target, "_insert", corrupt)
    with pytest.raises(ValueError, match="^imported history differs from source$"):
        hosted.restore(target, snapshot)
    assert not target.exists("migration-proof")


def test_import_without_history_preserves_the_absence_of_seed_metadata(stores):
    source, target = stores
    state = document()
    del state["_meta"]["seeds"]
    source.import_document("without-history", state)
    snapshot = hosted.capture(source, "without-history")
    assert hosted.restore(target, snapshot)["revisions"] == 0
    assert target.export_document("without-history") == state


def test_migration_limits_accept_exact_boundaries_and_leave_the_next_request_unread(stores, monkeypatch):
    source, target = stores
    snapshot = hosted.capture(source, "migration-proof")
    value = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", value)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    incoming = request(snapshot, hosted.token(value, "migration-proof", 1100))
    encoded = incoming.rfile.getvalue()
    monkeypatch.setattr(hosted, "MAX_COMPRESSED", len(encoded))
    monkeypatch.setattr(hosted, "MAX_EXPANDED", len(gzip.decompress(encoded)))
    incoming.rfile = io.BytesIO(encoded + b"GET /next HTTP/1.1\r\n\r\n")
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    assert routes.dispatch(incoming, as_services(server)) == {"data": hosted.manifest(snapshot)}
    assert incoming.rfile.read() == b"GET /next HTTP/1.1\r\n\r\n"


def test_one_byte_import_body_is_consumed_before_the_next_request(stores, monkeypatch):
    source, target = stores
    secret = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", secret)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    snapshot = hosted.capture(source, "migration-proof")
    handler = request(snapshot, hosted.token(secret, "migration-proof", 1100))
    handler.headers["Content-Length"] = "1"
    handler.rfile = io.BytesIO(b"\x1fNEXT")
    server = SimpleNamespace(repository=target, core=ledger_core)
    with pytest.raises(APIError) as failure:
        hosted.route(handler, as_services(server), ["migrations", "migration-proof"])
    assert failure.value.status == 400
    assert handler.rfile.read() == b"NEXT"


def test_missing_server_secret_never_accepts_a_predictable_default_key(stores, monkeypatch):
    source, target = stores
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    incoming = request(hosted.capture(source, "migration-proof"), hosted.token("XXXX", "migration-proof", 1100))
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    with pytest.raises(APIError) as failure:
        routes.dispatch(incoming, as_services(server))
    assert failure.value.status == 403
    assert failure.value.envelope() == {
        "error": {"code": "forbidden", "message": "Missing or expired migration credential"}
    }


@pytest.mark.parametrize("missing", ["OPENBAO_URL", "OPENBAO_TOKEN"])
def test_missing_openbao_settings_are_refused_before_any_network_access(monkeypatch, missing):
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.setenv("OPENBAO_URL", "https://vault.example/prefixX/")
    monkeypatch.setenv("OPENBAO_TOKEN", "$FIXTURE_OPENBAO_TOKEN")
    monkeypatch.delenv(missing)

    def unexpected(*args):
        raise AssertionError("network access before credential validation")

    monkeypatch.setattr(hosted.urllib.request, "build_opener", unexpected)
    with pytest.raises(ValueError, match="^trusted OpenBao credentials unavailable$"):
        hosted.server_secret()


def test_missing_openbao_record_value_names_the_record_without_values(monkeypatch):
    monkeypatch.delenv("SWARM_SECRET", raising=False)
    monkeypatch.setenv("OPENBAO_URL", "https://vault.example")
    monkeypatch.setenv("OPENBAO_TOKEN", "$FIXTURE_OPENBAO_TOKEN")
    monkeypatch.setattr(
        hosted.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: io.BytesIO(b'{"data":{"data":{}}}')),
    )
    with pytest.raises(ValueError, match="^OpenBao record k8s/swarm has no server secret$"):
        hosted.server_secret()


@pytest.mark.parametrize(
    "arguments,message",
    [
        ([], "the following arguments are required: --source"),
        (["--url", "http://hosted.example"], "hosted destination must use HTTPS"),
        (["--freeze"], "freezing requires a hosted destination"),
        (["--url", "https://hosted.example"], "hosted transfers require source fencing with --freeze"),
    ],
)
def test_migration_command_errors_are_actionable_and_exact(tmp_path, monkeypatch, capsys, arguments, message):
    import sys

    supplied = arguments if not arguments else ["--source", str(tmp_path), *arguments]
    monkeypatch.setattr(sys, "argv", ["migration", *supplied])
    with pytest.raises(SystemExit) as failure:
        hosted.main()
    assert failure.value.code == 2
    assert capsys.readouterr().err.endswith(f"migration: error: {message}\n")


def test_migration_decompression_never_reads_beyond_its_budget(stores, monkeypatch):
    source, target = stores
    value = secrets.token_hex(32)
    monkeypatch.setenv("SWARM_SECRET", value)
    monkeypatch.setattr(hosted.time, "time", lambda: 1000)
    monkeypatch.setattr(hosted, "MAX_EXPANDED", 16)
    incoming = request(hosted.capture(source, "migration-proof"), hosted.token(value, "migration-proof", 1100))

    class Expanded:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit is not None and 0 < limit <= 17
            return b"x" * min(1000, limit)

    monkeypatch.setattr(hosted.gzip, "GzipFile", lambda **kwargs: Expanded())
    server = SimpleNamespace(
        ALLOWED_HOSTS={"hosted.example"}, ALLOWED_ORIGINS=set(), repository=target, core=ledger_core
    )
    with pytest.raises(APIError) as failure:
        routes.dispatch(incoming, as_services(server))
    assert failure.value.status == 400
    assert not target.exists("migration-proof")
