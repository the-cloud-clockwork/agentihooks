import ast
from pathlib import Path

import pytest

import scripts.swarm_ledger.repository as package
from scripts.swarm_ledger import storage
from scripts.swarm_ledger.repository import LedgerRepository
from tests.swarm_ledger.test_sqlite import document

ROOT = Path(__file__).resolve().parents[2]
STORE = "scripts.swarm_ledger.repository.sqlite"
COMPOSITION_ROOT = ROOT / "scripts" / "swarm_ledger" / "storage.py"
STORE_PACKAGE = ROOT / "scripts" / "swarm_ledger" / "repository"


STORE_NAMES = {STORE, "repository.sqlite", "swarm_ledger.repository.sqlite"}
PACKAGE_NAMES = {name.removesuffix(".sqlite") for name in STORE_NAMES}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        return str(node.args[0].value) if dotted(node.func).endswith(("import_module", "__import__")) else ""
    return node.id if isinstance(node, ast.Name) else ""


def aliases(tree: ast.AST, path: Path, root: Path) -> dict:
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound.update({alias.asname: alias.name for alias in node.names if alias.asname})
        elif isinstance(node, ast.ImportFrom):
            module = absolute(path, root, node)
            bound.update({alias.asname or alias.name: f"{module}.{alias.name}" for alias in node.names})
    return bound


def absolute(path: Path, root: Path, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join([*parts[: len(parts) - node.level], *([node.module] if node.module else [])])


def imported(path: Path, root: Path = ROOT) -> set:
    names = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = aliases(tree, path, root)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = absolute(path, root, node)
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
            if module in PACKAGE_NAMES and any(alias.name == "*" for alias in node.names):
                names.add(f"{module}.sqlite")
        elif isinstance(node, ast.Constant) and node.value in STORE_NAMES:
            names.add(node.value)
        elif isinstance(node, ast.Attribute) and node.attr == "sqlite":
            head, _, rest = dotted(node).partition(".")
            names.add(".".join(part for part in (bound.get(head, head), rest) if part))
    return names


def store_importers() -> list:
    sources = [*(ROOT / "scripts").rglob("*.py"), *(ROOT / "hooks").rglob("*.py")]
    allowed = [path for path in sources if path == COMPOSITION_ROOT or STORE_PACKAGE in path.parents]
    return sorted(
        str(path.relative_to(ROOT)) for path in sources if path not in allowed and STORE_NAMES & imported(path)
    )


def test_no_module_outside_the_composition_root_imports_the_sqlite_store():
    assert store_importers() == []


@pytest.mark.parametrize(
    ("folder", "source", "name"),
    [
        ("", "from scripts.swarm_ledger.repository import sqlite\n", STORE),
        ("scripts/swarm_ledger/api", "from ..repository.sqlite import DATABASE\n", STORE),
        ("scripts/swarm_ledger/api", "from ..repository import sqlite\n", STORE),
        ("scripts/swarm_ledger", "from repository.sqlite import DATABASE\n", "repository.sqlite"),
        ("hooks", "import swarm_ledger.repository.sqlite\n", "swarm_ledger.repository.sqlite"),
        ("hooks", 'importlib.import_module("scripts.swarm_ledger.repository.sqlite")\n', STORE),
        ("hooks", "import scripts.swarm_ledger.repository as r\nr.sqlite.read_ledger\n", STORE),
        ("hooks", "from scripts.swarm_ledger import repository\nrepository.sqlite.DATABASE\n", STORE),
        ("hooks", "import scripts.swarm_ledger.repository\nscripts.swarm_ledger.repository.sqlite.DATABASE\n", STORE),
        ("hooks", "from scripts.swarm_ledger.repository import *\n", STORE),
        ("hooks", 'importlib.import_module("scripts.swarm_ledger.repository").sqlite\n', STORE),
    ],
)
def test_the_import_check_sees_every_spelling_of_the_store(tmp_path, folder, source, name):
    probe = tmp_path / folder / "probe.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(source, encoding="utf-8")
    assert name in imported(probe, tmp_path)


def test_the_repository_package_binds_no_repository_at_import():
    assert "repository" not in vars(package)


def test_the_composition_root_builds_one_repository_per_process():
    built = storage.default()
    assert storage.default() is built
    assert isinstance(built, LedgerRepository)


def test_a_built_repository_stores_at_the_path_it_was_given(tmp_path):
    built = storage.build(tmp_path / storage.DATABASE)
    built.import_document("composed", document())
    assert built.path == tmp_path / storage.DATABASE
    assert storage.read_slugs(tmp_path) == ["composed"]


def test_a_built_repository_follows_the_domain_it_was_given(tmp_path):
    domain = object()
    assert storage.build(tmp_path / storage.DATABASE, domain).domain is domain


def test_an_entry_keeps_what_it_was_built_with():
    from scripts.swarm_ledger.repository.entry import Entry

    entry = Entry(7, '{"a":1}', {"a": 1})
    assert (entry.generation, entry.text, entry.state, entry.views, entry.fragments) == (
        7,
        '{"a":1}',
        {"a": 1},
        {},
        None,
    )
    fragments = object()
    assert Entry(8, None, {}, fragments).fragments is fragments


def test_the_relay_journal_holds_releases_and_drops_revisions(tmp_path):
    journal = storage.build(tmp_path / storage.DATABASE)
    assert journal.held_revisions() == {}
    for kind, slug, revision in (("relay", "a", 3), ("relay", "a", 9), ("relay", "a", 3), ("alerts", "b", 4)):
        journal.hold_revision(kind, slug, revision)
    assert journal.held_revisions() == {"relay": {"a": [3, 9]}, "alerts": {"b": [4]}}
    journal.release_revisions("relay", "a", 1, 5)
    assert journal.held_revisions() == {"relay": {"a": [9]}, "alerts": {"b": [4]}}
    journal.drop_revisions("alerts", "b")
    assert journal.held_revisions() == {"relay": {"a": [9]}}


def test_a_failed_transaction_leaves_nothing_stored(tmp_path):
    built = storage.build(tmp_path / storage.DATABASE)
    with pytest.raises(RuntimeError, match="^stop$"), built.transaction() as transaction:
        transaction.insert("rolled", document(), None, "token")
        assert transaction.exists("rolled")
        raise RuntimeError("stop")
    assert not built.exists("rolled")


def test_a_transaction_holds_the_core_lock(tmp_path):
    built = storage.build(tmp_path / storage.DATABASE)
    with built.transaction():
        assert built.domain.LOCK.locked()
    assert not built.domain.LOCK.locked()


def test_a_snapshot_of_a_folder_without_a_database_is_none(tmp_path):
    with storage.build(tmp_path / "absent" / storage.DATABASE).snapshot() as snapshot:
        assert snapshot is None


def test_a_snapshot_exports_the_stored_document_with_its_registries(tmp_path):
    built = storage.build(tmp_path / storage.DATABASE)
    built.import_document("snapped", document())
    with built.transaction() as transaction:
        transaction.add_registries([["bin", "snapped", "1"]])
    with built.snapshot() as snapshot:
        assert snapshot.export("snapped") == document()
        assert snapshot.registries("snapped") == [["bin", "snapped", "1"]]
