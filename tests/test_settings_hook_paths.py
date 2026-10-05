import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_script_a_settings_hook_runs_exists():
    text = (ROOT / "profiles/_base/settings.base.json").read_text()
    paths = set(re.findall(r"/app/(scripts/[\w/]+\.py)", text))
    assert paths
    assert [p for p in sorted(paths) if not (ROOT / p).is_file()] == []
