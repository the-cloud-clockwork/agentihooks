import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_script_a_settings_hook_runs_exists():
    text = (ROOT / "profiles/_base/settings.base.json").read_text()
    paths = set(re.findall(r"/app/(scripts/[\w/]+\.py)", text))
    assert paths
    assert [p for p in sorted(paths) if not (ROOT / p).is_file()] == []


def test_the_old_plan_ledger_hook_path_still_runs_the_swarm_ledger_hook(monkeypatch, capsys):
    import runpy

    seen = []
    monkeypatch.setattr(runpy, "run_path", lambda path, run_name: seen.append((Path(path), run_name)))
    exec(
        (ROOT / "scripts/plan_ledger/ledger_hook.py").read_text(),
        {"__file__": str(ROOT / "scripts/plan_ledger/ledger_hook.py")},
    )
    assert seen == [(ROOT / "scripts/swarm_ledger/ledger_hook.py", "__main__")]
