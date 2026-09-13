"""Precedence rules for the ~/.agentihooks/*.env loader.

Three rules, and every one of them has been wrong at some point:

  1. the surrounding process env beats every file — a stale PVC-persisted .env
     must never mask a Helm env change;
  2. a later file in a load pass beats an earlier one, which is what the
     loader's docstring has always promised;
  3. a reload agrees with a boot, so a long-lived process that re-reads the
     files lands on the same value a fresh process would.

Rule 2 was broken for as long as the loader used ``setdefault`` (first file
won, silently), and a reload that overrode file-owned keys then disagreed with
boot in the opposite direction — the same config resolving two ways depending
on how the process got there.
"""

import importlib
import os

import pytest


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    """A throwaway AGENTIHOOKS_HOME with a freshly imported config module."""
    home = tmp_path / ".agentihooks"
    home.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    for key in ("BRAIN_URL", "BRAIN_SOURCE_PATH", "SOME_KEY"):
        monkeypatch.delenv(key, raising=False)

    def _load():
        import hooks.config as config

        return importlib.reload(config)

    return home, _load


def test_later_file_beats_earlier_file(env_home):
    home, load = env_home
    (home / ".env").write_text("SOME_KEY=from-main\n")
    (home / "zz-extra.env").write_text("SOME_KEY=from-companion\n")

    load()
    assert os.environ["SOME_KEY"] == "from-companion"


def test_process_env_beats_every_file(env_home, monkeypatch):
    home, load = env_home
    (home / ".env").write_text("SOME_KEY=from-main\n")
    (home / "zz-extra.env").write_text("SOME_KEY=from-companion\n")
    monkeypatch.setenv("SOME_KEY", "from-process")

    config = load()
    assert os.environ["SOME_KEY"] == "from-process"
    # ...and a forced reload must not quietly hand the files a second chance.
    config.reload_brain_env(force=True)
    assert os.environ["SOME_KEY"] == "from-process"


def test_reload_agrees_with_boot(env_home):
    """A reloaded process resolves the same value a fresh one would."""
    home, load = env_home
    (home / ".env").write_text("SOME_KEY=from-main\n")
    (home / "zz-extra.env").write_text("SOME_KEY=from-companion\n")
    config = load()
    assert os.environ["SOME_KEY"] == "from-companion"

    (home / "zz-extra.env").write_text("SOME_KEY=edited\n")
    os.utime(home / "zz-extra.env", None)
    config.reload_brain_env()
    assert os.environ["SOME_KEY"] == "edited"
    load()
    assert os.environ["SOME_KEY"] == "edited"


def test_reload_is_a_noop_while_the_files_are_untouched(env_home):
    """The gate that keeps a reload from clobbering caller-set values."""
    home, load = env_home
    (home / ".env").write_text("BRAIN_URL=http://from-main\n")
    config = load()

    config.BRAIN_URL = "http://set-by-caller"
    result = config.reload_brain_env()

    assert result["reloaded"] is False
    assert config.BRAIN_URL == "http://set-by-caller"


def test_missing_env_directory_leaves_defaults_alone(env_home):
    """A deployment with no .env at all must not be reset on every status call."""
    _, load = env_home
    config = load()
    config.BRAIN_URL = "http://set-by-caller"

    assert config.reload_brain_env()["reloaded"] is False
    assert config.BRAIN_URL == "http://set-by-caller"
