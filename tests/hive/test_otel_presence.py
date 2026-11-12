from types import SimpleNamespace

import pytest

from scripts.hive import otel_presence

NOW = 1_800_000_000.0
EXPOSITION = f"""# HELP agentihooks_presence_hive
# TYPE agentihooks_presence_hive gauge
agentihooks_presence_hive{{hive_headed="yes",hive_id="auto",hive_name="automation",hive_release="9.1",job="agentihooks"}} {NOW - 30:.6e}
agentihooks_presence_hive{{hive_headed="no",hive_id="anton-1",hive_name="anton one",hive_release="9.0",job="agentihooks"}} {NOW - 60}
agentihooks_presence_hive{{hive_headed="no",hive_id="gone",hive_name="gone",hive_release="8.0",job="agentihooks"}} {NOW - 181}
agentihooks_presence_hive_agents{{harness="claude",hive_headed="yes",hive_id="auto",hive_name="automation",hive_release="9.1"}} 4
agentihooks_presence_hive_agents{{harness="codex",hive_headed="yes",hive_id="auto",hive_name="automation",hive_release="9.1"}} 1
agentihooks_presence_hive_agents{{harness="claude",hive_headed="no",hive_id="anton-1",hive_name="anton one",hive_release="9.0"}} 2
agentihooks_presence_hive_agents{{harness="claude",hive_headed="no",hive_id="gone",hive_name="gone",hive_release="8.0"}} 7
agentihooks_presence_hive_agents{{hive_id="auto"}} 9
agentihooks_presence_hive{{hive_id="broken"}} NaNish
other_metric{{hive_id="auto"}} 3
"""


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    monkeypatch.setattr(otel_presence, "_cache", {})


def test_fresh_hives_carry_their_name_flag_release_and_agents_by_harness():
    assert otel_presence.hives(EXPOSITION, NOW) == [
        {"id": "anton-1", "name": "anton one", "headed": False, "release": "9.0", "agents": {"claude": 2}},
        {"id": "auto", "name": "automation", "headed": True, "release": "9.1", "agents": {"claude": 4, "codex": 1}},
    ]


def test_a_hive_seen_exactly_at_the_fresh_limit_still_counts():
    text = f'agentihooks_presence_hive{{hive_id="edge"}} {NOW - otel_presence.FRESH_S}\n'
    assert otel_presence.hives(text, NOW) == [
        {"id": "edge", "name": "edge", "headed": False, "release": "", "agents": {}}
    ]


def test_samples_skip_comments_and_unparsable_values_and_keep_a_trailing_timestamp():
    text = '# c\nm{a="1",b="x y"} 2.5 1700000000000\nm{a="2"} bad\nbare 3\n'
    assert otel_presence.samples(text) == [("m", {"a": "1", "b": "x y"}, 2.5)]


def test_read_is_none_when_no_url_is_configured():
    assert otel_presence.read({}, NOW, get=lambda url: pytest.fail("fetched")) is None


def test_read_is_none_when_the_collector_is_silent():
    assert otel_presence.read({otel_presence.URL_ENV: "http://c/metrics"}, NOW, get=lambda url: None) is None


def test_read_fetches_the_configured_url_once_within_the_cache_window():
    fetched = []

    def get(url):
        fetched.append(url)
        return EXPOSITION

    env = {otel_presence.URL_ENV: "http://c/metrics"}
    first, second = otel_presence.read(env, NOW, get=get), otel_presence.read(env, NOW, get=get)
    assert first is not None and second is not None
    assert [hive["id"] for hive in first] == [hive["id"] for hive in second] == ["anton-1", "auto"]
    assert fetched == ["http://c/metrics"]


def test_read_fetches_again_after_the_cache_window(monkeypatch):
    clock = iter([100.0, 100.0 + otel_presence.CACHE_S, 100.0 + otel_presence.CACHE_S])
    monkeypatch.setattr(otel_presence, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    fetched = []
    env = {otel_presence.URL_ENV: "http://c/metrics"}
    otel_presence.read(env, NOW, get=lambda url: fetched.append(url) or "")
    otel_presence.read(env, NOW, get=lambda url: fetched.append(url) or "")
    assert fetched == ["http://c/metrics", "http://c/metrics"]


def test_fetch_returns_none_when_the_collector_does_not_answer(monkeypatch):
    def refuse(url, timeout):
        assert timeout == otel_presence.TIMEOUT_S
        raise OSError("refused")

    monkeypatch.setattr(otel_presence, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=refuse)))
    assert otel_presence.fetch("http://c/metrics") is None


def test_fetch_returns_the_decoded_body(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"m 1\n"

    opener = SimpleNamespace(urlopen=lambda url, timeout: Response())
    monkeypatch.setattr(otel_presence, "urllib", SimpleNamespace(request=opener))
    assert otel_presence.fetch("http://c/metrics") == "m 1\n"
