import io
import json
import socket
import stat
import tarfile
from pathlib import Path

import pytest

from scripts.swarm_v2 import filesystem

pytestmark = pytest.mark.unit

FIXTURE = json.loads((Path(__file__).parent / "fixtures/swarm_v2/filesystem.json").read_text())
ATTEMPT = FIXTURE["attempt"]
LAYOUT_FILE = Path(__file__).resolve().parents[1] / "docker" / "swarm-node" / "layout.json"
TYPES = {"file": tarfile.REGTYPE, "dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE}


def failures() -> int:
    return filesystem.FAILURES["execution_path_validation_failures"]


def snapshot(root: Path) -> dict[str, tuple]:
    return {
        str(p.relative_to(root)): (p.is_symlink(), p.read_bytes() if p.is_file() and not p.is_symlink() else None)
        for p in sorted(root.rglob("*"))
    }


def writable(path: Path) -> bool:
    return bool(path.lstat().st_mode & 0o222)


@pytest.fixture
def world(tmp_path):
    bases = [tmp_path / name for name in FIXTURE["bases"]]
    for base in bases:
        base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text("{}")
    return bases, outside


@pytest.fixture
def layout():
    return filesystem.load(LAYOUT_FILE)


def template(tmp_path: Path) -> Path:
    source = tmp_path / "templates" / FIXTURE["profile"]
    (source / ".claude").mkdir(parents=True)
    (source / "CLAUDE.md").write_text("seed")
    (source / ".claude" / "rules.md").write_text("rule")
    (source / "persona.md").symlink_to("CLAUDE.md")
    return source


def archive(tmp_path: Path, members: list[dict]) -> Path:
    path = tmp_path / f"archive-{len(list(tmp_path.glob('archive-*')))}.tar"
    with tarfile.open(path, "w") as bundle:
        for member in members:
            info = tarfile.TarInfo(member["name"])
            info.type = TYPES[member["type"]]
            info.linkname = member.get("link", "")
            data = member["name"].encode() if member["type"] == "file" else b""
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return path


def test_the_shipped_layout_names_every_root_relative_to_the_attempt(layout):
    document = json.loads(LAYOUT_FILE.read_text())
    assert document["package"] == "SV2-FSY-01"
    assert layout == filesystem.Layout(
        1,
        {
            "home": "homes",
            "runtime": "run",
            "checkout": "checkouts",
            "worktree": "worktrees",
            "spool": "spool",
            "scratch": "tmp",
            "seed": "profiles",
        },
        ("seed",),
    )
    assert filesystem.mapping(layout) == {
        "layout_version": 1,
        "roots": layout.roots,
        "immutable": ["seed"],
    }
    assert filesystem.parse(filesystem.mapping(layout)) == layout


def test_load_reads_the_repository_layout_then_the_image_copy(tmp_path, monkeypatch, layout):
    assert filesystem.load() == layout
    image = tmp_path / "image-layout.json"
    image.write_text(LAYOUT_FILE.read_text())
    monkeypatch.setattr(filesystem, "LAYOUTS", (tmp_path / "missing.json", image))
    assert filesystem.load() == layout
    image.unlink()
    with pytest.raises(FileNotFoundError):
        filesystem.load()


def test_two_attempts_share_internal_names_but_no_mutable_file_or_socket(world, layout):
    bases, _ = world
    first, second = (filesystem.allocate(base, ATTEMPT, layout) for base in bases)
    for name in filesystem.ROOTS:
        assert first.path(name).relative_to(first.root) == second.path(name).relative_to(second.root)
        assert first.path(name) != second.path(name)
        assert first.path(name).is_dir() and not first.path(name).is_symlink()
        assert oct(first.path(name).stat().st_mode & 0o777) == "0o700"
    assert oct(first.root.stat().st_mode & 0o777) == "0o700"
    (first.path("home") / "claude.json").write_text("first")
    assert not (second.path("home") / "claude.json").exists()
    paths = [filesystem.socket(execution, FIXTURE["socket"]) for execution in (first, second)]
    assert [p.relative_to(e.root) for p, e in zip(paths, (first, second))] == [Path("run/herdr.sock")] * 2
    listeners = []
    for path in paths:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(path))
        listeners.append(listener)
    assert all(path.is_socket() for path in paths)
    for listener in listeners:
        listener.close()


def test_a_second_independent_fixture_allocates_the_same_layout(tmp_path, layout):
    runs = []
    for run in ("one", "two"):
        base = tmp_path / run
        base.mkdir()
        execution = filesystem.allocate(base, ATTEMPT, layout)
        runs.append(sorted(str(p.relative_to(base)) for p in base.rglob("*")))
        assert execution.root == base / ATTEMPT
    assert runs[0] == runs[1] == sorted([ATTEMPT, *(f"{ATTEMPT}/{f}" for f in layout.roots.values())])


def test_allocate_keeps_existing_contents(world, layout):
    bases, _ = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    (execution.path("spool") / "chunk").write_text("kept")
    again = filesystem.allocate(bases[0], ATTEMPT, layout)
    assert (again.path("spool") / "chunk").read_text() == "kept"


def test_seed_copies_a_profile_read_only_while_homes_stay_writable(tmp_path, world, layout):
    bases, _ = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    copy = filesystem.seed(execution, template(tmp_path), FIXTURE["profile"])
    assert copy == execution.path("seed") / FIXTURE["profile"]
    assert (copy / "persona.md").read_text() == "seed"
    assert (copy / "persona.md").readlink() == Path("CLAUDE.md")
    assert not any(writable(p) for p in [copy, *copy.rglob("*")] if not p.is_symlink())
    assert writable(execution.path("home")) and writable(execution.path("seed"))
    filesystem.remove(execution.root)
    assert not execution.root.exists()


@pytest.mark.parametrize("escape", FIXTURE["path_escapes"])
def test_a_relative_escape_is_refused(world, layout, escape):
    bases, _ = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    before = failures()
    with pytest.raises(filesystem.LayoutError) as error:
        filesystem.contain(execution.root, escape)
    assert str(error.value) == f"path resolves outside its execution root: {escape}"
    assert failures() == before + 1


def test_a_symlink_escape_is_refused_without_naming_its_target(world, layout):
    bases, outside = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    link = execution.path("home") / "state"
    link.symlink_to(outside)
    assert filesystem.contain(execution.root, "homes") == execution.path("home").resolve()
    with pytest.raises(filesystem.LayoutError) as error:
        filesystem.contain(execution.root, link)
    assert str(error.value) == f"path resolves outside its execution root: {link}"
    assert str(outside) not in str(error.value).replace(str(link), "")


@pytest.mark.parametrize("link", ["outside", "absolute inside"])
def test_a_profile_that_resolves_outside_its_root_is_refused_before_any_write(tmp_path, world, layout, link):
    bases, outside = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    source = template(tmp_path)
    (source / "escape").symlink_to(outside if link == "outside" else (source / "CLAUDE.md"))
    before, sources, count = snapshot(execution.root), snapshot(source), failures()
    with pytest.raises(filesystem.LayoutError) as error:
        filesystem.seed(execution, source, FIXTURE["profile"])
    assert str(error.value).startswith("path resolves outside its execution root: ")
    assert str(error.value).endswith("escape")
    assert snapshot(execution.root) == before
    assert snapshot(source) == sources
    assert failures() == count + 1


def test_an_invalid_seed_name_is_refused(tmp_path, world, layout):
    bases, _ = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    with pytest.raises(filesystem.LayoutError, match=r"^invalid profile name: \.\./x$"):
        filesystem.seed(execution, template(tmp_path), "../x")


def test_a_contained_archive_extracts_into_its_destination(tmp_path, world, layout):
    bases, _ = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    bundle = archive(
        tmp_path,
        [
            {"name": "repo", "type": "dir"},
            {"name": "repo/a.txt", "type": "file"},
            {"name": "repo/link", "type": "symlink", "link": "a.txt"},
        ],
    )
    destination = execution.path("checkout") / "task"
    assert filesystem.extract(execution, bundle, destination) == destination.resolve()
    assert (destination / "repo" / "link").read_text() == "repo/a.txt"
    assert list(execution.path("scratch").iterdir()) == []


@pytest.mark.parametrize("case", sorted(FIXTURE["archive_escapes"]))
def test_an_archive_that_resolves_outside_its_root_is_refused_before_any_write(tmp_path, world, layout, case):
    bases, outside = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    members = FIXTURE["archive_escapes"][case]
    bundle = archive(tmp_path, members)
    before, protected, count = snapshot(execution.root), snapshot(outside), failures()
    with pytest.raises(filesystem.LayoutError) as error:
        filesystem.extract(execution, bundle, execution.path("checkout") / "task")
    assert str(error.value).endswith(members[-1]["name"])
    assert snapshot(execution.root) == before
    assert snapshot(outside) == protected
    assert failures() == count + 1


def test_extraction_outside_the_root_or_over_existing_files_is_refused(tmp_path, world, layout):
    bases, outside = world
    execution = filesystem.allocate(bases[0], ATTEMPT, layout)
    bundle = archive(tmp_path, [{"name": "a.txt", "type": "file"}])
    with pytest.raises(filesystem.LayoutError, match="^path resolves outside its execution root: "):
        filesystem.extract(execution, bundle, outside / "task")
    with pytest.raises(filesystem.LayoutError, match="^extraction destination already exists: "):
        filesystem.extract(execution, bundle, execution.path("checkout"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"roots": None}, "layout must name exactly the roots home, runtime, checkout"),
        ({"roots": {"home": "homes"}}, "layout must name exactly the roots"),
        ({"roots": "/abs"}, "layout must name exactly the roots"),
        ({"home": "/node-a/homes"}, "layout root home must be one relative folder name"),
        ({"home": ".."}, "layout root home must be one relative folder name"),
        ({"home": "homes/claude"}, "layout root home must be one relative folder name"),
        ({"home": 7}, "layout root home must be one relative folder name"),
        ({"home": "tmp"}, "layout roots must not share a folder"),
        ({"immutable": ["cache"]}, "layout immutable roots must be named roots"),
        ({"immutable": "seed"}, "layout immutable roots must be named roots"),
        ({"layout_version": 2}, "layout version 2 has no reader, so new launches stop"),
        ({"layout_version": None}, "layout version None has no reader, so new launches stop"),
    ],
)
def test_a_layout_that_could_leave_its_root_is_refused(layout, change, message):
    document = filesystem.mapping(layout)
    if "home" in change:
        document["roots"]["home"] = change["home"]
    else:
        document |= change
    count = failures()
    with pytest.raises(filesystem.LayoutError) as error:
        filesystem.parse(document)
    assert str(error.value).startswith(message)
    assert failures() == count + 1


@pytest.mark.parametrize("attempt", ["../x", "", "A", "a/b"])
def test_an_invalid_attempt_is_refused(world, layout, attempt):
    bases, _ = world
    with pytest.raises(filesystem.LayoutError, match="^invalid attempt id: "):
        filesystem.allocate(bases[0], attempt, layout)
    assert list(bases[0].iterdir()) == []


def test_a_linked_base_attempt_or_root_is_refused(tmp_path, world, layout):
    bases, outside = world
    linked = tmp_path / "linked"
    linked.symlink_to(bases[0])
    with pytest.raises(filesystem.LayoutError, match="^execution base not found: "):
        filesystem.allocate(linked, ATTEMPT, layout)
    with pytest.raises(filesystem.LayoutError, match="^execution base not found: "):
        filesystem.allocate(tmp_path / "missing", ATTEMPT, layout)
    (bases[0] / ATTEMPT).symlink_to(outside)
    with pytest.raises(filesystem.LayoutError, match=f"^execution root is a link: {ATTEMPT}$"):
        filesystem.allocate(bases[0], ATTEMPT, layout)
    root = bases[1] / ATTEMPT
    root.mkdir()
    (root / "spool").symlink_to(outside)
    with pytest.raises(filesystem.LayoutError, match="^layout root spool is a link$"):
        filesystem.allocate(bases[1], ATTEMPT, layout)
    assert list(outside.iterdir()) == [outside / "state.json"]


def test_a_socket_path_past_the_unix_limit_or_with_a_path_name_is_refused(tmp_path, layout):
    deep = tmp_path / ("d" * (filesystem.SOCKET_BYTES - len(str(tmp_path)) - len("/") - len("/a/run/s")))
    deep.mkdir()
    execution = filesystem.allocate(deep, "a", layout)
    fits = filesystem.socket(execution, "s")
    assert len(str(fits).encode()) == filesystem.SOCKET_BYTES
    with pytest.raises(filesystem.LayoutError, match=f"^socket path exceeds {filesystem.SOCKET_BYTES} bytes: ss$"):
        filesystem.socket(execution, "ss")
    with pytest.raises(filesystem.LayoutError, match=r"^invalid socket name: \.\./s$"):
        filesystem.socket(execution, "../s")


def test_recreating_an_attempt_restores_paths_from_metadata_on_a_new_node(tmp_path, world, layout):
    bases, _ = world
    old = filesystem.allocate(bases[0], ATTEMPT, layout)
    filesystem.seed(old, template(tmp_path), FIXTURE["profile"])
    recorded = json.loads(json.dumps(filesystem.mapping(layout)))
    assert not any(str(value).startswith("/") for value in recorded["roots"].values())
    filesystem.remove(bases[0])
    restored = filesystem.restore(bases[1], ATTEMPT, recorded)
    assert restored.root == bases[1] / ATTEMPT
    assert restored.layout == layout
    for name in filesystem.ROOTS:
        assert restored.path(name).is_dir()
        assert not str(restored.path(name)).startswith(str(bases[0]))
    (restored.path("spool") / "chunk").write_text("kept")
    replay = filesystem.restore(bases[1], ATTEMPT, recorded)
    assert replay == restored
    assert (replay.path("spool") / "chunk").read_text() == "kept"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"home": "/a/fsy-1/homes"}, "layout root home must be one relative folder name"),
        ({"layout_version": 2}, "layout version 2 has no reader, so new launches stop"),
    ],
)
def test_a_record_naming_an_old_node_path_or_unknown_version_stops_the_launch(world, layout, change, message):
    bases, _ = world
    recorded = filesystem.mapping(layout)
    recorded["roots"]["home"] = change.get("home", recorded["roots"]["home"])
    recorded["layout_version"] = change.get("layout_version", 1)
    count = failures()
    with pytest.raises(filesystem.LayoutError) as error:
        filesystem.restore(bases[1], ATTEMPT, recorded)
    assert str(error.value) == message
    assert list(bases[1].iterdir()) == []
    assert failures() == count + 1


def test_remove_restores_write_bits_on_sealed_folders(tmp_path):
    tree = tmp_path / "tree"
    (tree / "inner").mkdir(parents=True)
    (tree / "inner" / "file").write_text("x")
    (tree / "link").symlink_to("inner")
    filesystem.seal(tree)
    assert stat.S_IMODE(tree.stat().st_mode) & 0o222 == 0
    assert stat.S_IMODE((tree / "inner" / "file").stat().st_mode) & 0o222 == 0
    filesystem.remove(tree)
    assert not tree.exists()
