import json
import sqlite3

import pytest

from scripts.swarm_ledger import ledger_core, storage
from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository
from scripts.swarm_ledger.storage_migration import shared
from tests.swarm_ledger.test_sqlite import document

pytestmark = pytest.mark.unit

SLUG = "conversion-proof"
COUNTS = {"tasks": 2, "events": 1, "comments": 1, "artifacts": 1}
ROW = {**COUNTS, "registries": 1}


@pytest.fixture
def stores(tmp_path):
    source = SQLiteLedgerRepository(tmp_path / "source" / DATABASE, ledger_core)
    target = SQLiteLedgerRepository(tmp_path / "target" / DATABASE, ledger_core)
    source.import_document(SLUG, document(), token="kept-token")
    with source.connect() as connection, connection:
        source.save_registry(connection, "bin", {SLUG: 42})
    return source, target


def generation(repo, slug):
    with repo.connect() as connection:
        return connection.execute("SELECT generation FROM ledgers WHERE slug=?", (slug,)).fetchone()[0]


def test_counts_name_tasks_events_comments_artifacts_and_registries_of_one_document():
    assert shared.counts(document(), [["bin", SLUG, "42"]]) == ROW
    nested = {"_meta": {"events": [], "comments": [1]}, "notes": [{"comments": [{"comments": [1, 2]}]}]}
    assert shared.counts(nested, []) == {"tasks": 0, "events": 0, "comments": 3, "artifacts": 0, "registries": 0}


def test_a_conversion_copies_full_history_token_and_registries_then_fences_the_source(stores):
    source, target = stores
    report = shared.convert(source, target)
    assert report == {
        "ledgers": [{"slug": SLUG, "source": ROW, "shared": ROW, "match": True}],
        "totals": {"ledgers": 1, "source": ROW, "shared": ROW},
        "match": True,
        "read_only": True,
        "dry_run": False,
    }
    assert target.export_document(SLUG) == document()
    assert target.token(SLUG) == "kept-token"
    with target.connect() as connection:
        assert target.registry("bin", connection)[SLUG] == 42
    assert sqlite.fenced(source.directory, "*")
    with pytest.raises(sqlite3.IntegrityError, match="ledger moved to hosted service"):
        source.import_document("after", document())


def test_an_equal_copy_already_in_the_shared_database_is_left_untouched(stores):
    source, target = stores
    shared.convert(source, target)
    before = generation(target, SLUG)
    assert shared.convert(source, target)["match"] is True
    assert generation(target, SLUG) == before


def test_a_write_landing_before_the_fence_reaches_the_shared_database(stores, monkeypatch):
    source, target = stores
    freeze = shared.freeze

    def late_write(repo):
        assert target.export_document(SLUG) == document()
        changed = {**document(), "title": "Late write", "tasks": [*document()["tasks"], {"id": "third"}]}
        repo.import_document(SLUG, changed, token="kept-token", replace=True)
        repo.import_document("late-ledger", document())
        freeze(repo)

    monkeypatch.setattr(shared, "freeze", late_write)
    report = shared.convert(source, target)
    assert report["match"] is True
    assert [row["slug"] for row in report["ledgers"]] == ["conversion-proof", "late-ledger"]
    assert target.export_document(SLUG)["title"] == "Late write"
    assert report["ledgers"][0]["shared"]["tasks"] == 3
    assert target.export_document("late-ledger") == document()


def test_a_registry_change_alone_before_the_fence_reaches_the_shared_database(stores, monkeypatch):
    source, target = stores
    freeze = shared.freeze

    def binned(repo):
        with repo.connect() as connection, connection:
            repo.save_registry(connection, "bin", {SLUG: 7})
        freeze(repo)

    monkeypatch.setattr(shared, "freeze", binned)
    assert shared.convert(source, target)["match"] is True
    with target.connect() as connection:
        assert target.registry("bin", connection)[SLUG] == 7


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_differing_ledger_already_in_the_shared_database_is_never_replaced(stores, dry_run):
    source, target = stores
    target.import_document(SLUG, {**document(), "title": "Shared work"})
    with pytest.raises(ValueError, match=f"^ledger {SLUG} differs in the shared database; conversion refuses"):
        shared.convert(source, target, dry_run=dry_run)
    assert target.export_document(SLUG)["title"] == "Shared work"
    assert not sqlite.fenced(source.directory, "*")


@pytest.mark.parametrize("dry_run", [False, True])
def test_registry_rows_differing_in_the_shared_database_are_never_replaced(stores, dry_run):
    source, target = stores
    target.import_document(SLUG, document())
    with target.connect() as connection, connection:
        target.save_registry(connection, "bin", {SLUG: 9})
    with pytest.raises(ValueError, match="conversion refuses to replace it$"):
        shared.convert(source, target, dry_run=dry_run)
    with target.connect() as connection:
        assert target.registry("bin", connection)[SLUG] == 9
    assert not sqlite.fenced(source.directory, "*")


def test_a_dry_run_reports_counts_and_rolls_every_write_back(stores):
    source, target = stores
    report = shared.convert(source, target, dry_run=True)
    assert report["ledgers"] == [{"slug": SLUG, "source": ROW, "shared": ROW, "match": True}]
    assert report["dry_run"] is True
    assert report["read_only"] is False
    assert not target.exists(SLUG)
    assert not sqlite.fenced(source.directory, "*")
    source.import_document("still-writable", document())


def test_a_differing_count_is_reported_as_no_match(stores, monkeypatch):
    source, target = stores
    original = shared.counts
    calls = []

    def skewed(state, registries):
        calls.append(state)
        result = original(state, registries)
        return {**result, "events": result["events"] + 1} if len(calls) % 2 == 0 else result

    monkeypatch.setattr(shared, "counts", skewed)
    report = shared.convert(source, target, dry_run=True)
    assert report["match"] is False
    assert report["ledgers"][0]["match"] is False


def test_the_command_refuses_without_a_shared_database(stores, monkeypatch):
    source, _ = stores
    monkeypatch.delenv("AGENTIHOOKS_SWARM_DATABASE_URL", raising=False)
    monkeypatch.setattr("sys.argv", ["shared", "--source", str(source.directory)])
    with pytest.raises(
        SystemExit, match="^shared conversion: AGENTIHOOKS_SWARM_DATABASE_URL names no shared database$"
    ):
        shared.main()


def test_the_command_refuses_a_folder_without_a_ledger_database(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["shared", "--source", str(tmp_path), "--unfence"])
    with pytest.raises(SystemExit, match=f"^shared conversion: {tmp_path} holds no ledger database$"):
        shared.main()


def shared_store(monkeypatch, target):
    opened = []
    build = shared.storage.build

    def built(path=None, domain=None):
        if path is not None:
            return build(path, domain)
        opened.append(shared.storage.shared())
        return target

    monkeypatch.setenv("AGENTIHOOKS_SWARM_DATABASE_URL", "postgresql://ledger@shared/ledger")
    monkeypatch.setattr(shared.storage, "build", built)
    return opened


def test_the_command_converts_into_the_shared_database_its_url_names(stores, monkeypatch, capsys):
    source, target = stores
    opened = shared_store(monkeypatch, target)
    monkeypatch.setattr("sys.argv", ["shared", "--source", str(source.directory)])
    shared.main()
    report = json.loads(capsys.readouterr().out)
    assert opened == ["postgresql://ledger@shared/ledger"]
    assert (report["match"], report["read_only"], report["totals"]["shared"]) == (True, True, ROW)
    assert target.export_document(SLUG) == document()


def test_unfencing_removes_the_untouched_copies_and_reopens_the_folder(stores, monkeypatch, capsys):
    source, target = stores
    shared.convert(source, target)
    shared_store(monkeypatch, target)
    monkeypatch.setattr("sys.argv", ["shared", "--source", str(source.directory), "--unfence"])
    shared.main()
    assert json.loads(capsys.readouterr().out) == {"removed": [SLUG], "read_only": False}
    assert not target.exists(SLUG)
    with target.connect() as connection:
        assert SLUG not in target.registry("bin", connection)
    source.import_document("after", document())
    assert source.exists("after")


def test_unfencing_is_refused_once_the_shared_copy_took_a_write(stores):
    source, target = stores
    shared.convert(source, target)
    target.import_document(SLUG, {**document(), "title": "Shared write"}, replace=True)
    with pytest.raises(ValueError, match=f"^ledger {SLUG} changed in the shared database; unfencing would leave"):
        shared.thaw(source, target)
    assert sqlite.fenced(source.directory, "*")
    assert target.export_document(SLUG)["title"] == "Shared write"


def test_unfencing_is_refused_once_the_shared_registry_rows_changed(stores):
    source, target = stores
    shared.convert(source, target)
    with target.connect() as connection, connection:
        target.save_registry(connection, "bin", {SLUG: 5})
    with pytest.raises(ValueError, match="two writable stores$"):
        shared.thaw(source, target)
    assert sqlite.fenced(source.directory, "*")
    assert target.exists(SLUG)


def test_a_dry_run_and_an_unfence_cannot_be_combined(stores, monkeypatch):
    source, _ = stores
    monkeypatch.setattr("sys.argv", ["shared", "--source", str(source.directory), "--dry-run", "--unfence"])
    with pytest.raises(SystemExit):
        shared.main()
    assert not sqlite.fenced(source.directory, "*")


def test_the_folder_reads_frozen_only_under_the_every_ledger_fence_even_with_a_shared_database(stores, monkeypatch):
    source, _ = stores
    monkeypatch.setenv("AGENTIHOOKS_SWARM_DATABASE_URL", "postgresql://ledger@shared/ledger")
    assert storage.folder_frozen(source.directory) is False
    shared.freeze(source)
    assert storage.folder_frozen(source.directory) is True
    with source.transaction() as transaction:
        transaction.unfence(shared.DESTINATION)
    assert storage.folder_frozen(source.directory) is False


def test_the_command_names_itself_and_requires_a_source(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["shared"])
    with pytest.raises(SystemExit):
        shared.main()
    error = capsys.readouterr().err
    assert (
        "\npython -m scripts.swarm_ledger.storage_migration.shared: error: the following arguments are required: --source\n"
        in error
    )


def test_the_command_dry_run_rolls_back_and_leaves_the_folder_writable(stores, monkeypatch, capsys):
    source, target = stores
    shared_store(monkeypatch, target)
    monkeypatch.setattr("sys.argv", ["shared", "--source", str(source.directory), "--dry-run"])
    shared.main()
    report = json.loads(capsys.readouterr().out)
    assert (report["dry_run"], report["read_only"], report["match"]) == (True, False, True)
    assert not target.exists(SLUG)
    assert not storage.folder_frozen(source.directory)
