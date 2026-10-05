import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import deps_preflight, install

_ROOT = Path(__file__).resolve().parent.parent

_PROBE = """
import importlib, json, sys
sys.path[:0] = [".", "scripts"]
groups = {}
for name in sys.argv[1:]:
    marks = getattr(importlib.import_module(name), "pytestmark", [])
    marks = marks if isinstance(marks, list) else [marks]
    groups[name] = [m.args[0] for m in marks if m.name == "xdist_group"]
print(json.dumps({"loaded": sorted(m for m in ("fakeredis", "redis") if m in sys.modules), "groups": groups}))
"""


def test_suite_never_runs_the_real_deps_preflight(tmp_path, monkeypatch):
    (tmp_path / "deps.json").write_text("{}")
    monkeypatch.setattr(install, "_get_bundle_path", lambda: tmp_path)
    assert deps_preflight.manifest_path() is None


@pytest.fixture(scope="module")
def fakeredis_modules():
    paths = sorted(
        path
        for path in (_ROOT / "tests").rglob("test_*.py")
        if path != Path(__file__).resolve() and "fakeredis" in path.read_text()
    )
    names = [".".join(path.relative_to(_ROOT).with_suffix("").parts) for path in paths]
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, *names], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return names, json.loads(out)


def test_fakeredis_test_modules_import_without_loading_redis(fakeredis_modules):
    names, probe = fakeredis_modules
    assert names
    assert probe["loaded"] == []


def test_fakeredis_test_modules_share_one_xdist_group(fakeredis_modules):
    names, probe = fakeredis_modules
    assert {name: probe["groups"][name] for name in names} == {name: ["fakeredis"] for name in names}
