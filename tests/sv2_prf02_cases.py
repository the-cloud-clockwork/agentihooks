import contextlib
import hashlib
import io
import json
import tempfile
from pathlib import Path

from scripts.swarm_v2 import startup_bench as bench
from scripts.swarm_v2 import startup_stages as stages
from scripts.swarm_v2.kubernetes import failures
from scripts.swarm_v2.resource_profiles import ClaimRefused
from tests import sv2_kub05_cases as kub05

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2" / "startup"
ANTON = ("anton-read-1.json", "anton-read-2.json")
KIND = ("kind-run-1.json",)
INPUTS = (*ANTON, *KIND)
FROM_ZERO_GROUP = "anton-k3s-swarm"
EVIDENCE_CLASS = (
    "measured: Anton reads are read only kubectl and aws listings of nodes, events, Pods, autoscaling activities and "
    "EC2 instances reduced by startup_bench reduce; the kind reads come from the Startup stages workflow named in "
    "their source, where a GitHub hosted runner pulled the published worker image from a local registry that the "
    "fixture stopped and restarted; attribution, report and claim are functions over those committed reads; the "
    "account release in case C runs the KUB-05 recovery over fakeredis and is mocked"
)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def digests() -> dict[str, str]:
    return {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS}


def samples(name: str) -> list[dict]:
    return stages.attribute(load(name))


def _row(row: dict) -> dict:
    keep = ("samples", "started", "pending", "failures", "transient", "from_zero", "node_sources")
    spreads = {"node_provision_ms": row["node_provision_ms"], "image_init_ms": row["image_init_ms"]}
    return {
        **{key: row[key] for key in keep},
        **{key: {k: v[k] for k in ("n", "p50", "p95")} if v else None for key, v in spreads.items()},
        "stages": {stage: {k: v[k] for k in ("n", "p50", "p95")} for stage, v in row["stages"].items()},
    }


def summary(name: str) -> dict:
    result = stages.report(samples(name))
    return {
        "read_at": load(name)["read_at"],
        "startup_stage_p50_p95": {scenario: _row(row) for scenario, row in result["scenarios"].items()},
    }


def _refusal(chosen: list[dict]) -> dict:
    try:
        stages.claim(chosen, stages.COLD_LABEL)
    except ClaimRefused as refused:
        return {
            "refused": str(refused),
            "reasons": sorted(set(refused.refused.values())),
            "count": len(refused.refused),
        }
    return {"refused": "", "reasons": [], "count": 0}


def _from_zero(entries: list[dict]) -> list[dict]:
    return [entry for entry in entries if entry["from_zero"]]


def _positive() -> tuple[dict, bool]:
    observed = {name: summary(name) for name in INPUTS}
    cold = {name: _from_zero(samples(name)) for name in ANTON}
    claimed = {name: len(stages.claim(entries, stages.COLD_LABEL)) for name, entries in cold.items() if entries}
    split = all(
        row["node_provision_ms"] and row["image_init_ms"]
        for name in ANTON
        for scenario, row in observed[name]["startup_stage_p50_p95"].items()
        if scenario.startswith("cold-node") and row["started"]
    )
    independent = not {e["pod"] for e in samples(ANTON[0])} & {e["pod"] for e in samples(ANTON[1])}
    repeat = summary(ANTON[0]) == observed[ANTON[0]]
    observed.update({"cold_claims": claimed, "independent_reads": independent, "repeat_equal": repeat})
    return observed, split and bool(claimed) and independent and repeat


def _rejection() -> tuple[dict, bool]:
    anton = samples(ANTON[0])
    ondemand = [e for e in anton if e["node_kind"] == "cold" and not e["from_zero"]]
    warm = [e for e in anton if e["node_kind"] == "warm" and e["outcome"] == stages.STARTED][:50]
    cached = [e for e in samples(KIND[0]) if e["image_cache"] == stages.CACHED]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "read.json"
        path.write_bytes((FIXTURES / ANTON[0]).read_bytes())
        before = sorted(p.name for p in Path(folder).iterdir()), path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code = bench.main(["claim", "--read", str(path), "--scenario", "cold-node/uncached-image"])
        after = sorted(p.name for p in Path(folder).iterdir()), path.read_bytes()
    observed = {
        "cold_not_from_zero": _refusal(ondemand),
        "warm_node": _refusal(warm),
        "cached_image": _refusal(cached),
        "cli_exit_code": code,
        "cli_refusal": json.loads(printed.getvalue())["refused"],
        "read_unchanged": before == after,
    }
    passed = (
        all(observed[key]["refused"] for key in ("cold_not_from_zero", "warm_node", "cached_image"))
        and code == 2
        and observed["read_unchanged"]
    )
    return observed, passed


def _account_release() -> dict:
    world = kub05.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.live = controller
        world.old = world.launch(controller, world.fx["first"])
        held = world.slots()
        world.show(world.old, "image_pull")
        recovery = world.recovery(controller)
        first = world.reconcile(recovery)
        decision = recovery.decision(world.old.execution_id)
        replay = world.reconcile(recovery)
        return {
            "slots_before": len(held),
            "slots_after": world.slots(),
            "fence_reason": recovery.fence(world.old.execution_id)["reason"],
            "released": recovery.fence(world.old.execution_id)["released"],
            "decision": decision.mode,
            "replay_equal": first == replay and recovery.decision(world.old.execution_id) == decision,
            "attempts": len(world.attempts()),
        }


def _recovery() -> tuple[dict, bool]:
    interrupted = [e for e in samples(KIND[0]) if e["pod"].split("/")[1].startswith("interrupted-")]
    classified = [e["transient"] or e["outcome"] for e in interrupted]
    observed = {
        "interrupted": [{key: e[key] for key in ("pod", "outcome", "transient", "image_cache")} for e in interrupted],
        "classified": classified,
        "unbounded": stages.unbounded(interrupted),
        "grace_ms": failures.PULL_GRACE_MS,
        "account": _account_release(),
    }
    account = observed["account"]
    passed = (
        bool(interrupted)
        and set(classified) == {stages.REGISTRY_UNAVAILABLE}
        and not observed["unbounded"]
        and all(e["outcome"] == stages.STARTED for e in interrupted)
        and account["slots_before"] == 1
        and account["slots_after"] == []
        and account["released"] == ["grant", "account"]
        and account["replay_equal"]
    )
    return observed, passed


CASES = {"a": ("T-SV2-PRF-02-A", _positive), "b": ("T-SV2-PRF-02-B", _rejection), "c": ("T-SV2-PRF-02-C", _recovery)}


def run_case(case: str) -> dict:
    name, run = CASES[case]
    observed, passed = run()
    return {
        "case": name,
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": digests(),
        "observed": observed,
    }
