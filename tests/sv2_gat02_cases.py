import copy
import hashlib
import json
import os
import sys
import tempfile
import traceback
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from hooks.context import project_cache
from hooks.context.brain_adapter import BrainEntry
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_memory import _arc_rows
from hooks.context.project_sessions import record_session

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/scoped-feeds.json"
EVIDENCE_CLASS = (
    "mocked: an in-process fake brain HTTP surface answering the fixture's scoped, legacy, empty, refused and "
    "outage responses stands in for brain-api; hooks run in a temporary home with a fake clock; no network call runs"
)
START = 1_793_000_000.0


class Brain:
    def __init__(self, inputs):
        self.inputs = inputs
        self.mode = "scoped"
        self.scoped = copy.deepcopy(inputs["scoped"])
        self.calls = []

    def request(self, method, path, *, params=None, surface_http_errors=False, **_):
        self.calls.append("feed" if path == "/feed" else "vault")
        if self.mode == "outage":
            return None
        if path == "/feed":
            if self.mode == "refused":
                return {"__http_status__": 403} if surface_http_errors else None
            if self.mode == "legacy":
                return copy.deepcopy(self.inputs["legacy"])
            return copy.deepcopy(self.scoped[params["project"]])
        vault = self.inputs["legacy_vault"]
        return copy.deepcopy(vault.get(params.get("path", params.get("q"))))


class Clock:
    def __init__(self):
        self.now = START

    def time(self):
        return self.now


class Rig:
    def __init__(self, inputs, root, stack):
        self.inputs, self.root = inputs, root
        self.brain, self.clock = Brain(inputs), Clock()
        environ = {key: value for key, value in os.environ.items() if not key.startswith("BRAIN_PROJECT")}
        environ.pop("AGENTIHOOKS_SWARM", None)
        environ["BRAIN_PROJECT_MEMORY_STALE_SECONDS"] = str(inputs["stale_seconds"])
        for item in (
            patch.dict(os.environ, environ, clear=True),
            patch("hooks.config.AGENTIHOOKS_HOME", root),
            patch("hooks.config.BRAIN_URL", inputs["brain_url"]),
            patch("hooks._brain_http._request", self.brain.request),
            patch("hooks._async.fork_and_call", side_effect=lambda fn, *args, **_: fn(*args)),
            patch("hooks.context.project_cache.time", self.clock),
        ):
            stack.enter_context(item)
        project_cache.store_feed([BrainEntry(**row) for row in inputs["fleet_entries"]])

    def session(self, name, project):
        identity = ProjectIdentity(**self.inputs[project])
        record_session(name, identity)
        return identity

    def deliver(self, name):
        project_cache.project_context(name)
        return project_cache.project_context(name)

    def snapshot(self):
        return {
            str(item.relative_to(self.root)): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in sorted(self.root.rglob("*"))
            if item.is_file() and item.suffix == ".json" and item.name.startswith("k")
        }


def _arcs(block):
    return [line.split("[")[-1].rstrip("]") for line in block.splitlines() if line.startswith("* ") and "[" in line]


def _lessons(block):
    lines = block.splitlines()
    if "Recent lessons:" not in lines:
        return []
    return [line[2:] for line in lines[lines.index("Recent lessons:") + 1 :] if line.startswith("* ")]


def _delivery(block):
    return {"arcs": _arcs(block), "lessons": _lessons(block), "fleet_warning_in_block": "Fleet wide" in block}


def _scoped_delivery(inputs, root):
    with ExitStack() as stack:
        rig = Rig(inputs, root, stack)
        rig.session("quiet-session", "quiet")
        block = rig.deliver("quiet-session")
        fleet_top = [row["id"] for row in _arc_rows([BrainEntry(**row) for row in inputs["fleet_entries"]])]
        return {
            "delivery": _delivery(block),
            "fleet_top_set": fleet_top,
            "requests": rig.brain.calls,
            project_cache.FALLBACK: project_cache.scoped_feed_fallbacks(),
        }


def _legacy_delivery(inputs, root):
    with ExitStack() as stack:
        rig = Rig(inputs, root, stack)
        rig.brain.mode = "legacy"
        project_cache.store_feed([BrainEntry(**row) for row in inputs["legacy"]["entries"]])
        rig.session("legacy-session", "quiet")
        block = rig.deliver("legacy-session")
        return {
            "delivery": _delivery(block),
            "requests": rig.brain.calls,
            project_cache.FALLBACK: project_cache.scoped_feed_fallbacks(),
        }


def positive(inputs, root):
    first = _scoped_delivery(inputs, root / "first")
    second = _scoped_delivery(inputs, root / "second")
    assert first == second
    assert first["delivery"]["arcs"] == ["quiet-cold"]
    assert "quiet-cold" not in first["fleet_top_set"]
    assert first["delivery"]["lessons"] == ["Quiet lesson."]
    assert first["delivery"]["fleet_warning_in_block"] is False
    assert first["requests"] == ["feed"]
    assert first[project_cache.FALLBACK] == {}
    legacy = _legacy_delivery(inputs, root / "legacy")
    assert legacy["delivery"]["arcs"] == ["quiet-legacy"]
    assert legacy[project_cache.FALLBACK] == {"legacy_endpoint": 1}
    return {"scoped": first, "second_fixture_identical": True, "legacy_endpoint": legacy}


def rejection(inputs, root):
    with ExitStack() as stack:
        rig = Rig(inputs, root, stack)
        rig.session("empty-session", "empty")
        rig.session("quiet-session", "quiet")
        before = rig.snapshot()
        empty = rig.deliver("empty-session")
        assert _delivery(empty) == {"arcs": [], "lessons": [], "fleet_warning_in_block": False}
        assert "No project memory yet" in empty
        assert rig.brain.calls == ["feed"]
        assert {path: digest for path, digest in rig.snapshot().items() if path in before} == before
        rig.brain.scoped["github.com/org/quiet"] = {
            **inputs["scoped"]["github.com/org/quiet"],
            "project_id": "github.com/org/loud",
        }
        foreign = rig.deliver("quiet-session")
        assert _delivery(foreign)["arcs"] == []
        rig.brain.scoped = copy.deepcopy(inputs["scoped"])
        rig.clock.now += 3600 * 24
        rig.deliver("quiet-session")
        rig.brain.mode = "refused"
        rig.clock.now += 3600 * 24
        refused = rig.deliver("quiet-session")
        assert _delivery(refused)["arcs"] == []
        assert "vault" not in rig.brain.calls
        rig.brain.mode = "scoped"
        corrected = rig.deliver("quiet-session")
        assert _delivery(corrected)["arcs"] == []
        rig.clock.now += 3600 * 24
        renewed = rig.deliver("quiet-session")
        assert _delivery(renewed)["arcs"] == ["quiet-cold"]
        assert project_cache.scoped_feed_fallbacks() == {}
        return {
            "empty_project": _delivery(empty),
            "answer_for_another_project": _delivery(foreign),
            "refused_after_cached_memory": _delivery(refused),
            "corrected_before_expiry": _delivery(corrected),
            "corrected_after_new_request": _delivery(renewed),
            "vault_requests": rig.brain.calls.count("vault"),
            "protected_state_unchanged": True,
            project_cache.FALLBACK: project_cache.scoped_feed_fallbacks(),
        }


def recovery(inputs, root):
    with ExitStack() as stack:
        rig = Rig(inputs, root, stack)
        identity = rig.session("quiet-session", "quiet")
        fresh = rig.deliver("quiet-session")
        cache = project_cache._cache_path(identity, project_cache._project_scope(identity))
        rig.brain.mode = "outage"
        rig.clock.now += 3600 / 2
        os.environ["BRAIN_PROJECT_MEMORY_TTL"] = "60"
        stale = rig.deliver("quiet-session")
        assert _delivery(stale)["arcs"] == ["quiet-cold"]
        assert "Stale project memory" in stale
        assert "vault" not in rig.brain.calls
        assert project_cache._cache_path(identity, project_cache._project_scope(identity)) == cache
        rig.clock.now += inputs["stale_seconds"]
        expired = rig.deliver("quiet-session")
        assert _delivery(expired)["arcs"] == []
        rig.brain.mode = "scoped"
        rig.brain.scoped["github.com/org/quiet"].update(inputs["restored_revision"])
        restored = rig.deliver("quiet-session")
        assert _delivery(restored)["arcs"] == ["quiet-next"] and "Stale" not in restored
        stored = json.loads(cache.read_text())
        project_cache.refresh_project_cache(
            identity,
            inputs["fleet_entries"],
            "obsolete-feed-hash",
            project_cache._project_scope(identity),
        )
        assert json.loads(cache.read_text()) == stored
        assert stored["memory"]["revision"] == "quiet-r2"
        return {
            "fresh": _delivery(fresh),
            "outage_within_policy": _delivery(stale) | {"marked_stale": True},
            "outage_past_policy": _delivery(expired),
            "restored": _delivery(restored),
            "cache_scope_unchanged": True,
            "obsolete_refresh_ignored": True,
            "stored_revision": stored["memory"]["revision"],
            "vault_requests": rig.brain.calls.count("vault"),
            project_cache.FALLBACK: project_cache.scoped_feed_fallbacks(),
        }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    record = {
        "case": f"T-SV2-GAT-02-{case.upper()}",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
    }
    with tempfile.TemporaryDirectory() as folder:
        try:
            observed = CASES[case](inputs, Path(folder))
        except AssertionError:
            failed = traceback.extract_tb(sys.exc_info()[2])[-1]
            reproduce = {"fixture": f"tests/fixtures/swarm_v2/{FIXTURE.name}", "case": case, "line": failed.lineno}
            return {**record, "state": "failed", "reproduce": {**reproduce, "assertion": failed.line}}
    return {**record, "state": "passed", "observed": observed}
