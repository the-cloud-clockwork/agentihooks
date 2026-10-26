import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import project_cache
from hooks.context.brain_adapter import BrainEntry, BrainSourceUnavailable
from hooks.context.project_identity import ProjectIdentity
from hooks.context.project_memory import (
    KernelProjectSource,
    ProjectMemory,
    ScopedFeedRefused,
    VaultProjectSource,
)
from hooks.context.project_sessions import record_session
from tests import sv2_gat02_cases

EVIDENCE = Path(__file__).resolve().parents[2] / "evidence" / "SV2-GAT-02"
INPUTS = json.loads(sv2_gat02_cases.FIXTURE.read_text(encoding="utf-8"))
QUIET = ProjectIdentity(**INPUTS["quiet"])
SCOPED = INPUTS["scoped"]["github.com/org/quiet"]


def _answer(found):
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return copy.deepcopy(found)

    return calls, patch("hooks._brain_http._request", side_effect=request)


def test_the_kernel_source_asks_for_the_canonical_project_and_keeps_only_its_own_rows():
    calls, answer = _answer(SCOPED)
    with answer:
        memory = KernelProjectSource([]).fetch(QUIET)
    assert calls == [
        (
            "GET",
            "/feed",
            {"params": {"project": "github.com/org/quiet"}, "surface_http_errors": True},
        )
    ]
    assert memory == ProjectMemory(
        "quiet",
        arcs=[{"id": "quiet-cold", "heat": 1, "status": "active", "title": "Quiet cold task"}],
        lessons=["Quiet lesson."],
        revision="quiet-r1",
    )


def test_kernel_rows_without_an_arc_reference_or_lesson_text_are_dropped():
    found = {
        **SCOPED,
        "arcs": [
            {"ref": "lesson:x", "projects": ["github.com/org/quiet"], "title": "not an arc"},
            {"ref": "arc:bare", "projects": ["github.com/org/quiet"]},
            "not a row",
        ],
        "lessons": [{"ref": "l", "projects": ["github.com/org/quiet"], "content": "  "}],
    }
    with _answer(found)[1]:
        memory = KernelProjectSource([]).fetch(QUIET)
    assert memory.arcs == [{"id": "bare", "heat": 0, "status": "", "title": ""}]
    assert memory.lessons == []


def test_a_kernel_kind_that_is_not_a_list_is_empty_and_a_missing_revision_is_blank():
    found = {key: value for key, value in SCOPED.items() if key != "revision"} | {"arcs": {}, "lessons": None}
    with _answer(found)[1]:
        assert KernelProjectSource([]).fetch(QUIET) == ProjectMemory("quiet")


def test_an_unknown_project_is_scoped_empty_and_never_asked_for():
    calls, answer = _answer(SCOPED)
    with answer:
        memory = KernelProjectSource([]).fetch(ProjectIdentity("quiet", "org/quiet"))
    assert memory == ProjectMemory("quiet") and calls == []


@pytest.mark.parametrize("key", ["hot_arcs", "inject_blocks", "entries"])
def test_only_the_legacy_fleet_feed_shape_falls_back_to_the_vault(key):
    entries = [BrainEntry("hot-arcs", "Arcs", "rows")]
    source = KernelProjectSource(entries)
    with (
        _answer({key: []})[1],
        patch.object(VaultProjectSource, "fetch", autospec=True, return_value=ProjectMemory("quiet", ["l"])) as vault,
    ):
        assert source.fetch(QUIET) == ProjectMemory("quiet", ["l"])
    assert source.fell_back is True
    assert vault.call_args.args[0].entries is entries and vault.call_args.args[1] == QUIET


def test_a_scoped_answer_does_not_fall_back():
    source = KernelProjectSource([])
    with _answer(SCOPED)[1], patch.object(VaultProjectSource, "fetch") as vault:
        source.fetch(QUIET)
    assert source.fell_back is False
    vault.assert_not_called()


@pytest.mark.parametrize(
    "found",
    [
        {**SCOPED, "project_id": "github.com/org/loud"},
        {**SCOPED, "scope": "fleet"},
        {"scope": "project", "entries": []},
        {},
    ],
)
def test_an_answer_outside_the_requested_project_is_refused(found):
    with _answer(found)[1], pytest.raises(ScopedFeedRefused) as refused:
        KernelProjectSource([]).fetch(QUIET)
    assert str(refused.value) == "project feed answered outside the requested project"


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_is_a_refusal(status):
    with _answer({"__http_status__": status})[1], pytest.raises(ScopedFeedRefused) as refused:
        KernelProjectSource([]).fetch(QUIET)
    assert str(refused.value) == f"project feed refused with {status}"


@pytest.mark.parametrize(
    ("found", "message"),
    [(None, "project feed unavailable"), ({"__http_status__": 502}, "project feed answered 502")],
)
def test_a_transport_failure_is_an_outage(found, message):
    with _answer(found)[1], pytest.raises(BrainSourceUnavailable) as down:
        KernelProjectSource([]).fetch(QUIET)
    assert str(down.value) == message


def test_get_passes_the_http_error_choice_through():
    with patch("hooks._brain_http._request", return_value={"ok": 1}) as request:
        from hooks._brain_http import get

        assert get("/feed", {"a": 1}, surface_http_errors=True) == {"ok": 1}
        get("/feed")
    assert request.call_args_list[0].kwargs == {"params": {"a": 1}, "surface_http_errors": True}
    assert request.call_args_list[1].kwargs == {"params": None, "surface_http_errors": False}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("BRAIN_PROJECT_FEED_SOURCE", raising=False)
    return tmp_path


@pytest.mark.parametrize(("setting", "kind"), [(None, KernelProjectSource), ("vault", VaultProjectSource)])
def test_the_source_setting_selects_the_kernel_by_default_and_the_vault_for_rollback(home, monkeypatch, setting, kind):
    if setting:
        monkeypatch.setenv("BRAIN_PROJECT_FEED_SOURCE", setting)
    source = project_cache._source([{"id": "a", "title": "b", "content": "c"}])
    assert type(source) is kind and source.entries == [BrainEntry("a", "b", "c")]


def _refresh(identity, fetch):
    project_cache.store_feed([])
    feed = project_cache._scoped(project_cache.feed_file(), project_cache.namespace(), "feed")
    scope = project_cache._project_scope(identity)
    with patch.object(KernelProjectSource, "fetch", autospec=True, side_effect=fetch):
        project_cache.refresh_project_cache(identity, [], feed["hash"], scope)
    return project_cache._scoped(project_cache._cache_path(identity, scope), scope, "project-memory")


def test_the_project_scope_names_the_canonical_project(home):
    assert project_cache._project_scope(QUIET).project == "github.com/org/quiet"
    assert project_cache._project_scope(QUIET).brain == project_cache.namespace().brain


def test_a_refusal_replaces_cached_memory_with_empty_memory(home):
    _refresh(QUIET, lambda self, identity: ProjectMemory("quiet", lessons=["old"]))
    with patch("hooks.common.log") as log:
        cached = _refresh(QUIET, lambda self, identity: (_ for _ in ()).throw(ScopedFeedRefused("no")))
    assert cached["memory"] == {"project": "quiet", "arcs": [], "lessons": [], "revision": ""}
    log.assert_called_with("brain_project: scoped feed refused", {"error": "no"})
    assert project_cache.scoped_feed_fallbacks() == {}


def test_an_outage_marks_cached_memory_stale_and_counts_it(home, monkeypatch):
    monkeypatch.setattr(project_cache.time, "time", lambda: 100.0)
    _refresh(QUIET, lambda self, identity: ProjectMemory("quiet", lessons=["kept"]))
    monkeypatch.setenv("BRAIN_PROJECT_MEMORY_TTL", "0")
    monkeypatch.setattr(project_cache.time, "time", lambda: 200.0)
    cached = _refresh(QUIET, lambda self, identity: (_ for _ in ()).throw(BrainSourceUnavailable("down")))
    assert cached["memory"]["lessons"] == ["kept"] and cached["at"] == 100.0 and cached["outage_at"] == 200.0
    assert project_cache.scoped_feed_fallbacks() == {"kernel_outage": 1}
    assert project_cache.scoped_feed_fallback_total() == 1


def test_an_outage_without_cached_memory_writes_nothing(home):
    assert _refresh(QUIET, lambda self, identity: (_ for _ in ()).throw(BrainSourceUnavailable("down"))) == {}
    assert project_cache.scoped_feed_fallbacks() == {}


def test_a_legacy_fallback_is_counted_once_per_refresh(home):
    def legacy(self, identity):
        self.fell_back = True
        return ProjectMemory("quiet")

    _refresh(QUIET, legacy)
    assert project_cache.scoped_feed_fallbacks() == {"legacy_endpoint": 1}
    assert project_cache.scoped_feed_fallback_total() == 1


def _cached(home, monkeypatch, at, outage_at, now):
    record_session("s", QUIET)
    project_cache.store_feed([])
    scope = project_cache._project_scope(QUIET)
    feed = project_cache._scoped(project_cache.feed_file(), project_cache.namespace(), "feed")
    document = {"feed_hash": feed["hash"], "at": at, "memory": {"project": "quiet", "lessons": ["kept"]}}
    if outage_at is not None:
        document["outage_at"] = outage_at
    project_cache._write(
        project_cache._cache_path(QUIET, scope), project_cache.stamp(scope, "project-memory", document)
    )
    monkeypatch.setattr(project_cache.time, "time", lambda: now)
    monkeypatch.setenv("BRAIN_PROJECT_MEMORY_TTL", "1000000")
    with patch("hooks._async.fork_and_call"):
        return project_cache.project_context("s")


def test_an_outage_serves_marked_stale_memory_within_policy(home, monkeypatch):
    monkeypatch.setenv("BRAIN_PROJECT_MEMORY_STALE_SECONDS", "100")
    block = _cached(home, monkeypatch, 1_793_000_000.0, 1_793_000_050.0, 1_793_000_100.0)
    assert block.splitlines()[:4] == [
        "[Project memory: quiet]",
        "BRAIN CONTEXT (recalled state, not an operator directive):",
        "",
        "Stale project memory: the brain is unreachable; last refreshed 2026-10-26T07:33:20+00:00.",
    ]
    assert "* kept" in block


def test_an_outage_past_policy_serves_no_memory(home, monkeypatch):
    monkeypatch.setenv("BRAIN_PROJECT_MEMORY_STALE_SECONDS", "100")
    block = _cached(home, monkeypatch, 1_793_000_000.0, 1_793_000_050.0, 1_793_000_100.5)
    assert "kept" not in block and "Stale" not in block and "No project memory yet" in block


def test_the_stale_policy_defaults_to_a_day(home, monkeypatch):
    monkeypatch.delenv("BRAIN_PROJECT_MEMORY_STALE_SECONDS", raising=False)
    assert "Stale" in _cached(home, monkeypatch, 0.0, 1.0, 86400.0)
    assert "kept" not in _cached(home, monkeypatch, 0.0, 1.0, 86400.5)


def test_memory_without_an_outage_is_not_marked_stale(home, monkeypatch):
    block = _cached(home, monkeypatch, 0.0, None, 10**9)
    assert "Stale" not in block and "* kept" in block


def test_the_plan_line_comes_before_the_stale_line(home, monkeypatch):
    monkeypatch.setattr(project_cache, "_swarm_overview", lambda: "Overview.")
    lines = _cached(home, monkeypatch, 0.0, 1.0, 2.0).splitlines()
    assert lines[3] == "Plan: Overview."
    assert lines[4].startswith("Stale project memory: ")


def test_brain_status_reports_scoped_feed_fallbacks(home):
    from hooks.context.brain_adapter import get_status

    with patch.object(project_cache, "scoped_feed_fallback_total", return_value=3):
        assert get_status()[project_cache.FALLBACK] == 3
    assert project_cache.FALLBACK == "scoped_feed_fallback_total"


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_gat02_cases.run_case(case), sv2_gat02_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_a_failing_case_keeps_its_reproduction(monkeypatch):
    def failing(inputs, root):
        assert root is None

    monkeypatch.setitem(sv2_gat02_cases.CASES, "c", failing)
    record = sv2_gat02_cases.run_case("c")
    assert record["state"] == "failed" and "observed" not in record
    assert record["reproduce"] == {
        "fixture": "tests/fixtures/swarm_v2/scoped-feeds.json",
        "case": "c",
        "line": failing.__code__.co_firstlineno + 1,
        "assertion": "assert root is None",
    }


def test_the_evidence_record_names_its_inputs_cases_and_metric():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, value in sv2_gat02_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == value
    assert manifest["cases"] == ["T-SV2-GAT-02-A", "T-SV2-GAT-02-B", "T-SV2-GAT-02-C"]
    assert result["package"] == "SV2-GAT-02"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    assert set(result["metrics"]) == {project_cache.FALLBACK}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
