import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

import hooks.config
from hooks.context import project_sessions
from hooks.context.brain_writer_hook import _parse_transcript_for_markers

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evidence/SV2-IDN-03"
FIXTURE_PATH = ROOT / "tests/fixtures/swarm_v2/session-scope.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text())
GRANT = project_sessions.SessionGrant(frozenset(FIXTURE["grant"]["project_ids"]))
EVIDENCE_CLASS = "local isolated fixture in a temporary agentihooks home; not live rollout proof"
KEYS = ("attribution", "project_id", "worktree", "task", "task_revision", "lane")


def _home(folder: Path) -> None:
    hooks.config.AGENTIHOOKS_HOME = folder


def _record(session: str) -> list:
    return [project_sessions.record_scope(session, item["scope"], item["at"], GRANT) for item in FIXTURE["transitions"]]


def _attributed(session: str, folder: Path) -> list[dict]:
    path = folder / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in FIXTURE["transcript"]) + "\n")
    markers = _parse_transcript_for_markers(str(path), 20)
    events = [{"at": marker.get("at"), "attrs": marker["attrs"]} for marker in markers]
    return project_sessions.attribute(session, events, GRANT)


def _projected(results: list[dict]) -> list[dict]:
    return [{key: result[key] for key in KEYS if result.get(key)} for result in results]


def case_a(folder: Path) -> dict:
    runs = []
    for index, session in enumerate(("first", "second")):
        _home(folder / f"home-{index}")
        _record(session)
        results = _attributed(session, folder)
        runs.append(
            {
                "session": session,
                "transitions": len(project_sessions.transitions(session)),
                "attribution": _projected(results),
                "unattributed_session_events_total": project_sessions.unattributed_session_events_total(results),
            }
        )
    return {
        "then": "a session that changes task halfway has correctly scoped events on both sides of each transition",
        "expected": FIXTURE["expected"],
        "runs": runs,
        "passed": all(run["attribution"] == FIXTURE["expected"] for run in runs),
    }


def case_b(folder: Path) -> dict:
    _home(folder / "home-b")
    _record("first")
    gamma = folder / "gamma"
    gamma.mkdir()
    path = project_sessions._scope_path("first")
    before = path.read_bytes()
    try:
        project_sessions.record_scope(
            "first", {**FIXTURE["outside_grant"], "cwd": str(gamma)}, "2026-10-08T11:00:00+00:00", GRANT
        )
        refusal = None
    except project_sessions.ScopeRefused as refused:
        refusal = str(refused)
    claim = {"at": "2026-10-08T10:05:00Z", "attrs": {"project_id": FIXTURE["outside_grant"]["project_id"]}}
    marker = project_sessions.attribute("first", [claim], GRANT)
    return {
        "then": "a worker supplied project outside the launch grant is rejected even when the folder exists locally",
        "folder_exists": gamma.is_dir(),
        "refusal": refusal,
        "refusal_discloses_project": refusal is not None and "gamma" in refusal,
        "scope_log_unchanged": path.read_bytes() == before,
        "explicit_marker": marker,
        "unattributed_session_events_total": project_sessions.unattributed_session_events_total(marker),
        "passed": refusal is not None
        and "gamma" not in refusal
        and path.read_bytes() == before
        and marker == [{"attribution": "refused"}],
    }


def case_c(folder: Path) -> dict:
    _home(folder / "home-c")
    _record("first")
    path = project_sessions._scope_path("first")
    before = path.read_bytes()
    replay = _record("first")
    results = _attributed("first", folder)
    _home(folder / "home-interrupted")
    first, second = FIXTURE["transitions"][:2]
    project_sessions.record_scope("partial", first["scope"], first["at"])
    with project_sessions._scope_path("partial").open("a") as stream:
        stream.write('{"session_id": "partial", "sequ')
    resumed = project_sessions.record_scope("partial", second["scope"], second["at"])
    return {
        "then": "replaying metadata transitions reconstructs the same attribution without duplicate session entries",
        "replay_results": replay,
        "scope_log_unchanged_after_replay": path.read_bytes() == before,
        "attribution": _projected(results),
        "unattributed_session_events_total": project_sessions.unattributed_session_events_total(results),
        "interrupted_write": {
            "accepted_after_partial_line": resumed is not None and resumed["sequence"] == 1,
            "readable_transitions": [row["worktree"] for row in project_sessions.transitions("partial")],
            "survived": "accepted transitions before the partial line",
            "not_survived": "the partial line, which is skipped and never replaced by a success label",
        },
        "passed": replay == [None] * 4 and path.read_bytes() == before and _projected(results) == FIXTURE["expected"],
    }


def main() -> None:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True)
    tested = commit.stdout.strip()
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        for case, run in (("a", case_a), ("b", case_b), ("c", case_c)):
            result = {"case": f"T-SV2-IDN-03-{case.upper()}", "tested_commit": tested, "evidence_class": EVIDENCE_CLASS}
            result |= run(folder / case)
            (OUTPUT / f"{case}-result.json").write_text(json.dumps(result, indent=2) + "\n")
    inputs = (
        "tests/fixtures/swarm_v2/session-scope.json",
        "hooks/context/project_sessions.py",
        "hooks/context/brain_writer_hook.py",
        "docs/swarm-v2/schemas/session-scope.json",
        "tests/context/test_session_scope.py",
    )
    manifest = {
        "tested_commit": tested,
        "evidence_class": EVIDENCE_CLASS,
        "inputs": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in inputs},
    }
    (OUTPUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
