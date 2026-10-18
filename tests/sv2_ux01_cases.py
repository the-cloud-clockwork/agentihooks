import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.swarm import commands
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_v2.herdr import visibility
from scripts.swarm_v2.herdr.visibility import View

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUT = "ledger-execution-cards.json"
EVIDENCE_CLASS = (
    "mocked: a fake Redis swarm store holds one published status for a mixed local and Kubernetes swarm and terminal "
    "views published after the heartbeat but checked before it; the swarm view the ledger server returns is read at a "
    "fixed clock; no ledger server, browser, worker Pod or cluster runs in these cases"
)
CASES = {"a": "T-SV2-UX-01-A", "b": "T-SV2-UX-01-B", "c": "T-SV2-UX-01-C"}


def fixture() -> dict:
    return json.loads((FIXTURES / INPUT).read_text())


def seeded(slug: str, data: dict) -> RedisStore:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(slug, ".", 0, 0, state="running"))
    commands.publish(store, slug, data["status"], {})
    visibility.publish(store, slug, [View(**row) for row in data["terminal_views_published_after_the_heartbeat"]])
    return store


def read(store: RedisStore, slug: str, now_ms: int) -> dict:
    with patch.object(commands, "time", SimpleNamespace(time_ns=lambda: now_ms * 1_000_000)):
        return commands.view(store, slug)


def snapshot(store: RedisStore) -> dict:
    return {key: store.redis.dump(key) for key in sorted(store.redis.keys("*"))}


def shown(cards: dict) -> dict:
    return {name: {k: v for k, v in card.items() if k != "agent"} for name, card in cards.items()}


def _case_a(data: dict) -> tuple[dict, dict, int]:
    views = [read(seeded(slug, data), slug, data["now_ms"]) for slug in ("sv2-ux-01-a", "sv2-ux-01-a2")]
    first, second = (shown(view["executions"]) for view in views)
    backends = sorted({card["backend"] for card in first.values()})
    checks = {
        "every_card_matches_the_fixture": first == data["expected"],
        "second_fixture_matches_the_first": second == first,
        "local_and_remote_in_one_view": {"local", "kubernetes"} <= set(backends),
        "agent_rows_unchanged": [a["name"] for a in views[0]["agents"]]
        == [a["name"] for a in data["status"]["agents"]],
    }
    observed = {"backends": backends, "waits": {name: card["wait"] for name, card in first.items() if card["wait"]}}
    return checks, observed, views[0]["ledger_state_projection_errors"]


def _case_b(data: dict) -> tuple[dict, dict, int]:
    slug = "sv2-ux-01-b"
    store = seeded(slug, data)
    before = snapshot(store)
    view = read(store, slug, data["now_ms"])
    card = view["executions"]["eng-2"]
    terminal = view["terminals"]["eng-2"]
    checks = {
        "heartbeat_state_kept": card["observed"] == "working",
        "terminal_marked_behind": card["terminal_behind"] is True,
        "seen_from_the_heartbeat": card["seen_at"] == 1791299880000,
        "store_unchanged": snapshot(store) == before,
        "unreadable_row_contained": view["executions"]["eng-4"]["observed"] == "unreadable",
    }
    observed = {"terminal_state": terminal["state"], "terminal_checked_at": terminal["checked_at"], "card": card}
    return checks, observed, view["ledger_state_projection_errors"]


def _case_c(data: dict) -> tuple[dict, dict, int]:
    slug = "sv2-ux-01-c"
    store = seeded(slug, data)
    first = read(store, slug, data["now_ms"])
    before = snapshot(store)
    again = read(RedisStore(store.redis), slug, data["now_ms"])
    checks = {
        "reload_restores_the_same_cards": again["executions"] == first["executions"],
        "no_command_replayed": again["commands"] == first["commands"] == [],
        "store_unchanged_by_reload": snapshot(store) == before,
    }
    observed = {"cards": len(again["executions"]), "commands": len(again["commands"])}
    return checks, observed, again["ledger_state_projection_errors"]


RUNNERS = {"a": _case_a, "b": _case_b, "c": _case_c}


def run_case(case: str) -> dict:
    checks, observed, errors = RUNNERS[case](fixture())
    return {
        "case": CASES[case],
        "package": "SV2-UX-01",
        "state": "passed" if all(checks.values()) else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "checks": checks,
        "observed": observed,
        "ledger_state_projection_errors": errors,
        "input_sha256": {INPUT: hashlib.sha256((FIXTURES / INPUT).read_bytes()).hexdigest()},
    }
