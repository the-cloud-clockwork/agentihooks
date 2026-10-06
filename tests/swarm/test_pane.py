from pathlib import Path

import pytest

from scripts.swarm.pane import selection_prompt, typed_input


@pytest.mark.parametrize("arrow", ["❯", "›", "→", ">"])
@pytest.mark.parametrize("footer", ["Enter to confirm", "Press Enter to select", "ENTER to continue · Esc to cancel"])
def test_selection_prompts(arrow, footer):
    text = f"Do you trust the files in this folder?\n{arrow} 1. Yes\n  2. No\n{footer}"
    assert selection_prompt(text) == "Do you trust the files in this folder?"


def test_real_import_capture_with_ansi():
    capture = (Path(__file__).parents[1] / "fixtures/swarm/claude-import-prompt.txt").read_text()
    assert selection_prompt(f"\x1b[32m{capture}\x1b[0m") == "Allow external CLAUDE.md file imports?"


def test_selection_dialog_can_have_a_cancel_footer_after_confirmation():
    text = "Do you trust the files in this folder?\n❯ 1. Yes\n  2. No\nEnter to confirm\nEsc to cancel"
    assert selection_prompt(text) == "Do you trust the files in this folder?"


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


RULE = "\x1b[0m\x1b[38;2;136;136;136m" + "─" * 40
ECHO = (
    "\x1b[38;2;153;153;153m\x1b[48;2;55;55;55m❯ \x1b[0m\x1b[38;2;255;255;255m\x1b[48;2;55;55;55mhow is the swarm\x1b[0m"
)
STATUS = "  \x1b[38;2;153;153;153m25% | Opus 5.5\x1b[0m\n  ⏵⏵ bypass permissions on"


def claude(*box):
    return "\n".join([ECHO, "", "  The swarm runs four agents.", "", RULE, *box, RULE, STATUS])


@pytest.mark.parametrize(
    "capture, typed",
    [
        (claude("❯\xa0"), ""),
        (claude('❯\xa0\x1b[2mTry "fix lint errors"\x1b[0m'), ""),
        (claude("❯\xa0wait, pause the"), "wait, pause the"),
        (claude("❯\xa0\x1b[38;2;255;255;255mfirst line\x1b[0m", "  second line"), "first line\nsecond line"),
        (claude("❯\xa0", "  still typing"), "still typing"),
        ("Working on code\n› \x1b[2mAsk Codex to do anything\x1b[0m\n  100% context left", ""),
        ("Working on code\n› stop after this\n  100% context left", "stop after this"),
        ("", ""),
        ("  The swarm runs four agents.", ""),
        ("❯ \x1b[2mhint\x1b[22mtyped", "typed"),
        ("❯ \x1b[2mhint\x1b[mtyped", "typed"),
        ("❯ \x1b[38;5;2mgreen\x1b[0m", "green"),
        ("❯ \x1b[48;5;2mshaded\x1b[0m", "shaded"),
        ("❯ \x1b[58;2;1;2;3munderlined\x1b[0m", "underlined"),
        ("❯ \x1b[1;2mbold hint\x1b[0m", ""),
        ("❯ \x1b[2mhint\x1b[0mtyped", "typed"),
        ("❯ \x1b[1;38;5;2mbold green\x1b[0m", "bold green"),
        ("❯ \x1b[38;5;9;2mhint\x1b[0m", ""),
        ("❯ \x1b[38;2;1;1;1;2mhint\x1b[0m", ""),
        ("❯ typed\x1b[2m hint\x1b[38;5;9m still hint\x1b[0m", "typed"),
        ("❯ typed\x1b[2m hint", "typed"),
        ("❯ \x1b[?25ltyped", "typed"),
        ("❯ \x1b[38mcut short", "cut short"),
        ("❯ \x1b[38;5mcut short", "cut short"),
        ("❯ \x1b[38;2;1mcut short", "cut short"),
        ("❯ pick a › b", "pick a › b"),
        ("❯ typed\x1b[2m hint\n" + RULE + "\nstatus\x1b[0m", "typed"),
    ],
)
def test_typed_input_is_the_text_on_the_last_input_line_without_dim_hints(capture, typed):
    assert typed_input(capture) == typed
