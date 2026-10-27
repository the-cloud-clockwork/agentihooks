"""Every hook one harness wires runs on Claude, Codex and Copilot alike."""

import json
import re
from pathlib import Path

import install
import pytest

from hooks.targets.normalizer import _COPILOT_EVENTS
from scripts.targets.codex_target import CodexAdapter
from scripts.targets.copilot_target import CopilotAdapter

pytestmark = pytest.mark.unit

HARNESSES = ("claude", "codex", "copilot")

UNSUPPORTED = {
    ("codex", "Notification"): "Codex hooks.json has no Notification event",
    ("codex", "PostToolUseFailure"): "Codex hooks.json has no failed tool call event",
    ("copilot", "PostToolUseFailure"): "Copilot folds postToolUseFailure into PostToolUse",
}

UNHELD = {
    (
        "copilot",
        "Stop",
    ): "Copilot agentStop has no block channel: exit 2 is advisory there and stdout blocks only prompts",
}

_SHARED_ENTRY = re.compile(r"(?:^|\s)-m hooks(?:\s|$)")
_SCRIPT = re.compile(r"(?:/app|\S*agentihooks\S*)/(scripts/\S+\.py)")


def _entry_name(command: str) -> str:
    path = Path(command.split()[0])
    if path.name == "agentihooks-hook.sh":
        command = path.read_text()
    if _SHARED_ENTRY.search(command):
        return "hooks"
    script = _SCRIPT.search(command)
    return script.group(1) if script else command


def _wiring(groups_by_event: dict, event_name) -> dict[str, set[str]]:
    wiring: dict[str, set[str]] = {}
    for event, groups in groups_by_event.items():
        hooks = [h for g in groups for h in (g.get("hooks", [g]))]
        names = wiring.setdefault(event_name(event), set())
        names.update(_entry_name(h["command"]) for h in hooks)
    return wiring


def _claude() -> dict[str, set[str]]:
    base = json.loads((install.PROFILES_DIR / "_base" / "settings.base.json").read_text())
    return _wiring(base["hooks"], str)


def _codex(home: Path) -> dict[str, set[str]]:
    CodexAdapter()._write_hooks_json(home)
    return _wiring(json.loads((home / "hooks.json").read_text())["hooks"], str)


def _copilot(home: Path) -> dict[str, set[str]]:
    CopilotAdapter()._write_hooks_json(home)
    doc = json.loads((home / "hooks" / "agentihooks.json").read_text())
    return _wiring(doc["hooks"], _COPILOT_EVENTS.__getitem__)


@pytest.fixture
def wiring(tmp_path) -> dict[str, dict[str, set[str]]]:
    (tmp_path / "codex").mkdir()
    (tmp_path / "copilot").mkdir()
    return {"claude": _claude(), "codex": _codex(tmp_path / "codex"), "copilot": _copilot(tmp_path / "copilot")}


def test_every_harness_wires_the_same_hooks_for_each_event(wiring):
    events = set().union(*(w.keys() for w in wiring.values()))
    differences = {}
    for event in sorted(events):
        wired = {h: frozenset(wiring[h].get(event, ())) for h in HARNESSES if (h, event) not in UNSUPPORTED}
        if len(set(wired.values())) > 1:
            differences[event] = {h: sorted(names) for h, names in wired.items()}
    assert differences == {}


def test_every_named_difference_is_a_real_gap(wiring):
    still_missing = {key for key in UNSUPPORTED if key[1] not in wiring[key[0]]}
    assert still_missing == set(UNSUPPORTED)


def test_every_harness_reaches_swarm_hooks_only_through_the_shared_entry(wiring):
    entries = {name for w in wiring.values() for names in w.values() for name in names}
    assert entries == {"hooks"}


def test_every_unheld_event_is_one_its_harness_cannot_block():
    from hooks.targets.capabilities import _COPILOT_BLOCKABLE_EVENTS

    blockable = {"copilot": _COPILOT_BLOCKABLE_EVENTS}
    assert {key for key in UNHELD if key[1] not in blockable[key[0]]} == set(UNHELD)
