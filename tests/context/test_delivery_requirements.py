"""The delivery requirements summary: the code review file limit setting and the rules it names."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hooks.context import delivery_requirements

ROOT = Path(__file__).resolve().parents[2]
PROBE = (
    "from hooks import config\n"
    "from hooks.context import delivery_requirements\n"
    "print(config.CODEREVIEW_FILELIMIT)\n"
    "print(delivery_requirements.summary())\n"
)


def _fresh_process(tmp_path: Path, limit: str | None) -> list[str]:
    env = {key: value for key, value in os.environ.items() if key != "AGENTIHOOKS_CODEREVIEW_FILELIMIT"}
    env["AGENTIHOOKS_HOME"] = str(tmp_path)
    if limit is not None:
        env["AGENTIHOOKS_CODEREVIEW_FILELIMIT"] = limit
    run = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=60
    )
    return run.stdout.splitlines()


@pytest.mark.feature("delivery-requirements-summary")
def test_the_file_limit_defaults_to_twenty_and_the_summary_names_it(tmp_path: Path) -> None:
    limit, summary = _fresh_process(tmp_path, None)
    assert limit == "20"
    assert "Split it in two only when it changes more than 20 files" in summary


@pytest.mark.feature("delivery-requirements-summary")
def test_an_env_file_limit_changes_the_number_the_summary_names(tmp_path: Path) -> None:
    limit, summary = _fresh_process(tmp_path, "35")
    assert limit == "35"
    assert "Split it in two only when it changes more than 35 files" in summary
    assert "20" not in summary


@pytest.mark.feature("delivery-requirements-summary")
@pytest.mark.parametrize("limit", [1, 20, 9999])
def test_the_summary_stays_within_fifty_words_and_names_both_rules(limit: int) -> None:
    text = delivery_requirements.render(limit)
    assert len(text.split()) <= 50
    assert text.startswith("Delivery requirements: deliver each change as one complete pull request.")
    assert f"Split it in two only when it changes more than {limit} files" in text
    assert "never split smaller for size alone." in text
    assert text.endswith(" Always deliver or offer the source root fix, never a workaround the user did not ask for.")


@pytest.mark.feature("delivery-requirements-summary")
def test_the_summary_reads_the_current_file_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hooks.config.CODEREVIEW_FILELIMIT", 7)
    assert delivery_requirements.summary() == delivery_requirements.render(7)


@pytest.mark.feature("delivery-requirements-summary")
def test_a_full_session_start_budget_keeps_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hooks import config
    from hooks.common import inject_context
    from hooks.targets import emitter

    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(config, "HOOK_INJECTION_MAX_CHARS", 9000)
    monkeypatch.setattr(emitter, "_buffer", [])
    monkeypatch.setattr(emitter, "_forced", True)
    monkeypatch.setattr(emitter, "_plain", True)
    inject_context("PROJECT CONTEXT\n" + "p\n" * 6000, also_log=False, skip_compression=True)
    delivery_requirements.inject_on_session_start()
    emitter.flush("SessionStart")

    assert delivery_requirements.summary() in capsys.readouterr().out
