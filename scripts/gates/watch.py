"""The watch budget: a swarm agent that has only watched since its last action is refused another watch call."""

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from scripts.gates.base import Call, Decision, Who
from scripts.swarm import naming
from scripts.swarm.health import activity, findings
from scripts.swarm.health.findings import Limits
from scripts.swarm_ledger.ledger_gate import WATCH_STALE_SECONDS

LANES = frozenset({"eng", "ci", "master"})


def watcher_alive(who: Who, ledger_dir: Path, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    for candidate in cast(Callable[[str], list[str]], naming.addresses)(who.name):
        try:
            age = now - (ledger_dir / ".sessions" / f"{who.swarm}.{candidate}.watch").stat().st_mtime
        except OSError:
            continue
        if age <= WATCH_STALE_SECONDS:
            return True
    return False


def refusal(who: Who, after: dict[str, int], least: int, ratio: int) -> str:
    return (
        f"watch budget: {after['since'] - 1} watch calls since your last action (limit {least}) and "
        f"{after['watch'] - 1} watch calls for {after['act']} actions (limit {ratio} per action). Hand the waiting "
        f'to the tick: agentihooks swarm {who.swarm} wait <minutes> --reason "<what you wait on>", or act first: '
        "commit, push or record progress on the ledger."
    )


class WatchBudget:
    name = "watch"
    default_mode = "enforce"

    def __init__(self, root: str | Path | None = None, environ: dict[str, str] | None = None) -> None:
        self.environ = os.environ if environ is None else environ
        self.root = Path(root or cast(Callable[[], Path], activity.default_root)())
        self.ledger_dir = Path(self.environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()

    def matches(self, call: Call) -> bool:
        return cast(Callable[[str, dict[str, Any]], str], activity.classify)(call.tool, call.tool_input) == "watch"

    def decide(self, call: Call, who: Who, state: object) -> Decision:
        named = activity.NAME_RE.match(who.swarm) and activity.NAME_RE.match(who.name)
        if not named or naming.lane_of(who.name) not in LANES or who.lane == naming.OPERATOR:
            return Decision()
        if activity.REARM_RE.search(call.command) and not watcher_alive(who, self.ledger_dir):
            cast(Callable[[str, str, Path | None], None], activity.mark_revived)(who.swarm, who.name, self.root)
            return Decision()
        rows = cast(Callable[[str, str, Path | None], list[dict[str, Any]]], activity.rows_of)(
            who.swarm, who.name, self.root
        )
        totals = cast(Callable[[list[dict[str, Any]]], dict[str, int]], activity.tally)(rows)
        since = cast(Callable[[list[dict[str, Any]]], int], activity.since_action)(rows)
        after = {"watch": totals["watch"] + 1, "act": totals["act"], "since": since + 1}
        least, ratio = cast(Callable[[str, Limits], tuple[int, int]], findings.watch_limits)(
            who.name, cast(Callable[[Mapping[str, str]], Limits], findings.limits)(self.environ)
        )
        if cast(Callable[[dict[str, int], int, int], bool], findings.over_watched)(after, least, ratio):
            return Decision.deny(refusal(who, after, least, ratio))
        return Decision()
