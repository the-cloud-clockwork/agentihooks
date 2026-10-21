import json
from unittest.mock import Mock

import fakeredis
import pytest

from scripts.doctor import ci, ci_read


def attempt(monkeypatch, cache, tests, number=1):
    monkeypatch.setattr(ci_read, "_pages", lambda *args: [{"status": "completed", "conclusion": "success"}])
    monkeypatch.setattr(ci_read, "_job_tests", lambda *args: tests)
    return ci_read._attempt("o/r", {"id": 42, "head_sha": "head"}, number, Mock(), cache)


def key(number):
    return f"{ci_read.ROOT}:doctor:ci-attempt:o/r:42:{number}"


def test_write_compresses_all_observations_without_changing_results(monkeypatch):
    cache = fakeredis.FakeRedis(decode_responses=True)
    tests = [{"job": "unit", "nodeid": f"tests/test_x.py::test_{n}", "outcome": "PASSED"} for n in range(2000)]
    expected = attempt(monkeypatch, None, tests)
    assert attempt(monkeypatch, cache, tests) == expected
    assert cache.strlen(key(1)) < len(json.dumps(expected).encode()) // 4
    assert 3590 <= cache.ttl(key(1)) <= 3600
    monkeypatch.setattr(ci_read, "_pages", Mock(side_effect=AssertionError("cache hit fetched jobs")))
    assert ci_read._attempt("o/r", {"id": 42, "head_sha": "head"}, 1, Mock(), cache) == expected


def test_legacy_hit_preserves_flaky_findings_and_trims_other_oversized_keys(monkeypatch):
    cache = fakeredis.FakeRedis(decode_responses=True)
    failed = {"job": "unit", "nodeid": "tests/test_x.py::test_x", "outcome": "FAILED"}
    first = {"run_id": 42, "head_sha": "head", "attempt": 1, "tests": [failed, failed]}
    cache.set(key(1), json.dumps(first), ex=604800)
    cache.set(key(9), "x" * (1024 * 1024 + 1), ex=604800)
    cache.set("unrelated", "x" * (1024 * 1024 + 1), ex=604800)
    assert attempt(monkeypatch, cache, []) == first
    assert cache.strlen(key(1)) < len(json.dumps(first))
    assert 3590 <= cache.ttl(key(1)) <= 3600
    assert not cache.exists(key(9))
    assert cache.exists("unrelated")
    passed = {**failed, "outcome": "PASSED"}
    second = attempt(monkeypatch, cache, [passed], number=2)
    record = {"pr": {"number": 7, "head": {"sha": "head"}}, "attempts": [first, second]}
    assert len(ci.flaky_tests(record)) == 1
    assert ci.flaky_tests({**record, "attempts": [attempt(monkeypatch, cache, []), second]}) == ci.flaky_tests(record)


def test_write_bounds_total_storage_and_shortens_only_long_expiries(monkeypatch):
    cache = fakeredis.FakeRedis(decode_responses=True)
    for number in range(2, 20):
        cache.set(key(number), "x" * (1024 * 1024), ex=604800)
    cache.set(key(0), "{}", ex=20)
    cache.set(key(99), "{}")
    cache.set(key(100), "x" * (1024 * 1024 + 1), ex=604800)
    expected = attempt(monkeypatch, cache, [])
    keys = list(cache.scan_iter(match=f"{ci_read.ROOT}:doctor:ci-attempt:*"))
    assert sum(cache.strlen(k) for k in keys) <= 16 * 1024 * 1024
    assert max(cache.strlen(k) for k in keys) <= 1024 * 1024
    assert cache.get(key(2)) is not None
    assert not cache.exists(key(100))
    assert 0 < cache.ttl(key(0)) <= 20
    assert all(0 < cache.ttl(k) <= 3600 for k in keys)
    monkeypatch.setattr(ci_read, "_pages", Mock(side_effect=AssertionError("current entry evicted")))
    assert ci_read._attempt("o/r", {"id": 42, "head_sha": "head"}, 1, Mock(), cache) == expected


@pytest.mark.parametrize("delta,retained", [(0, True), (1, False)])
def test_encoded_entry_byte_boundary(monkeypatch, delta, retained):
    from scripts.doctor import ci_cache

    cache = fakeredis.FakeRedis(decode_responses=True)
    payload = {"tests": []}
    encoded = "x" * (1024 * 1024 + delta)
    monkeypatch.setattr(ci_cache, "encode", lambda value: encoded)
    ci_cache.write(cache, key(1), payload)
    assert bool(cache.exists(key(1))) is retained


def test_uncacheable_attempt_is_returned_in_full_and_removes_old_copy(monkeypatch):
    from scripts.doctor import ci_cache

    cache = fakeredis.FakeRedis(decode_responses=True)
    tests = [{"job": "unit", "nodeid": "tests/test_x.py::test_x", "outcome": "FAILED"}]
    expected = attempt(monkeypatch, None, tests)
    cache.set(key(1), json.dumps(expected), ex=604800)
    monkeypatch.setattr(ci_cache, "encode", lambda value: "x" * (1024 * 1024 + 1))
    assert attempt(monkeypatch, cache, []) == expected
    assert not cache.exists(key(1))
