import io
import sys

import pytest

from scripts import term_colors as colors


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("paint, code", [(colors.blue, "34"), (colors.yellow, "33"), (colors.red, "31")])
def test_a_terminal_gets_each_colour(paint, code):
    assert paint("hi", Tty(), {}) == f"\x1b[{code}mhi\x1b[0m"


@pytest.mark.parametrize("stream, environ", [(io.StringIO(), {}), (Tty(), {"NO_COLOR": "1"})])
def test_a_pipe_or_no_color_gets_the_plain_text(stream, environ):
    assert colors.red("hi", stream, environ) == "hi"


def test_an_empty_no_color_also_turns_colour_off():
    assert colors.blue("hi", Tty(), {"NO_COLOR": ""}) == "hi"


def test_the_defaults_are_read_at_call_time(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdout", Tty())
    assert colors.yellow("hi") == "\x1b[33mhi\x1b[0m"
    monkeypatch.setenv("NO_COLOR", "1")
    assert colors.yellow("hi") == "hi"


@pytest.mark.parametrize("paint, code", [(colors.blue, "34"), (colors.yellow, "33"), (colors.red, "31")])
def test_an_explicit_environment_wins_over_the_process_one(paint, code, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert paint("hi", Tty(), {}) == f"\x1b[{code}mhi\x1b[0m"
