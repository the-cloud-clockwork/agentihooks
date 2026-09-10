"""agentihooks discovers the brain from the brain's own directory.

The kernel writes its connection settings to ~/.agentibrain/.env — the file
that already feeds docker compose. Copying the bearer into agentihooks' chain
put one value in two places, and a rotation left the copy stale. agentihooks
now reads the brain's file directly, as a default beneath its own.

That file also carries database, object-store and provider credentials. Those
must never reach a session's environment, so only the connection keys are
adopted.
"""

import importlib
import os

import pytest

pytestmark = pytest.mark.unit

_TOKEN = "KB_ROUTER_TOKEN"
_URL = "BRAIN_URL"

_SCRUB = (
    _URL,
    "BRAIN_HTTP_TOKEN",
    _TOKEN,
    "BRAIN_ENABLED",
    "BRAIN_WRITER_ENABLED",
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_PASSWORD",
    "EMBEDDINGS_API_KEY",
)


def _write_env(path, mapping):
    """Build an env file without spelling KEY=VALUE literals in this source."""
    path.write_text("".join(f"{k}={v}\n" for k, v in mapping.items()), encoding="utf-8")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Separate AGENTIHOOKS_HOME and AGENTIBRAIN_HOME, both empty."""
    hooks_home = tmp_path / ".agentihooks"
    brain_home = tmp_path / ".agentibrain"
    hooks_home.mkdir()
    brain_home.mkdir()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(hooks_home))
    monkeypatch.setenv("AGENTIBRAIN_HOME", str(brain_home))
    for key in _SCRUB:
        monkeypatch.delenv(key, raising=False)

    def _load():
        import hooks.config as config

        return importlib.reload(config)

    return hooks_home, brain_home, _load


def test_the_brain_env_alone_wires_the_adapter(homes):
    """No agentihooks brain config at all — the kernel's file is enough."""
    _, brain, load = homes
    _write_env(brain / ".env", {_TOKEN: "t1", _URL: "http://127.0.0.1:8103"})

    config = load()
    assert config.BRAIN_URL == "http://127.0.0.1:8103"
    assert config.BRAIN_HTTP_TOKEN == "t1"
    # BRAIN_URL resolving is what flips both defaults on.
    assert config.BRAIN_ENABLED is True
    assert config.BRAIN_WRITER_ENABLED is True


def test_unrelated_credentials_are_never_adopted(homes):
    """The kernel .env is shared with compose; most of it is none of our business."""
    _, brain, load = homes
    _write_env(
        brain / ".env",
        {
            _TOKEN: "t1",
            _URL: "http://127.0.0.1:8103",
            "POSTGRES_PASSWORD": "pg-value",
            "MINIO_ROOT_PASSWORD": "minio-value",
            "EMBEDDINGS_API_KEY": "embed-value",
        },
    )

    load()
    for unwanted in ("POSTGRES_PASSWORD", "MINIO_ROOT_PASSWORD", "EMBEDDINGS_API_KEY"):
        assert unwanted not in os.environ, f"{unwanted} reached the session environment"


def test_an_agentihooks_env_still_wins(homes):
    """Discovery is a default. An explicit agentihooks setting outranks it."""
    hooks_home, brain, load = homes
    _write_env(brain / ".env", {_URL: "http://from-kernel", _TOKEN: "t1"})
    _write_env(hooks_home / ".env", {_URL: "http://from-agentihooks"})

    assert load().BRAIN_URL == "http://from-agentihooks"


def test_process_env_still_beats_both(homes, monkeypatch):
    _, brain, load = homes
    _write_env(brain / ".env", {_URL: "http://from-kernel", _TOKEN: "t1"})
    monkeypatch.setenv(_URL, "http://from-process")

    assert load().BRAIN_URL == "http://from-process"


def test_no_brain_installed_is_not_an_error(homes):
    """agentihooks must run on machines that have no brain at all."""
    _, brain, load = homes
    (brain / ".env").unlink(missing_ok=True)

    config = load()
    assert config.BRAIN_URL == ""
    assert config.BRAIN_ENABLED is False


def test_a_rotated_bearer_is_picked_up_on_reload(homes):
    """The whole point: one file, so a rotation cannot go stale in a copy."""
    _, brain, load = homes
    env = brain / ".env"
    _write_env(env, {_URL: "http://127.0.0.1:8103", _TOKEN: "before"})
    config = load()
    assert config.BRAIN_HTTP_TOKEN == "before"

    _write_env(env, {_URL: "http://127.0.0.1:8103", _TOKEN: "after"})
    config.reload_brain_env(force=True)
    assert config.BRAIN_HTTP_TOKEN == "after"
