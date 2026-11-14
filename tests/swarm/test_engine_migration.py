import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.swarm import engine_migration
from scripts.swarm import store as store_module
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

ORIGIN = "github.com/the-cloud-clockwork/agentihooks"


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    path = tmp_path / "source"
    git = path / ".git"
    git.mkdir(parents=True)
    (git / "config").write_text('[remote "origin"]\n\turl = git@github.com:The-Cloud-Clockwork/agentihooks.git\n')
    return path


def configs(store: RedisStore) -> dict[str, tuple[str, str]]:
    return {slug: (store.config(slug).engine, store.config(slug).repo_origin) for slug in engine_migration.slugs(store)}


@pytest.mark.feature("engine-config-migration")
def test_every_stored_legacy_config_moves_to_events_with_an_origin_the_doctor_included(
    store: RedisStore, checkout: Path
) -> None:
    store.create(SwarmConfig("watch", str(checkout), 1, 1))
    store.create(SwarmConfig("watch-doctor", "/missing", 1, 0, template="doctor"))
    store.create(replace(SwarmConfig("pinned", "/missing", 1, 1), engine="events", repo_origin="github.com/team/other"))
    store.create(replace(SwarmConfig("stored", "/missing", 1, 1), repo_origin="github.com/team/stored"))

    assert engine_migration.migrate(store) == {
        "pinned": "unchanged",
        "stored": "migrated: github.com/team/stored",
        "watch": f"migrated: {ORIGIN}",
        "watch-doctor": f"migrated: {ORIGIN}",
    }
    assert configs(store) == {
        "pinned": ("events", "github.com/team/other"),
        "stored": ("events", "github.com/team/stored"),
        "watch": ("events", ORIGIN),
        "watch-doctor": ("events", ORIGIN),
    }
    assert engine_migration.migrate(store) == dict.fromkeys(("pinned", "stored", "watch", "watch-doctor"), "unchanged")


@pytest.mark.feature("engine-config-migration")
def test_a_doctor_takes_the_origin_of_its_peer_and_a_watched_swarm_keeps_its_own(
    store: RedisStore, checkout: Path
) -> None:
    store.create(replace(SwarmConfig("watch", "/missing", 1, 1), repo_origin="github.com/team/watched"))
    store.create(SwarmConfig("other-doctor", str(checkout), 1, 0, template="doctor"))
    store.set_peer("watch", "other-doctor")
    store.set_peer("other-doctor", "watch")

    engine_migration.migrate(store)

    assert configs(store) == {
        "other-doctor": ("events", "github.com/team/watched"),
        "watch": ("events", "github.com/team/watched"),
    }
    assert engine_migration.watched(store, "watch") == ""
    assert engine_migration.watched(store, "other-doctor") == "watch"


@pytest.mark.feature("engine-config-migration")
def test_a_doctor_keeps_a_stored_origin(store: RedisStore) -> None:
    store.create(replace(SwarmConfig("watch", "/missing", 1, 1), repo_origin="github.com/team/watched"))
    store.create(
        replace(SwarmConfig("watch-doctor", "/missing", 1, 0, template="doctor"), repo_origin="github.com/team/own")
    )

    assert engine_migration.migrate(store)["watch-doctor"] == "migrated: github.com/team/own"
    assert configs(store)["watch-doctor"] == ("events", "github.com/team/own")


@pytest.mark.feature("engine-config-migration")
def test_a_swarm_without_any_origin_stays_legacy_and_is_reported(store: RedisStore) -> None:
    store.create(SwarmConfig("orphan", "/missing", 1, 1))
    store.create(SwarmConfig("orphan-doctor", "/missing", 1, 0, template="doctor"))

    assert engine_migration.migrate(store) == dict.fromkeys(
        ("orphan", "orphan-doctor"), "skipped: no repository origin"
    )
    assert configs(store) == {"orphan": ("legacy", ""), "orphan-doctor": ("legacy", "")}


@pytest.mark.feature("engine-config-migration")
def test_a_doctor_takes_its_own_origin_when_the_watched_swarm_has_none(store: RedisStore, checkout: Path) -> None:
    store.create(SwarmConfig("bare", "/missing", 1, 1))
    store.create(SwarmConfig("bare-doctor", str(checkout), 1, 0, template="doctor"))

    assert engine_migration.migrate(store) == {
        "bare": "skipped: no repository origin",
        "bare-doctor": f"migrated: {ORIGIN}",
    }
    assert configs(store) == {"bare": ("legacy", ""), "bare-doctor": ("events", ORIGIN)}


@pytest.mark.feature("engine-config-migration")
def test_a_swarm_that_cannot_load_is_reported_and_the_rest_still_migrate(store: RedisStore) -> None:
    store.create(SwarmConfig("gone-doctor", "/missing", 1, 0, template="doctor"))
    store.set_peer("gone-doctor", "gone")
    store.redis.sadd(f"{store_module.PREFIX}:index", "ghost")
    store.create(replace(SwarmConfig("zeta", "/missing", 1, 1), repo_origin="github.com/team/project"))

    outcomes = engine_migration.migrate(store)

    assert outcomes == {
        "ghost": "skipped: no swarm ghost",
        "gone-doctor": "skipped: no repository origin",
        "zeta": "migrated: github.com/team/project",
    }


@pytest.mark.feature("engine-config-migration")
def test_a_new_doctor_starts_on_events_with_the_watched_origin(store: RedisStore) -> None:
    store.create(replace(SwarmConfig("watch", "/missing", 1, 1), repo_origin="github.com/team/watched"))
    store.create(SwarmConfig("watch-doctor", "/missing", 1, 0, template="doctor"))
    store.create(SwarmConfig("other", "/missing", 1, 1))

    engine_migration.start_on_events(store, "watch-doctor", "watch")

    assert configs(store) == {
        "other": ("legacy", ""),
        "watch": ("legacy", "github.com/team/watched"),
        "watch-doctor": ("events", "github.com/team/watched"),
    }


@pytest.mark.feature("engine-config-migration")
def test_a_new_doctor_takes_its_own_origin_when_the_watched_swarm_has_none_or_is_not_stored(
    store: RedisStore, checkout: Path
) -> None:
    store.create(SwarmConfig("watch", "/missing", 1, 1))
    store.create(SwarmConfig("watch-doctor", str(checkout), 1, 0, template="doctor"))
    store.create(SwarmConfig("ledger-only-doctor", str(checkout), 1, 0, template="doctor"))
    store.create(SwarmConfig("lost-doctor", "/missing", 1, 0, template="doctor"))

    engine_migration.start_on_events(store, "watch-doctor", "watch")
    engine_migration.start_on_events(store, "ledger-only-doctor", "ledger-only")
    engine_migration.start_on_events(store, "lost-doctor", "lost")

    assert configs(store) == {
        "ledger-only-doctor": ("events", ORIGIN),
        "lost-doctor": ("legacy", ""),
        "watch": ("legacy", ""),
        "watch-doctor": ("events", ORIGIN),
    }
    assert engine_migration.watched(store, "lost-doctor") == ""
    assert engine_migration.watched(store, "watch-doctor") == "watch"


@pytest.mark.feature("engine-config-migration")
def test_main_migrates_the_connected_store_and_prints_each_outcome(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.create(replace(SwarmConfig("sw", "/missing", 1, 1), repo_origin="github.com/team/project"))
    monkeypatch.setattr(engine_migration.store, "connect", lambda: store)

    engine_migration.main([])

    assert capsys.readouterr().out == '{"sw": "migrated: github.com/team/project"}\n'
    assert configs(store) == {"sw": ("events", "github.com/team/project")}
