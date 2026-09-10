"""Brain reader and writer must default on for the deployment that configures them.

Both defaults used to be derived from evidence that an HTTP deployment never
produces: the reader from `.md` files under `$AGENTIHOOKS_HOME/brain-feed`, the
writer from a bare "false". In an HTTP deployment the feed is served by
brain-api out of the vault and no local feed directory is ever created, so a
machine wired correctly — BRAIN_URL set, token published, profile linked —
resolved both flags false and injected nothing while reporting no error.

The legacy filesystem default is kept: a brain-feed directory with content
still enables the reader on its own.
"""

import importlib

import pytest

pytestmark = pytest.mark.unit

_BRAIN_KEYS = (
    "BRAIN_URL",
    "BRAIN_ENABLED",
    "BRAIN_WRITER_ENABLED",
    "BRAIN_HTTP_TOKEN",
    "KB_ROUTER_TOKEN",
    "BRAIN_SOURCE_PATH",
)


@pytest.fixture
def brain_env(tmp_path, monkeypatch):
    """A throwaway AGENTIHOOKS_HOME with every brain key cleared."""
    home = tmp_path / ".agentihooks"
    home.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(home))
    for key in _BRAIN_KEYS:
        monkeypatch.delenv(key, raising=False)

    def _load():
        import hooks.config as config

        return importlib.reload(config)

    return home, _load


def test_both_off_without_a_brain_url(brain_env):
    _, load = brain_env
    config = load()

    assert config.BRAIN_ENABLED is False
    assert config.BRAIN_WRITER_ENABLED is False


def test_brain_url_turns_reader_and_writer_on(brain_env, monkeypatch):
    _, load = brain_env
    monkeypatch.setenv("BRAIN_URL", "http://127.0.0.1:8103")
    config = load()

    assert config.BRAIN_ENABLED is True
    assert config.BRAIN_WRITER_ENABLED is True


@pytest.mark.parametrize("flag", ["BRAIN_ENABLED", "BRAIN_WRITER_ENABLED"])
def test_explicit_false_beats_the_url_default(brain_env, monkeypatch, flag):
    _, load = brain_env
    monkeypatch.setenv("BRAIN_URL", "http://127.0.0.1:8103")
    monkeypatch.setenv(flag, "false")

    assert getattr(load(), flag) is False


def test_legacy_feed_directory_still_enables_the_reader(brain_env):
    home, load = brain_env
    feed = home / "brain-feed"
    feed.mkdir()
    (feed / "hot-arcs.md").write_text("# hot arcs\n")

    config = load()
    assert config.BRAIN_ENABLED is True
    # No URL, so the writer has nowhere to POST and stays off.
    assert config.BRAIN_WRITER_ENABLED is False


def test_reload_agrees_with_boot(brain_env, monkeypatch):
    _, load = brain_env
    monkeypatch.setenv("BRAIN_URL", "http://127.0.0.1:8103")
    config = load()

    reloaded = config.reload_brain_env(force=True)
    assert reloaded["brain_enabled"] is True
    assert reloaded["brain_writer_enabled"] is True


def test_env_file_url_turns_them_on_too(brain_env):
    """The token and URL usually arrive as a file, not as shell env."""
    home, load = brain_env
    (home / "agentibrain.env").write_text("BRAIN_URL=http://127.0.0.1:8103\nBRAIN_HTTP_TOKEN=t0ken\n")

    config = load()
    assert config.BRAIN_ENABLED is True
    assert config.BRAIN_WRITER_ENABLED is True


class _StubSource:
    def fetch(self):
        return []


def _adapter_with_stub_source(monkeypatch):
    import hooks.context.brain_adapter as adapter

    importlib.reload(adapter)
    monkeypatch.setattr(adapter, "_get_source", lambda: _StubSource())
    return adapter


def test_status_warns_when_a_source_resolved_but_the_adapter_is_off(brain_env, monkeypatch):
    """The failure that reads as health: every field correct, nothing injected."""
    _, load = brain_env
    monkeypatch.setenv("BRAIN_URL", "http://127.0.0.1:8103")
    monkeypatch.setenv("BRAIN_HTTP_TOKEN", "t0ken")
    monkeypatch.setenv("BRAIN_ENABLED", "false")
    load()

    status = _adapter_with_stub_source(monkeypatch).get_status()

    assert status["enabled"] is False
    assert any("BRAIN_ENABLED is false" in w for w in status["warnings"])


def test_status_warns_when_a_url_carries_no_token(brain_env, monkeypatch):
    _, load = brain_env
    monkeypatch.setenv("BRAIN_URL", "http://127.0.0.1:8103")
    load()

    status = _adapter_with_stub_source(monkeypatch).get_status()

    assert any("401" in w for w in status["warnings"])


def test_status_is_quiet_when_correctly_wired(brain_env, monkeypatch):
    _, load = brain_env
    monkeypatch.setenv("BRAIN_URL", "http://127.0.0.1:8103")
    monkeypatch.setenv("BRAIN_HTTP_TOKEN", "t0ken")
    load()

    status = _adapter_with_stub_source(monkeypatch).get_status()

    assert status["warnings"] == []
