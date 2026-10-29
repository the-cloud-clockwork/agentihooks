import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_settings_hook_runs_the_shared_hooks_entry():
    hooks = json.loads((ROOT / "profiles/_base/settings.base.json").read_text())["hooks"]
    commands = {h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]}
    assert hooks
    assert commands == {"cd /app && __PYTHON__ -m hooks"}
