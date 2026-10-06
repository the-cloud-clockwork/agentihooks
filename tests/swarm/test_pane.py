from pathlib import Path

import pytest

from scripts.swarm.pane import selection_prompt


@pytest.mark.parametrize("arrow", ["❯", "›", "→", ">"])
@pytest.mark.parametrize("footer", ["Enter to confirm", "Press Enter to select", "ENTER to continue · Esc to cancel"])
def test_selection_prompts(arrow, footer):
    text = f"Do you trust the files in this folder?\n{arrow} 1. Yes\n  2. No\n{footer}"
    assert selection_prompt(text) == "Do you trust the files in this folder?"


def test_real_import_capture_with_ansi():
    capture = (Path(__file__).parents[1] / "fixtures/swarm/claude-import-prompt.txt").read_text()
    assert selection_prompt(f"\x1b[32m{capture}\x1b[0m") == "Allow external CLAUDE.md file imports?"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Working on code\n› Ask Codex to do anything",
        "Do you trust this folder?\n  1. Yes\nEnter to confirm",
        "Do you trust this folder?\n❯ Yes\nWorking",
        "❯ Yes\nEnter to confirm",
        "Old prompt?\n❯ Yes\nEnter to confirm\nRunning a tool",
    ],
)
def test_non_dialog_content_is_not_waiting(text):
    assert selection_prompt(text) == ""
