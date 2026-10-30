"""The feature gate: swarm done on work proved live names the feature-map.json entry it proved, and that entry lists tests.

The map is read from dev and from the merged pull request head, so a feature mapped in the task's own pull request counts.
"""

import json
import subprocess
from collections.abc import Callable

from scripts.gates.enforcement import FEATURE
from scripts.swarm_ledger import ledger_kinds

GATE = FEATURE
REPO = "the-cloud-clockwork/agentihooks"
MAP = "feature-map.json"
BASE = "dev"
LIVE_KINDS = ("troubleshoot", "ops", "tune")
MISSING = f"this task is done only with the feature it proved: give --feature <id in {MAP}>"


def required(task: dict) -> bool:
    """Whether swarm done must name a feature for this task."""
    flagged = (task.get("contract") or {}).get("feature") == "yes"
    return flagged or ledger_kinds.after(task) or ledger_kinds.kind(task) in LIVE_KINDS


def refusal(task: dict, feature: str, maps: Callable[[], list[dict]]) -> str:
    """Why swarm done must refuse this task's feature, or '' when it may close."""
    if not required(task):
        return ""
    wanted = feature.strip()
    if not wanted:
        return MISSING
    entries = [entry for found in maps() for entry in _features(found) if entry.get("id") == wanted]
    if not entries:
        return f"feature {wanted} is not in {MAP} on {BASE} or the merged pull request head; map it there first"
    if not any(entry.get("tests") for entry in entries):
        return f"feature {wanted} lists no test in {MAP}; map its tests first"
    return ""


def refs(head: str) -> list[str]:
    """The revisions whose feature map counts: dev, and the merged pull request head when there is one."""
    return [BASE, *([head] if head else [])]


def read_map(ref: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict:
    """The feature map at a revision on GitHub, or an empty map when it cannot be read."""
    try:
        done = run(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{REPO}/contents/{MAP}?ref={ref}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if done.returncode != 0:
        return {}
    try:
        found = json.loads(done.stdout)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _features(found: dict) -> list[dict]:
    entries = found.get("features")
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
