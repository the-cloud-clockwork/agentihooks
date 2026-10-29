from scripts.swarm import tick
from scripts.swarm.store import SwarmConfig


def _pass(repo):
    return tick._Pass("sw", SwarmConfig("sw", repo, 1, 1), None, None, None, {}, {}, 0)


def test_a_spawn_pass_lists_the_remote_heads_once_and_the_next_pass_lists_again(monkeypatch):
    listed = []
    monkeypatch.setattr(tick, "remote_heads", lambda repo: listed.append(repo) or {"b": repo})
    run = _pass("/repo")
    first, second = tick._pass_reclaim(run), tick._pass_reclaim(run)
    assert listed == ["/repo"]
    assert first.func is tick.reclaim
    assert first.keywords == second.keywords == {"heads": {"b": "/repo"}}
    assert tick._pass_reclaim(_pass("/other")).keywords == {"heads": {"b": "/other"}}
    assert listed == ["/repo", "/other"]


def test_an_unreadable_listing_leaves_each_reclaim_to_read_its_own_heads(monkeypatch):
    listed = []
    monkeypatch.setattr(tick, "remote_heads", lambda repo: listed.append(repo))
    run = _pass("/repo")
    assert tick._pass_reclaim(run).keywords == {"heads": None}
    assert tick._pass_reclaim(run).keywords == {"heads": None}
    assert listed == ["/repo"]
