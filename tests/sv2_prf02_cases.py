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
KIND = ("kind-run-1.json", "kind-run-2.json")
SOURCES = {
    "anton-read-1.json": "read only kubectl and aws read of anton-prod at 2026-10-11T14:57:15Z",
    "anton-read-2.json": "read only kubectl and aws read of anton-prod, the next hour of events",
    "kind-run-1.json": "Startup stages run 38150380291 at ec5276308ced2ea1c182dbce76c6e81a239a9d48, both nodes behind a"
    " 90 second registry outage",
    "kind-run-2.json": "Startup stages run 38153743721 at f49241dc520bf8eeb84cb0816a2880e43a85e787, both nodes behind a"
    " 90 second registry outage",
}
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
        "source": SOURCES[name],
        "read_at": load(name)["read_at"],
        "startup_stage_p50_p95": {scenario: _row(row) for scenario, row in result["scenarios"].items()},
    }


NOTES = (
    "Node provision runs from the Pod waiting to the new node accepting it: trigger, instance, join and schedule. Image"
    " pull with retries is the successful pull plus the time lost to failed pulls before it. Pod start (sandbox,"
    " containers, init containers) and bootstrap are separate columns. No column sums them into one launch figure.",
    "Only samples from a scale up of an empty pool onto a new instance that pulled uncached may be labelled cold"
    " scale from zero; the ondemand scale ups in these reads started from a non empty pool and are refused that label.",
    "Kubernetes keeps events for about an hour, so each Anton read covers the hour before it; Pods whose events"
    " expired are absent rather than counted as fast.",
    "EC2 launch times have one second precision and the join stage ends at the node's Ready transition.",
    "Bootstrap is the worker image's own report followed by a readiness file in the kind fixture; the native harness"
    " handshake needs a controller grant and provider credentials and is not measured here.",
    "The kind fixture pulls from a registry on the same runner, so its pull times show image size and unpack cost,"
    " not registry distance; the Anton reads show the production registry path.",
    "With few cold samples the p95 equals the maximum.",
)


def results_page() -> str:
    sections = [
        bench.markdown(stages.report(samples(name)), SOURCES[name]).replace("# Swarm v2 startup stages", f"## {name}")
        for name in INPUTS
    ]
    notes = "\n".join(f"- {note}" for note in NOTES)
    return "# Swarm v2 startup stages\n\n" + "\n".join(sections) + "\n## Limits of this measurement\n\n" + notes + "\n"


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


def _anton_holds(name: str) -> dict:
    rows = summary(name)["startup_stage_p50_p95"]
    cold = [row for scenario, row in rows.items() if scenario.startswith("cold-node") and row["started"]]
    zero = _from_zero(samples(name))
    return {
        "cold_rows_split": bool(cold) and all(row["node_provision_ms"] and row["image_init_ms"] for row in cold),
        "cold_claimed": len(stages.claim(zero, stages.COLD_LABEL)) if zero else 0,
    }


def _kind_holds(name: str) -> dict:
    rows = summary(name)["startup_stage_p50_p95"]
    wanted = ("warm-node/cached-image", "warm-node/uncached-image")
    return {
        "scenarios": sorted(rows),
        "all_started": all(rows[s]["samples"] == rows[s]["started"] for s in wanted if s in rows),
        "uncached_pull_measured": "image" in rows.get(wanted[1], {}).get("stages", {}),
    }


def _positive() -> tuple[dict, bool]:
    observed = {name: summary(name) for name in INPUTS}
    held = {name: _anton_holds(name) for name in ANTON} | {name: _kind_holds(name) for name in KIND}
    starts = [{(e["pod"], e["at"]) for e in samples(name) if e["outcome"] == stages.STARTED} for name in ANTON]
    shared = sorted(
        e["pod"] for e in samples(ANTON[1]) if (e["pod"], e["at"]) in {(s["pod"], s["at"]) for s in samples(ANTON[0])}
    )
    independent = not starts[0] & starts[1]
    observed.update({"each_fixture": held, "independent_reads": independent, "continuing_in_both_reads": shared})
    anton = all(held[name]["cold_rows_split"] and held[name]["cold_claimed"] for name in ANTON)
    kind = all(
        held[name]["all_started"]
        and held[name]["uncached_pull_measured"]
        and {"warm-node/cached-image", "warm-node/uncached-image"} <= set(held[name]["scenarios"])
        for name in KIND
    )
    return observed, anton and kind and independent


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
    """An unavailable registry holds the account slot only through the pull grace; past it the attempt is fenced, the
    slot released once, and a replayed pass changes nothing."""
    world = kub05.World()
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.live = controller
        world.old = world.launch(controller, world.fx["first"])
        recovery = world.recovery(controller)
        held = world.slots()
        world.show(world.old, "image_pull")
        within = world.reconcile(recovery)
        during = world.slots()
        world.clock[0] += failures.PULL_GRACE_MS
        assert controller.renew()
        past = world.reconcile(recovery)
        fence = recovery.fence(world.old.execution_id)
        decision = recovery.decision(world.old.execution_id)
        after = world.slots()
        replay = world.reconcile(recovery)
        return {
            "slots_before": len(held),
            "within_grace": within[world.old.execution_id],
            "slots_within_grace": len(during),
            "past_grace": past[world.old.execution_id],
            "slots_after": after,
            "fence_reason": fence["reason"],
            "released": fence["released"],
            "decision": decision.mode,
            "replay_status": replay[world.old.execution_id],
            "replay_equal": recovery.decision(world.old.execution_id) == decision
            and recovery.fence(world.old.execution_id) == fence,
            "slots_after_replay": world.slots(),
            "attempts": len(world.attempts()),
        }


def _recovery() -> tuple[dict, bool]:
    interrupted = [e for e in samples(KIND[1]) if e["pod"].split("/")[1].startswith("interrupted-")]
    classified = [e["transient"] or e["outcome"] for e in interrupted]
    observed = {
        "interrupted": [
            {key: e[key] for key in ("pod", "outcome", "transient", "failure_ms", "image_cache")}
            | {"pod_start_ms": e["stages"]["pod_start"]}
            for e in interrupted
        ],
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
        and account["slots_before"] == account["slots_within_grace"] == 1
        and account["slots_after"] == account["slots_after_replay"] == []
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
