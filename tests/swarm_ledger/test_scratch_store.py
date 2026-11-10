from pathlib import Path

import ledger_core
import pytest

from scripts.swarm_ledger import storage
from scripts.swarm_ledger.repository import postgres, sqlite
from tests.swarm_ledger.test_sqlite import CONTENT

DATABASE_URL = "postgresql://ledger@live/ledger"
SLUG = "scratch-proof"


def refuse_shared(*_args, **_kwargs):
    raise AssertionError("a scratch ledger reached the shared database")


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = Path.home() / "scratchpad" / "agentihooks" / "proof" / "ledger"
    folder.mkdir(parents=True)
    monkeypatch.setenv("LEDGER_DIR", str(folder))
    monkeypatch.setenv("LEDGER_PORT", "18999")
    monkeypatch.setenv(postgres.URL, DATABASE_URL)
    monkeypatch.delenv(postgres.SCRATCH_SHARED, raising=False)
    monkeypatch.setattr(ledger_core, "LEDGER_DIR", folder)
    monkeypatch.setattr(postgres, "PostgresLedgerRepository", refuse_shared)
    monkeypatch.setattr(postgres, "Database", refuse_shared)
    return folder


def test_a_scratch_launch_with_inherited_database_settings_writes_only_to_its_folder(scratch: Path) -> None:
    repository = storage.build()

    assert type(repository) is sqlite.SQLiteLedgerRepository
    assert repository.create(SLUG, CONTENT)
    assert repository.path == scratch / storage.DATABASE
    assert (scratch / storage.DATABASE).is_file()
    assert storage.shared() is None
    assert storage.store(scratch) == scratch
    assert storage.read_slugs(scratch) == [SLUG]


def test_a_scratch_folder_ignores_the_shared_database_url() -> None:
    folder = Path.home() / "scratchpad" / "agentihooks" / "proof"

    assert postgres.url({postgres.URL: DATABASE_URL, "LEDGER_DIR": str(folder)}) is None
    assert postgres.url({postgres.URL: DATABASE_URL, "LEDGER_DIR": str(Path.home() / "scratchpad")}) is None


def test_a_scratch_folder_uses_the_shared_database_only_when_asked() -> None:
    folder = str(Path.home() / "scratchpad" / "agentihooks" / "proof")
    asked = {postgres.URL: DATABASE_URL, "LEDGER_DIR": folder, postgres.SCRATCH_SHARED: "1"}
    other = {postgres.URL: DATABASE_URL, "LEDGER_DIR": folder, postgres.SCRATCH_SHARED: "yes"}

    assert postgres.SCRATCH_SHARED == "AGENTIHOOKS_SCRATCH_SHARED_DATABASE"
    assert postgres.url(asked) == DATABASE_URL
    assert postgres.url(other) is None


@pytest.mark.parametrize(
    "folder",
    ["/data", str(Path.home() / "development-ledger"), str(Path.home() / "scratchpad-ledger"), None],
)
def test_a_ledger_folder_outside_the_scratchpad_keeps_the_shared_database(folder: str | None) -> None:
    env = {postgres.URL: DATABASE_URL} if folder is None else {postgres.URL: DATABASE_URL, "LEDGER_DIR": folder}

    assert postgres.url(env) == DATABASE_URL


def test_a_scratch_folder_given_with_a_home_tilde_is_a_scratch_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(Path.home()))

    assert postgres.url({postgres.URL: DATABASE_URL, "LEDGER_DIR": "~/scratchpad/agentihooks/proof"}) is None
