from hooks import config
from hooks.classifier import down_cache


def test_marker_lives_under_agentihooks_home():
    assert down_cache.marker_path() == config.AGENTIHOOKS_HOME / "classifier" / "api-down"


def test_mark_down_creates_missing_parents_and_can_repeat(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", tmp_path / "deep" / "home")
    down_cache.mark_down()
    down_cache.mark_down()
    assert down_cache.is_down(60)


def test_down_ends_exactly_at_the_ttl(monkeypatch):
    down_cache.mark_down()
    marked = down_cache.marker_path().stat().st_mtime
    monkeypatch.setattr(down_cache.time, "time", lambda: marked + 60)
    assert not down_cache.is_down(60)
    monkeypatch.setattr(down_cache.time, "time", lambda: marked + 59.9)
    assert down_cache.is_down(60)


def test_no_marker_is_not_down():
    assert not down_cache.is_down(60)


def test_clear_removes_the_marker_and_tolerates_none():
    down_cache.clear()
    down_cache.mark_down()
    down_cache.clear()
    assert not down_cache.marker_path().exists()
