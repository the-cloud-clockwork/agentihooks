import json
from pathlib import Path

import pytest

from scripts.swarm_ledger import artifact_sanity as sanity
from tests.swarm_ledger.test_artifact_sanity import AUDIT


@pytest.fixture(scope="module")
def browsers_path():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            pw.chromium.launch().close()
        except Exception as exc:
            pytest.skip(f"no chromium: {exc}")
        return str(Path(pw.chromium.executable_path).parents[2])


@pytest.fixture
def chromium(browsers_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", browsers_path)


def test_the_command_prints_the_report_and_exits_zero_when_clean(chromium, capsys):
    assert sanity.main([str(AUDIT)]) == 0
    assert json.loads(capsys.readouterr().out) == {AUDIT.name: []}


def test_the_command_exits_one_on_any_failure(chromium, tmp_path, capsys):
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert sanity.main([str(AUDIT), str(broken)]) == 1
    assert json.loads(capsys.readouterr().out) == {AUDIT.name: [], "broken.json": ["the viewer rendered nothing"]}


def test_the_command_needs_at_least_one_file(capsys):
    with pytest.raises(SystemExit) as exit_info:
        sanity.main([])
    assert exit_info.value.code == 2
    assert "files" in capsys.readouterr().err
