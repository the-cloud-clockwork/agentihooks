"""Old and new routing give byte-identical results on recorded inputs.

Record the golden file from the base code with
``python -m tests.routing.test_slot_equivalence --record``.
"""

import json
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import scripts.claude_quota_balancer as balancer
import scripts.codex_router as codex_router
import scripts.swarm.capacity as capacity
from scripts.claude_quota_balancer import ProbeResult, QuotaWindow
from scripts.codex_quota import CodexQuota
from scripts.swarm.store import AgentRecord, SwarmConfig

GOLDEN = Path(__file__).with_name("slot_equivalence.json")
NOW = 1_900_000_000


def _claude_results() -> list[ProbeResult]:
    def result(account, five, week, *, status="allowed", week_reset=NOW + 7200, five_reset=NOW + 1800, fable=None):
        return ProbeResult(
            account,
            status,
            "NORMAL",
            None if five is None else 100 - five,
            QuotaWindow(five, five_reset),
            QuotaWindow(week, week_reset),
            QuotaWindow(fable, NOW + 9000) if fable is not None else QuotaWindow(),
        )

    return [
        result("ALPHA", 10, 20, week_reset=NOW + 3600, fable=30),
        result("BRAVO", 50, 10, fable=99),
        result("CHARLIE", 96, 10),
        result("DELTA", 30, 97, week_reset=NOW - 10, fable=5),
        result("ECHO", 10, 10, status="rejected"),
        result("FOXTROT", None, 10),
        result("GOLF", 70, 40, five_reset=NOW - 5, fable=50),
        result("HOTEL", 10, 96, week_reset=None, fable=1),
    ]


def _seed(path: Path, observed_at: float) -> None:
    entries = {
        f"{balancer.TOKEN_PREFIX}{result.account}": {
            "observed_at": observed_at,
            "result": balancer._result_data(result),
        }
        for result in _claude_results()
    }
    balancer._write_cache(
        path, {"version": 1, "modes": {"normal": {"accounts": entries}, "fable": {"accounts": entries}}}
    )


def _claude_env(*names: str, **extra: str) -> dict:
    return {**{f"{balancer.TOKEN_PREFIX}{name}": f"value-{name}" for name in names}, **extra}


ALL = ("ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL")

CLAUDE_CASES = {
    "open": {"env": _claude_env(*ALL)},
    "sessions": {"env": _claude_env(*ALL), "sessions": {"ALPHA": 2, "DELTA": 1, "GOLF": 1, "HOTEL": 3}},
    "tie": {"env": _claude_env(*ALL), "sessions": {"ALPHA": 1, "BRAVO": 1, "DELTA": 1, "GOLF": 1, "HOTEL": 1}},
    "full": {"env": _claude_env(*ALL), "sessions": {"ALPHA": 6, "BRAVO": 4, "DELTA": 6, "GOLF": 6, "HOTEL": 6}},
    "exclude": {"env": _claude_env(*ALL), "exclude": ["DELTA", "GOLF"]},
    "exclude_all": {"env": _claude_env("ALPHA", "ECHO"), "exclude": ["ALPHA"]},
    "reserve": {"env": _claude_env(*ALL, AGENTIHOOKS_RESERVE_ACCOUNTS=" DELTA, GOLF ,,HOTEL")},
    "reserve_fallback": (
        {
            "env": _claude_env("ALPHA", "DELTA", AGENTIHOOKS_RESERVE_ACCOUNTS="DELTA"),
            "sessions": {"ALPHA": 6},
        }
    ),
    "fable": {"env": _claude_env(*ALL), "include_fable": True, "sessions": {"ALPHA": 1}},
    "rejected_only": {"env": _claude_env("ECHO", "CHARLIE")},
    "subset": {"env": _claude_env("BRAVO", "FOXTROT"), "sessions": {"BRAVO": 3}},
    "none": {"env": {"AH_CC_TOKEN_": "x", "AH_CC_TOKEN_EMPTY": ""}},
}


def _probe(credentials, *args, **kwargs):
    return [
        ProbeResult(
            credential.account,
            "allowed",
            "NORMAL",
            75,
            QuotaWindow(25, NOW + 600),
            QuotaWindow(35, NOW + 6000),
        )
        for credential in credentials
    ]


def _claude_outcome(case: dict, root: Path) -> dict:
    cache = root / "claude-cache.json"
    _seed(cache, time.time())
    kwargs = {key: case[key] for key in ("sessions", "exclude", "include_fable") if key in case}
    try:
        decision = balancer.select_credential(case["env"], cache_file=cache, now=NOW, **kwargs)
    except balancer.RoutingError as exc:
        return {"error": str(exc), "results": [result.account for result in exc.results]}
    return {
        "env_name": decision.credential.env_name,
        "account": decision.result.account,
        "source": decision.source,
        "sessions": decision.sessions,
        "max_sessions": decision.max_sessions,
    }


def _codex_pool(signed_in: bool = True) -> list[codex_router.CodexAccount]:
    return [
        codex_router.CodexAccount("default", signed_in=signed_in),
        codex_router.CodexAccount("alpha", "AH_CX_TOKEN_alpha"),
        codex_router.CodexAccount("beta", "AH_CX_TOKEN_beta"),
        codex_router.CodexAccount("gamma", "AH_CX_TOKEN_gamma"),
        codex_router.CodexAccount("delta", "AH_CX_TOKEN_delta"),
    ]


def _codex_quotas() -> dict:
    return {
        "default": CodexQuota(NOW - 60, "team", QuotaWindow(20, NOW + 1000), QuotaWindow(30, NOW + 5000)),
        "alpha": CodexQuota(NOW - 30, "team", QuotaWindow(97, NOW + 1000), QuotaWindow(10, NOW + 4000)),
        "beta": CodexQuota(NOW - 10, "team", QuotaWindow(None), QuotaWindow(96, NOW - 1)),
        "gamma": CodexQuota(NOW - 3600, "team", QuotaWindow(5), QuotaWindow(5)),
        "delta": CodexQuota(NOW, "team", QuotaWindow(40), QuotaWindow(96, NOW + 100)),
    }


CODEX_CASES = {
    "open": {},
    "sessions": {"sessions": {"default": 2, "beta": 1}},
    "tie": {"sessions": {"default": 1, "alpha": 1, "beta": 1}},
    "signed_out": {"signed_in": False, "sessions": {"alpha": 3}},
    "full": {"sessions": {"default": 6, "alpha": 6, "beta": 6}},
    "forced": {"route": "gamma"},
    "forced_unknown": {"route": "zulu"},
    "sparse": {"quotas": ["alpha", "gamma"]},
}


def _seat(seat) -> list | None:
    return None if seat is None else [seat.harness, seat.account, seat.cap, seat.sessions, seat.spend_before]


def _codex_outcome(case: dict) -> dict:
    quotas = _codex_quotas()
    if "quotas" in case:
        quotas = {name: quotas[name] for name in case["quotas"]}
    pool = _codex_pool(case.get("signed_in", True))
    try:
        account, placement, seat = codex_router.select(
            pool, quotas, case.get("sessions", {}), NOW, case.get("route", "")
        )
    except codex_router.RoutingError as exc:
        return {"error": str(exc)}
    return {"account": account.name, "env_name": account.env_name, "placement": placement, "seat": _seat(seat)}


CAPACITY_CASES = {
    "open": {"config": (3, 1, 1)},
    "busy": {
        "config": (4, 2, 1),
        "agents": [("eng", "working", "claude"), ("eng", "finished", "codex"), ("ci", "working", "codex")],
        "claude_sessions": {"ALPHA": 3, "BRAVO": 1, "ZULU": 2},
        "codex_sessions": {"default": 1, "beta": 2, "omega": 1},
    },
    "demand": {"config": (6, 2, 1), "demand": {"eng": 4, "ci": 1, "plan": 0}},
    "requirements": {
        "config": (5, 2, 1),
        "requirements": {
            "eng": [("claude",), ("codex",), ("claude", "codex"), ("codex",), ("claude",)],
            "ci": [("codex",), ("claude",)],
            "plan": [("claude",)],
        },
    },
    "lane_agent": {"config": (6, 2, 1), "lanes": {"eng": {"agent": "codex"}, "ci": {"agent": "claude"}}},
    "no_refresh": {"config": (3, 1, 1), "refresh": False},
    "claude_only": {"config": (8, 3, 1), "codex_tokens": False},
}


def _capacity_outcome(case: dict, root: Path) -> dict:
    home = root / "home"
    home.mkdir(exist_ok=True)
    _seed(home / "claude-router-cache.json", NOW - 5)
    env = {"AGENTIHOOKS_HOME": str(home), **_claude_env("ALPHA", "BRAVO", "CHARLIE", "DELTA", "GOLF", "HOTEL")}
    if case.get("codex_tokens", True):
        env.update({f"AH_CX_TOKEN_{name}": f"cx-{name}" for name in ("alpha", "beta", "gamma", "delta")})
    quotas = _codex_quotas()
    with ExitStack() as stack:
        patch = stack.enter_context
        patch(
            mock.patch.object(capacity.account_sessions, "sessions_by_account", lambda: case.get("claude_sessions", {}))
        )
        patch(
            mock.patch.object(
                capacity.account_sessions, "codex_sessions_by_account", lambda: case.get("codex_sessions", {})
            )
        )
        patch(mock.patch.object(codex_router, "default_signed_in", lambda environ, run=None: True))
        patch(
            mock.patch.object(codex_router, "quotas", lambda pool, environ: {a.name: quotas.get(a.name) for a in pool})
        )
        patch(mock.patch.object(codex_router, "probe", lambda account, environ, run=None: None))
        patch(mock.patch.object(codex_router, "_attempts_path", lambda: root / "attempts.json"))
        rows = capacity.accounts(env, NOW, case.get("refresh", True))
    eng, ci, plan = case["config"]
    config = SwarmConfig("sw", "/repo", max_eng=eng, max_ci=ci, max_plan=plan, lanes=case.get("lanes", {}))
    agents = [
        AgentRecord(f"a{i}", lane, f"t{i}", state=state, harness=h)
        for i, (lane, state, h) in enumerate(case.get("agents", []))
    ]
    requirements = case.get("requirements")
    if requirements:
        requirements = {lane: [tuple(choice) for choice in choices] for lane, choices in requirements.items()}
    return capacity.calculate(config, rows, agents, case.get("demand"), requirements)


def outcomes() -> dict:
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(balancer, "probe_credentials", _probe):
        root = Path(tmp)
        claude = {}
        for name, case in CLAUDE_CASES.items():
            folder = root / f"claude-{name}"
            folder.mkdir()
            claude[name] = _claude_outcome(case, folder)
        codex = {name: _codex_outcome(case) for name, case in CODEX_CASES.items()}
        decisions = {}
        for name, case in CAPACITY_CASES.items():
            folder = root / f"capacity-{name}"
            folder.mkdir()
            decisions[name] = _capacity_outcome(case, folder)
    return {"select_credential": claude, "codex_select": codex, "capacity_calculate": decisions}


def render() -> str:
    return json.dumps(outcomes(), indent=2, sort_keys=True) + "\n"


def test_routing_matches_the_recorded_base_byte_for_byte():
    assert render() == GOLDEN.read_text()


if __name__ == "__main__":
    if sys.argv[1:] != ["--record"]:
        sys.exit("usage: python -m tests.routing.test_slot_equivalence --record")
    GOLDEN.write_text(render())
