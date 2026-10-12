import contextlib
import copy
import hashlib
import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from scripts.swarm_v2 import resource_bench as bench
from scripts.swarm_v2 import resource_profiles as profiles

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2" / "worker-resources"
INPUTS = ("measurements.json", "repeat.json", "update.json", "nodes.json", "disclosure.json")
POLICY = profiles.Policy()
REVIEWER = "fixture-operator"
EVIDENCE_CLASS = (
    "measured: the workloads ran on a GitHub hosted Ubuntu runner in the Worker resources workflow named in "
    "measurements.json, repeat.json and update.json, and node capacity was read from the Anton cluster with kubectl "
    "as named in nodes.json; derivation, coexistence, promotion and rollback are functions over those committed "
    "records run on a temporary accepted file; no worker Pod ran the workloads and no profile was applied to a "
    "deployed Pod policy"
)
NOTES = (
    "Measured on a GitHub hosted runner, not inside a worker Pod: CPU peaks are bounded by the runner's cores.",
    "The agent CLI's own resident memory is a declared reserve, not a measurement; it needs a live agent run.",
    "The idle class is a sleeping Python process standing in for an idle agent CLI.",
    "With nine runs per class the p95 column equals the maximum.",
    "Descendants that leave the workload's session and process tree are neither counted nor killed.",
    "A child that starts and exits between two samples never enters a peak window; CPU seconds still count it.",
    "A workload change is detected through its declared version, not a fingerprint of its command.",
    "The sampler walks the workload folder on every sample; that cost runs outside the measured tree.",
    "Coexistence counts whole workers in each node's allocatable capacity minus the requests already scheduled there.",
)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def nodes() -> list[dict]:
    return load("nodes.json")["nodes"]


def derive(measured: dict) -> dict:
    return profiles.derive(copy.deepcopy(measured), nodes(), POLICY)


def results_page() -> str:
    return profiles.markdown(derive(load("measurements.json")), NOTES)


def _summary(report: dict) -> dict:
    return {
        "generation": report["generation"],
        "profiles": {name: built["limits"] for name, built in report["profiles"].items()},
        "samples": {name: built["uncertainty"]["samples"] for name, built in report["profiles"].items()},
        "refused": report["refused"],
        "fits": {row["node"]: row["fits"] for row in report["coexistence"]},
        "worker_process_tree_peak_memory": report["worker_process_tree_peak_memory"],
    }


def _floor(measured: dict, name: str) -> int:
    return POLICY.min_repeats * len({r["children"] for r in measured["runs"] if r["class"] == name})


def _positive() -> tuple[dict, bool]:
    measured, repeat = load("measurements.json"), load("repeat.json")
    first, again, second = derive(measured), derive(measured), derive(repeat)
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "accepted.json"
        decision = profiles.accept(path, first, POLICY, "")["decision"]
        stored = json.loads(path.read_text())
    admitted = set(first["profiles"])
    observed = {
        **_summary(first),
        "deterministic_rederivation": first == again,
        "independent_repeat": _summary(second),
        "repeat_changes": profiles.compare(first, second, POLICY),
        "accepted": decision,
        "accepted_file_matches": stored == first,
    }
    checks = [
        admitted == set(profiles.CLASSES) == set(second["profiles"]),
        all(first["profiles"][n]["measured"]["peak_rss_mib"]["n"] >= _floor(measured, n) for n in admitted),
        first["refused"] == second["refused"] == {"idle": profiles.IDLE_REFUSAL},
        len(first["coexistence"]) == len(second["coexistence"]) == len(nodes()),
        all(set(row["fits"]) == admitted for row in first["coexistence"]),
        first == again,
        observed["repeat_changes"] == {"material": [], "missing": [], "tools_changed": {}},
        decision == "promoted" and stored == first,
    ]
    return observed, all(checks)


def _rejection() -> tuple[dict, bool]:
    measured = load("measurements.json")
    idle = {**measured, "runs": [r for r in measured["runs"] if r["class"] == profiles.IDLE]}
    sized = derive(measured)
    forged = {**sized, "generation": sized["generation"] + 1, "profiles": {**sized["profiles"]}}
    forged["profiles"][profiles.IDLE] = sized["profiles"]["memory"]
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        accepted, report = root / "accepted.json", root / "report.json"
        profiles.accept(accepted, sized, POLICY, "")
        before = accepted.read_bytes()
        (root / "idle.json").write_text(json.dumps(idle))
        (root / "nodes.json").write_text(json.dumps(load("nodes.json")))
        args = ["derive", "--measurements", str(root / "idle.json"), "--nodes", str(root / "nodes.json")]
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = bench.main([*args, "--output", str(report)])
        (root / "forged.json").write_text(json.dumps(forged))
        promote = ["promote", "--accepted", str(accepted), "--candidate", str(root / "forged.json")]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            forged_code = bench.main([*promote, "--requalified-by", REVIEWER])
        written = sorted(p.name for p in root.iterdir())
        unchanged = accepted.read_bytes() == before
        texts = {p.name: p.read_text() for p in root.iterdir()}
    secret = "-".join(load("disclosure.json")["fake_secret_parts"])
    texts.update({f"fixture {name}": (FIXTURES / name).read_text() for name in INPUTS if name != "disclosure.json"})
    texts["refusal"] = stderr.getvalue()
    texts["forged_decision"] = stdout.getvalue()
    observed = {
        "idle_runs": len(idle["runs"]),
        "idle_peak_rss_mib": max(r["peak_rss_mib"] for r in idle["runs"]),
        "exit_code": code,
        "refusal": stderr.getvalue().strip(),
        "forged_idle_profile": json.loads(stdout.getvalue()),
        "forged_exit_code": forged_code,
        "files_after_refusal": written,
        "accepted_unchanged": unchanged,
        "scanned_for_secret": sorted(texts),
        "fake_secret_in": sorted(name for name, text in texts.items() if secret in text),
        "fake_secret_set_in_run": load("disclosure.json")["variable"],
    }
    checks = [
        len(idle["runs"]) >= POLICY.min_repeats,
        code == 2,
        observed["refusal"] == f"refused: no class was admitted: idle: {profiles.IDLE_REFUSAL}",
        observed["forged_idle_profile"] == {"decision": "refused", "changes": {"unsized": [profiles.IDLE]}},
        forged_code == 1,
        written == ["accepted.json", "forged.json", "idle.json", "nodes.json"],
        unchanged,
        observed["fake_secret_in"] == [],
    ]
    return observed, all(checks)


def _updated(measured: dict, update: dict) -> dict:
    classes = {r["class"] for r in update["runs"]}
    runs = [r for r in measured["runs"] if r["class"] not in classes] + update["runs"]
    return {
        **measured,
        "generation": update["generation"],
        "tools": {**measured["tools"], **update["tools"]},
        "runs": runs,
    }


def _interrupted(path: Path, candidate: dict) -> tuple[str, bool]:
    before = path.read_bytes()
    with mock.patch.object(Path, "replace", side_effect=OSError("interrupted before install")):
        try:
            profiles.accept(path, candidate, POLICY, REVIEWER)
            outcome = "installed"
        except OSError as error:
            outcome = str(error)
    return outcome, path.read_bytes() == before


def _recovery() -> tuple[dict, bool]:
    measured, update = load("measurements.json"), load("update.json")
    accepted = derive(measured)
    candidate = derive(_updated(measured, update))
    stale = derive({**measured, "generation": measured["generation"] - 1})
    without_browser = [r for r in measured["runs"] if r["class"] != "browser"]
    partial = derive({**measured, "generation": update["generation"] + 1, "runs": without_browser})
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "accepted.json"
        decisions = [profiles.accept(path, accepted, POLICY, "")["decision"]]
        before = path.read_bytes()
        decisions += [profiles.accept(path, c, POLICY, "")["decision"] for c in (candidate, stale, partial)]
        decisions.append(profiles.accept(path, json.loads(json.dumps(accepted)), POLICY, "")["decision"])
        held_unchanged = path.read_bytes() == before
        interruption, survived = _interrupted(path, candidate)
        decisions.append(profiles.accept(path, candidate, POLICY, REVIEWER)["decision"])
        installed_bytes = path.read_bytes()
        installed = json.loads(installed_bytes)["generation"]
        restored = profiles.rollback(path)
        rolled_back = path.read_bytes() == before
        kept = path.with_name(f"{path.name}.previous").read_bytes() == installed_bytes
    changes = profiles.compare(accepted, candidate, POLICY)
    observed = {
        "accepted_generation": accepted["generation"],
        "candidate_generation": candidate["generation"],
        "decisions": dict(
            zip(("first", "workload_update", "stale", "missing_class", "replay", "requalified"), decisions)
        ),
        "changes": changes,
        "missing": profiles.compare(accepted, partial, POLICY)["missing"],
        "held_file_unchanged": held_unchanged,
        "interrupted_install": interruption,
        "accepted_survived_interruption": survived,
        "installed_after_requalification": installed,
        "rollback_restored_generation": restored,
        "rollback_matches_prior_file": rolled_back,
        "rollback_kept_displaced_report": kept,
        "candidate_memory_limit": candidate["profiles"]["memory"]["limits"],
        "accepted_memory_limit": accepted["profiles"]["memory"]["limits"],
    }
    checks = [
        decisions == ["promoted", "requalify", "stale", "requalify", "replayed", "requalified"],
        any(row[0] == "memory" for row in changes["material"]),
        set(changes["tools_changed"]) >= {"workload:memory"},
        observed["missing"] == ["browser"],
        held_unchanged and survived and interruption == "interrupted before install",
        installed == candidate["generation"],
        restored == accepted["generation"] and rolled_back and kept,
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-PRF-01-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
