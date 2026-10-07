import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.profiles import homes


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(homes, "GRACE_SECONDS", 0)
    monkeypatch.setattr(homes, "live_homes", lambda: [])
    return tmp_path / "profiles"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_live_homes_reads_each_process_profile_home(tmp_path):
    _write(tmp_path / "self" / "environ", "CLAUDE_CONFIG_DIR=/p/self\0")
    (tmp_path / "14").mkdir()
    (tmp_path / "13").mkdir()
    (tmp_path / "13" / "environ").write_bytes(b"\xff=1\0B=2\0CLAUDE_CONFIG_DIR=\0")
    _write(tmp_path / "11" / "environ", "A=1\0CLAUDE_CONFIG_DIR=/p/a=b\0")
    (tmp_path / "12").mkdir()
    (tmp_path / "12" / "environ").write_bytes(b"CODEX_HOME=/p/\xff\0B=2\0")
    order = [tmp_path / name for name in ("self", "14", "13", "11", "12")]

    found = homes.live_homes(SimpleNamespace(iterdir=lambda: iter(order)))

    assert found == [Path("/p/a=b"), Path("/p/�")]


def test_live_homes_resolves_each_path(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    _write(tmp_path / "proc" / "21" / "environ", f"CLAUDE_CONFIG_DIR={tmp_path / 'link'}\0")

    assert homes.live_homes(tmp_path / "proc") == [target]


def test_current_follows_the_pointer_then_a_real_legacy_folder(root):
    assert homes.current(root, "eng") is None
    legacy = root / "eng"
    legacy.mkdir(parents=True)
    assert homes.current(root, "eng") == legacy

    home = homes.fresh(root, "eng", {"a": 1})
    homes.promote(root, "eng", home)

    assert homes.current(root, "eng") == home.resolve()


def test_a_named_link_without_a_pointer_is_no_home(root):
    root.mkdir()
    (root / "eng").symlink_to(root)

    assert homes.current(root, "eng") is None


def test_owner_names_the_profile_of_a_home(root, tmp_path):
    home = homes.fresh(root, "eng", {})
    (home / "claude").mkdir()
    (root / "qa" / "claude").mkdir(parents=True)

    assert homes.owner(root, home / "claude") == "eng"
    assert homes.owner(root, root / "qa" / "claude") == "qa"
    assert homes.owner(root, home) is None
    assert homes.owner(root, root / "qa") is None
    assert homes.owner(root, tmp_path) is None


def test_fresh_homes_are_named_by_the_stamp_and_never_reused(root):
    stamp = {"b": 2, "a": 1}
    digest = hashlib.sha256(json.dumps(stamp, sort_keys=True).encode()).hexdigest()[:12]

    first, second = homes.fresh(root, "eng", stamp), homes.fresh(root, "eng", dict(reversed(stamp.items())))

    assert first.parent == second.parent == root / homes.HOMES / "eng"
    assert first.name.startswith(f"{digest}-") and second.name.startswith(f"{digest}-")
    assert first != second and first.is_dir() and second.is_dir()


def test_promote_points_the_profile_at_the_new_home(root):
    home = homes.fresh(root, "eng", {})

    homes.promote(root, "eng", home)

    pointer = root / homes.HOMES / "eng" / homes.CURRENT
    assert os.readlink(pointer) == home.name
    assert os.readlink(root / "eng") == str(Path(homes.HOMES) / "eng" / homes.CURRENT)
    assert (root / "eng").resolve() == home.resolve()
    assert sorted(p.name for p in pointer.parent.iterdir()) == sorted([home.name, homes.CURRENT])


def test_collect_keeps_the_current_and_live_homes_and_removes_the_rest(root, monkeypatch):
    live, old, kept = (homes.fresh(root, "eng", {"n": n}) for n in range(3))
    monkeypatch.setattr(homes, "live_homes", lambda: [(live / "claude").resolve()])

    homes.promote(root, "eng", kept)

    assert live.is_dir() and kept.is_dir() and not old.exists()


def test_collect_waits_out_the_launch_grace(root, monkeypatch):
    monkeypatch.setattr(homes, "GRACE_SECONDS", 600)
    legacy = _write(root / "eng" / "claude" / "settings.json", "{}").parent.parent
    os.utime(legacy, (1, 1))
    recent = homes.fresh(root, "eng", {})
    expired = homes.fresh(root, "eng", {})
    now = expired.stat().st_mtime + 600
    os.utime(recent, (now, now))
    monkeypatch.setattr(homes.time, "time", lambda: now)

    homes.promote(root, "eng", homes.fresh(root, "eng", {}))

    assert recent.is_dir() and not expired.exists() and legacy.is_symlink()


def test_a_legacy_folder_goes_once_unused_and_the_name_links_to_the_current_home(root, monkeypatch):
    legacy = _write(root / "eng" / "claude" / "settings.json", "{}").parent.parent
    live = [(legacy / "claude").resolve()]
    monkeypatch.setattr(homes, "live_homes", lambda: live)
    first = homes.fresh(root, "eng", {})
    homes.promote(root, "eng", first)

    assert legacy.is_dir() and not legacy.is_symlink()

    live.clear()
    second = homes.fresh(root, "eng", {})
    homes.promote(root, "eng", second)

    assert (root / "eng").is_symlink() and (root / "eng").resolve() == second.resolve()
    assert not first.exists()
