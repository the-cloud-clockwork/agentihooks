import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import project_cache
from hooks.context.brain_adapter import BrainEntry
from hooks.context.brain_writer_hook import _drain_outbox, _marker_request, _write_to_outbox
from hooks.context.project_sessions import record_session
from scripts.swarm_v2 import keyspace
from tests.test_swarm_v2_keyspace import (
    FIXTURE,
    IDENTITY,
    SESSION,
    context,
    files,
    forge,
    install,
    legacy_files,
    marker,
    paired,
    refresh,
    scope,
    use_brain,
)

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evidence/SV2-IDN-05"
EVIDENCE_CLASS = (
    "local isolated fixture on temporary homes with a fixed installation time; mocked: the brain vault fetch, "
    "the background refresh fork and the brain HTTP post; not live rollout proof"
)
INPUTS = (
    "tests/fixtures/swarm_v2/keyspace-golden.json",
    "hooks/_redis.py",
    "hooks/context/controls_toggle.py",
    "tests/fixtures/swarm_v2/keyspace.json",
    "scripts/swarm_v2/keyspace.py",
    "hooks/context/project_cache.py",
    "hooks/context/brain_adapter.py",
    "hooks/context/brain_writer_hook.py",
    "hooks/context/project_sessions.py",
    "tests/test_swarm_v2_keyspace.py",
    "evidence/SV2-IDN-05/generate_case_results.py",
)


def world(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    home = root / FIXTURE["home"]
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", home)
    monkeypatch.setattr("hooks.context.brain_adapter._HASH_CACHE_FILE", home / "brain_feed_hash")
    monkeypatch.setattr("hooks.common.LOG_FILE", str(root / "hooks.log"))
    monkeypatch.setattr("hooks.config.BRAIN_SOURCE_TYPE", "none")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", FIXTURE["swarm"])
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", FIXTURE["task"])
    monkeypatch.delenv("BRAIN_PROJECT_SCOPE", raising=False)
    return home


def case_a() -> dict:
    runs = []
    for _ in range(2):
        with pytest.MonkeyPatch.context() as monkeypatch, tempfile.TemporaryDirectory() as root:
            pair = paired(monkeypatch, world(monkeypatch, Path(root)))
        personal, swarm = pair["personal"], pair["swarm"]
        runs.append(
            {
                "same_home_path": True,
                "installations_differ": personal["installation"] != swarm["installation"],
                "brains_differ": personal["brain"] != swarm["brain"],
                "marker_keys_differ": personal["marker_key"] != swarm["marker_key"],
                "cache_keys_shared": len(set(personal["caches"]) & set(swarm["caches"])),
                "each_reads_its_own_context": all(f"{name} lesson" in run["context"] for name, run in pair.items()),
                "each_takes_its_own_pending": all(run["pending"] == f"{name} pending" for name, run in pair.items()),
                "secret_free": all(run["secret_free"] for run in pair.values()),
                "cache_scope_mismatch_total": [run["mismatches"] for run in pair.values()],
            }
        )
    passed = all(
        run["installations_differ"]
        and run["brains_differ"]
        and run["marker_keys_differ"]
        and run["cache_keys_shared"] == 0
        and run["each_reads_its_own_context"]
        and run["each_takes_its_own_pending"]
        and run["secret_free"]
        and run["cache_scope_mismatch_total"] == [0, 0]
        for run in runs
    )
    return {
        "then": "two installations with the same native session ID and path produce independent cache and archive identities",
        "runs": runs,
        "passed": passed,
    }


def case_b() -> dict:
    with pytest.MonkeyPatch.context() as monkeypatch, tempfile.TemporaryDirectory() as root:
        home = world(monkeypatch, Path(root))
        install(home)
        record_session(SESSION, IDENTITY)
        use_brain(monkeypatch, "personal")
        refresh("personal lesson")
        project_cache.defer_project_context(SESSION, "personal pending")
        use_brain(monkeypatch, "swarm")
        project_cache.store_feed([BrainEntry("hot-arcs", "Arcs", "swarm feed")])
        forge(home, "personal", "swarm", "feed", monkeypatch)
        forge(home, "personal", "swarm", "project-memory", monkeypatch)
        use_brain(monkeypatch, "swarm")
        before = {name: body for name, body in files(home).items() if keyspace.NAMESPACED.fullmatch(Path(name).name)}
        with patch("hooks._async.fork_and_call") as fork:
            served = project_cache.project_context(SESSION) or ""
        pending = project_cache.take_project_context(SESSION)
        after = {name: body for name, body in files(home).items() if keyspace.NAMESPACED.fullmatch(Path(name).name)}
        log = (Path(root) / "hooks.log").read_text() if (Path(root) / "hooks.log").exists() else ""
        result = {
            "other_brain_lesson_served": "personal lesson" in served,
            "other_brain_pending_served": pending is not None,
            "refresh_started_from_forged_feed": fork.called,
            "protected_cache_unchanged": before == after,
            "cache_scope_mismatch_total": project_cache.cache_scope_mismatch_total(),
            "log_discloses_brain_url": any(
                part in log for part in ("brain.personal.example", "fixture-secret", "fixture-token")
            ),
        }
    passed = (
        not result["other_brain_lesson_served"]
        and not result["other_brain_pending_served"]
        and not result["refresh_started_from_forged_feed"]
        and result["protected_cache_unchanged"]
        and result["cache_scope_mismatch_total"] == 2
        and not result["log_discloses_brain_url"]
    )
    return {
        "then": "a cache entry from a different brain cannot satisfy a targeted context request",
        "result": result,
        "passed": passed,
    }


def case_c() -> dict:
    with pytest.MonkeyPatch.context() as monkeypatch, tempfile.TemporaryDirectory() as root:
        home = world(monkeypatch, Path(root))
        install(home)
        record_session(SESSION, IDENTITY)
        use_brain(monkeypatch, "swarm")
        state = home / "brain" / "project-memory"
        legacy = legacy_files(state, 3)
        stale_served = "stale lesson" in context() or project_cache.take_project_context(SESSION) is not None
        coexisting = all(path.exists() for path in legacy)
        refresh("swarm lesson")
        current_served = "swarm lesson" in context()
        legacy_swept = not any(path.exists() for path in legacy)
        content = FIXTURE["marker"]["content"]
        old = _marker_request({**marker(FIXTURE["legacy_marker_at"]), "scope": scope()}, SESSION)[1]
        new = _marker_request({**marker(), "scope": scope()}, SESSION)[1]
        repeat = _marker_request({**marker(), "scope": scope()}, SESSION)[1]
        outbox = Path(root) / "outbox"
        _write_to_outbox([{**marker(), "scope": scope()}], SESSION, str(outbox))
        use_brain(monkeypatch, "personal")
        with (
            patch("hooks._brain_http.brain_http_enabled", return_value=True),
            patch("hooks._brain_http.post", return_value={"ok": True}) as post,
        ):
            _drain_outbox(str(outbox))
        replayed = post.call_args.kwargs["idempotency_key"]
        durable = {
            name: body for name, body in files(home).items() if not keyspace.NAMESPACED.fullmatch(Path(name).name)
        }
        dropped = keyspace.drop_namespaced(state)
        result = {
            "legacy_entries_coexist": coexisting,
            "legacy_read_as_current": stale_served,
            "new_namespace_serves_current": current_served,
            "legacy_entries_swept_after_refresh": legacy_swept,
            "pre_cutover_marker_keeps_legacy_key": old == keyspace.legacy_marker_key(SESSION, "lesson", content),
            "post_cutover_marker_key_is_namespaced": new != old,
            "marker_key_stable_across_stops": new == repeat,
            "outbox_replay_keeps_first_key_after_brain_change": replayed == new,
            "rollback_dropped_new_caches": dropped,
            "rollback_kept_durable_records": files(home) == durable,
            "cache_scope_mismatch_total": project_cache.cache_scope_mismatch_total(),
        }
    passed = (
        result["legacy_entries_coexist"]
        and not result["legacy_read_as_current"]
        and result["new_namespace_serves_current"]
        and result["legacy_entries_swept_after_refresh"]
        and result["pre_cutover_marker_keeps_legacy_key"]
        and result["post_cutover_marker_key_is_namespaced"]
        and result["marker_key_stable_across_stops"]
        and result["outbox_replay_keeps_first_key_after_brain_change"]
        and result["rollback_dropped_new_caches"] == 2
        and result["rollback_kept_durable_records"]
        and result["cache_scope_mismatch_total"] == 0
    )
    return {
        "then": "old and new cache namespaces coexist during rollout without reading stale data as current authority",
        "result": result,
        "passed": passed,
    }


def main() -> int:
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *INPUTS], cwd=ROOT).returncode:
        print("commit the case inputs first: results must name the commit that holds them")
        return 2
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    outcomes = {}
    for name, case in (("a", case_a), ("b", case_b), ("c", case_c)):
        result = {"tested_commit": commit, "evidence_class": EVIDENCE_CLASS, **case()}
        outcomes[name] = result["passed"]
        (OUTPUT / f"{name}-result.json").write_text(json.dumps(result, indent=2) + "\n")
    manifest = {
        "tested_commit": commit,
        "evidence_class": EVIDENCE_CLASS,
        "inputs": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in INPUTS},
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(outcomes))
    return 0 if all(outcomes.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
