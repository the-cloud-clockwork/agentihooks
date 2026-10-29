import json

import pytest

pytestmark = pytest.mark.unit

SENTINEL = "never-published-value"


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def checkout(path, url):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    return path


@pytest.fixture
def machine(tmp_path, monkeypatch):
    from scripts import deps_preflight

    home, state = tmp_path / "home", tmp_path / "home" / ".agentihooks"
    state.mkdir(parents=True)
    (state / ".env").write_text(f"BRAIN_URL={SENTINEL}\nexport LANGFUSE_HOST={SENTINEL}\n")
    (state / "langfuse.env").write_text(f"LANGFUSE_SECRET_KEY={SENTINEL}\n")
    (state / "hive.env").write_text(f"AGENTIHOOKS_HIVE_ID={SENTINEL}\n")
    bundle = checkout(tmp_path / "bundle", "git@github.com:Owner/Bundle.git")
    (state / "state.json").write_text(
        json.dumps(
            {
                "bundle": {"path": str(bundle)},
                "linked_profiles": [{"name": "brain", "path": str(tmp_path / "brain")}],
                "managed_mcp_servers": ["serena", "agentihooks"],
            }
        )
    )
    for folder, names in (("rules", ["worktrees.md", "durability.md"]), ("skills", ["handoff", "dev-cycle"])):
        for name in names:
            (home / ".claude" / folder / name).mkdir(parents=True)
    manifest = tmp_path / "deps.json"
    manifest.write_text(
        json.dumps(
            {
                "deps": [
                    {"id": "herdr", "kind": "system", "check": ["herdr"]},
                    {"id": "uv", "kind": "system", "check": ["uv"]},
                ]
            }
        )
    )
    monkeypatch.setattr(deps_preflight, "manifest_path", lambda: manifest)
    monkeypatch.setattr(deps_preflight, "satisfied", lambda dep: dep.id == "herdr")
    environ = {"AH_CC_TOKEN_one": SENTINEL, "AH_CX_TOKEN_two": SENTINEL, "AH_CC_TOKEN_empty": "", "OTHER": SENTINEL}
    return environ, home, state


def test_the_snapshot_names_the_configuration_without_values_or_host_paths(machine):
    from scripts.hive import config

    environ, home, state = machine
    repos = ["/home/worker/dev/agentihooks", "github.com/owner/agentihooks"]
    snapshot = config.snapshot(environ, home, state, repos)
    assert snapshot == {
        "accounts": ["AH_CC_TOKEN_one", "AH_CX_TOKEN_two"],
        "settings": ["BRAIN_URL", "LANGFUSE_HOST", "LANGFUSE_SECRET_KEY"],
        "bundle": ["github.com/owner/bundle"],
        "profiles": ["brain"],
        "rules": ["durability.md", "worktrees.md"],
        "skills": ["dev-cycle", "handoff"],
        "mcp": ["agentihooks", "serena"],
        "deps": ["herdr"],
        "repos": ["github.com/owner/agentihooks"],
    }
    assert SENTINEL not in json.dumps(snapshot)
    assert str(home) not in json.dumps(snapshot)


def test_an_uninstalled_machine_reports_empty_sections(tmp_path, monkeypatch):
    from scripts import deps_preflight
    from scripts.hive import config

    monkeypatch.setattr(deps_preflight, "manifest_path", lambda: None)
    assert config.snapshot({}, tmp_path, tmp_path / "state", []) == {section: [] for section in config.SECTIONS}


@pytest.mark.parametrize(
    "manifest",
    [
        "{not json",
        '{"deps": [{"id": "x", "kind": "bogus", "check": ["x"]}]}',
        '{"deps": [{"kind": "system"}]}',
        '{"deps": 7}',
        "[]",
    ],
)
def test_an_unreadable_dependency_manifest_reports_no_dependencies(tmp_path, monkeypatch, manifest):
    from scripts import deps_preflight
    from scripts.hive import config

    path = tmp_path / "deps.json"
    path.write_text(manifest)
    monkeypatch.setattr(deps_preflight, "manifest_path", lambda: path)
    assert config.snapshot({}, tmp_path, tmp_path / "state", [])["deps"] == []


def test_differences_name_every_missing_and_extra_entry_per_section():
    from scripts.hive import config

    base = {section: [] for section in config.SECTIONS} | {"accounts": ["a", "b"], "skills": ["s"]}
    other = {section: [] for section in config.SECTIONS} | {"accounts": ["b", "c"], "skills": ["s"]}
    assert config.differences(base, other) == ["accounts missing a", "accounts extra c"]
    assert config.differences(base, base) == []


def test_publish_writes_the_snapshot_once_until_it_expires(redis, tmp_path, monkeypatch):
    import os

    from scripts.hive import config, registry

    calls = []

    def snapshot(environ, home, state, repos):
        calls.append((environ, home, state, repos))
        return {"accounts": [f"call-{len(calls)}"]}

    monkeypatch.setattr(config, "snapshot", snapshot)
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "state"))
    config.publish(redis, "pod", ["github.com/owner/agentihooks"])
    key = f"{registry.key('pod')}:config"
    assert calls == [(os.environ, tmp_path / "home", tmp_path / "state", ["github.com/owner/agentihooks"])]
    assert calls[0][0] is os.environ
    assert config.read(redis, "pod") == {"accounts": ["call-1"]}
    assert config.TTL_SECONDS - 5 < redis.ttl(key) <= config.TTL_SECONDS
    redis.expire(key, config.REFRESH_SECONDS + 5)
    config.publish(redis, "pod", [])
    assert config.read(redis, "pod") == {"accounts": ["call-1"]}
    redis.expire(key, config.REFRESH_SECONDS)
    config.publish(redis, "pod", [])
    assert config.read(redis, "pod") == {"accounts": ["call-2"]}
    assert redis.ttl(f"{registry.key('other')}:config") == -2
    assert config.read(redis, "absent") is None


def live(redis, hive_id, snapshot):
    from scripts.hive import registry

    registry.update(redis, hive_id, [f"name={hive_id}"])
    registry.beat(redis, hive_id, {"heartbeat_at": registry_now()})
    if snapshot is not None:
        redis.set(f"{registry.key(hive_id)}:config", json.dumps(snapshot))


def registry_now():
    import time

    return int(time.time() * 1000)


def full(**sections):
    from scripts.hive import config

    return {section: [] for section in config.SECTIONS} | sections


def test_compare_lists_the_base_and_each_live_hive_with_its_differences(redis, monkeypatch, capsys):
    from scripts.hive import cli, registry

    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    live(redis, "root", full(accounts=["a", "b"], skills=["s"]))
    live(redis, "pod", full(accounts=["a"], skills=["s"]))
    live(redis, "twin", full(accounts=["a", "b"], skills=["s"]))
    live(redis, "silent", None)
    registry.update(redis, "gone", ["name=gone"])
    redis.set(f"{registry.key('gone')}:config", json.dumps(full()))
    assert cli.main(["compare", "--base", "root"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("root\tbase\taccounts=2 settings=0 bundle=0 profiles=0 rules=0 skills=1")
    assert "pod\tdiffers\taccounts=1 settings=0" in "\n".join(out)
    assert "  accounts missing b" in out
    assert "silent\tno configuration published" in out
    assert any(line.startswith("twin\tequal\t") for line in out)
    assert not any(line.startswith("gone") for line in out)


def test_compare_passes_when_every_live_hive_matches_the_local_base(redis, monkeypatch, capsys):
    from scripts.hive import cli

    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr("scripts.swarm.commands.hive_id", lambda: "root")
    live(redis, "root", full(accounts=["a"]))
    live(redis, "pod", full(accounts=["a"]))
    assert cli.main(["compare"]) == 0
    assert capsys.readouterr().out.splitlines()[1].startswith("pod\tequal\t")


@pytest.mark.parametrize(("other", "code"), [(None, 1), (full(accounts=["b"]), 1), (full(accounts=["a"]), 0)])
def test_compare_exits_one_for_a_single_silent_or_differing_hive(redis, monkeypatch, other, code):
    from scripts.hive import cli

    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    live(redis, "root", full(accounts=["a"]))
    live(redis, "pod", other)
    assert cli.main(["compare", "--base", "root"]) == code


def test_compare_refuses_a_base_that_is_not_live(redis, monkeypatch, capsys):
    from scripts.hive import cli

    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    live(redis, "pod", full())
    assert cli.main(["compare", "--base", "absent"]) == 1
    assert "hive compare refused: live hive absent published no configuration" in capsys.readouterr().err


def test_compare_help_names_the_command_and_its_base():
    import argparse

    from scripts.hive import cli

    parser = cli._parser()
    sub = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert {choice.dest: choice.help for choice in sub._choices_actions}["compare"] == (
        "List each live hive's configuration against a base hive's"
    )
    base = next(action for action in sub.choices["compare"]._actions if action.dest == "base")
    assert base.help == "The base hive id; defaults to this machine's hive"


def test_compare_refuses_a_base_without_a_published_configuration(redis, monkeypatch, capsys):
    from scripts.hive import cli

    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    live(redis, "root", None)
    assert cli.main(["compare", "--base", "root"]) == 1
    assert "hive compare refused: live hive root published no configuration" in capsys.readouterr().err
