"""The voice summarizer speaks prose only: hashes, code, links and extra whitespace never reach its prompt."""

import subprocess
from unittest.mock import Mock

import pytest

from hooks.context import voice_output


@pytest.fixture
def summarizer(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Capture the summarizer command instead of starting the claude CLI."""
    run = Mock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="Done.\n", stderr=""))
    monkeypatch.setattr(voice_output.subprocess, "run", run)
    monkeypatch.setattr(voice_output, "_get_summarizer_prefix", lambda: "SAY: ")
    monkeypatch.setattr(voice_output, "_get_summarizer_model", lambda: "model-x")
    return run


def test_prompt_drops_hashes_code_links_and_extra_whitespace(summarizer: Mock) -> None:
    """Every noise class is stripped and whitespace collapses to single spaces."""
    text = "Merged abc1234def into dev.\n\n```py\nx = 1\n```  See `wt.sh` at https://example.com/pr/1   now."

    assert voice_output._summarize(text) == "Done."

    command = summarizer.call_args.args[0]
    assert command == ["claude", "SAY: Merged into dev. See at now.", "-p", "--model", "model-x"]
