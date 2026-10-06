"""Correction quarantine: an open correction the operator confirmed withholds its directive wherever rules are injected.

A correction an agent records inside a swarm is proposed until the operator confirms it; one recorded outside a
swarm, or before proposals existed, counts as confirmed. A source confirmed wrong twice is held whole, open or closed,
until the operator releases it. AGENTIHOOKS_GATE_QUARANTINE: enforce (default), observe (logged, still injected), off.
"""

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from hooks.context import injection_trace

MODES = ("enforce", "observe", "off")
PROPOSED = "proposed"
WITHHELD = "withheld"
QUOTE_WORDS = 3
PASSAGE_NOTICE = "> CORRECTION: the passage above is marked wrong for the {repo} repo: {reason}. Do not follow it."
FILE_NOTICE = "> CORRECTION: this file is marked wrong for the {repo} repo: {reason}. Do not follow it."
HELD_NOTICE = "> CORRECTION: {source} was marked wrong more than once and is held whole until the operator releases it."
OPERATOR_ONLY = (
    "only the operator {verb}s a correction: run it outside a swarm, or pass --quote with at least three of his own "
    "words from this session that say {verb} and correction, or have him comment that on your task"
)
REFUSED_PATCH = (
    "this text carries a directive under correction ({source}: {reason}); fix it at its source instead of "
    "restating it here"
)


@dataclass(frozen=True)
class Held:
    correction: dict
    keys: frozenset
    needle: str
    whole: bool = False


def mode(environ=None) -> str:
    env = os.environ if environ is None else environ
    value = str(env.get("AGENTIHOOKS_GATE_QUARANTINE")).strip().lower()
    return value if value in MODES else "enforce"


def _confirmations_path():
    return injection_trace._home() / "injection_corrections_confirmed.jsonl"


def _releases_path():
    return injection_trace._home() / "injection_corrections_released.jsonl"


def _key(row: dict) -> str:
    from hooks.context import trace_sweep

    return trace_sweep._correction_key(row)


def _norm(text) -> str:
    return " ".join(str(text).split())


def is_proposed(row: dict, confirmed_keys: set) -> bool:
    return row.get("status") == PROPOSED and _key(row) not in confirmed_keys


def confirmed_keys() -> set:
    return {row["correction"] for row in injection_trace._read(_confirmations_path())}


def confirmed_rows() -> list[dict]:
    keys = confirmed_keys()
    return [row for row in injection_trace.corrections() if not is_proposed(row, keys)]


def confirm(rows: list[dict], by: str, words: str) -> None:
    for row in rows:
        entry = {"at": injection_trace._now(), "correction": _key(row), "by": by, "words": words}
        injection_trace._append(_confirmations_path(), entry)


def release(source: str, by: str, words: str) -> None:
    entry = {"at": injection_trace._now(), "source": source, "by": by, "words": words}
    injection_trace._append(_releases_path(), entry)


def needle(row: dict) -> str:
    from hooks.context import trace_sweep

    return _norm(row["quote"]) if row.get("quote") else trace_sweep._needle(row)


def _keys(row: dict) -> frozenset:
    return frozenset(k for k in (row.get("source"), (row.get("locator") or {}).get("id")) if k)


def _since_release(rows: list[dict]) -> Counter:
    released = {entry["source"]: entry["at"] for entry in injection_trace._read(_releases_path())}
    return Counter(row["source"] for row in rows if row["at"] > released.get(row["source"], ""))


def index() -> list[Held]:
    if not injection_trace._corrections_path().is_file():
        return []
    from hooks.context import trace_sweep

    rows = confirmed_rows()
    recent = _since_release(rows)
    open_keys = {_key(row) for row in trace_sweep.open_corrections()}
    held = []
    for row in rows:
        whole = recent[row["source"]] > 1
        if whole or _key(row) in open_keys:
            held.append(Held(row, _keys(row), needle(row), whole))
    return held


def match(held: list[Held], keys, text) -> Held | None:
    wanted, norm = {k for k in keys if k}, _norm(text)
    return next((h for h in held if h.keys & wanted or (h.needle and h.needle in norm)), None)


def withheld_row(layer: str, source: str, hit: Held, current: str) -> dict:
    correction = hit.correction
    return {
        "layer": WITHHELD,
        "source": source,
        "locator": {"layer": layer, "correction": _key(correction), "mode": current},
        "text": f"withheld under the correction of {correction['source']}: {correction['reason']}",
    }


def keep(session_id: str, layer: str, items, keys, text) -> list:
    """The items to inject: those under a confirmed correction drop out in enforce mode; each is logged once per session."""
    items, current = list(items), mode()
    held = index() if items and current != "off" else []
    if not held:
        return items
    kept, rows = [], []
    for item in items:
        hit = match(held, keys(item), text(item))
        if hit is not None:
            rows.append(withheld_row(layer, keys(item)[0], hit, current))
        if hit is None or current == "observe":
            kept.append(item)
    if rows:
        injection_trace.record_rows(session_id, rows)
    return kept


def _after(text: str, quote: str, notice: str) -> str:
    found = re.search(r"\s+".join(map(re.escape, quote.split())) + r"[^\n]*", text)
    if found is None:
        return text
    return f"{text[: found.end()]}\n\n{notice}\n{text[found.end() :]}"


def _notice(template: str, correction: dict) -> str:
    return template.format(repo=Path(correction["repo"]).name, reason=correction["reason"])


def _enforced() -> list[Held]:
    return index() if mode() == "enforce" else []


def _passages(text: str, held: list[Held]) -> str:
    for item in held:
        correction = item.correction
        if correction.get("layer") in injection_trace.FILE_LAYERS and correction.get("quote"):
            text = _after(text, correction["quote"], _notice(PASSAGE_NOTICE, correction))
    return text


def passages(text: str) -> str:
    """The rendered persona: a notice after each corrected passage it carries."""
    return _passages(text, _enforced())


def annotate(text: str, source: str) -> str:
    """The rendered copy of a rule or doctrine file: a notice after each corrected passage, or only a notice when held."""
    held = _enforced()
    if any(h.whole and source in h.keys for h in held):
        return HELD_NOTICE.format(source=source) + "\n"
    for item in held:
        correction = item.correction
        if (
            correction.get("layer") in injection_trace.FILE_LAYERS
            and not correction.get("quote")
            and source in item.keys
        ):
            text = f"{_notice(FILE_NOTICE, correction)}\n\n{text}"
    return _passages(text, held)


def digest() -> str:
    held = sorted((_key(h.correction), h.whole) for h in index())
    return hashlib.sha1(json.dumps(held).encode()).hexdigest()[:12] if held else ""


def patch_refusal(text: str) -> str:
    """Why `text` may not be written to a learned note or the culture: it restates a directive under correction."""
    from hooks.context import trace_sweep

    norm = _norm(text)
    for row in trace_sweep.open_corrections():
        found, whole = needle(row), _norm(row.get("text") or "")
        if (found and found in norm) or (whole and whole == norm):
            return REFUSED_PATCH.format(source=row["source"], reason=row["reason"])
    return ""


def _asks(verb: str):
    return lambda words: bool(re.search(rf"\b{verb}", words.lower()) and re.search(r"\bcorrection", words.lower()))


def operator_said(verb: str, quote: str, environ=None) -> str:
    """Who stands behind a confirm or release: 'operator' outside a swarm, else his quoted words or his task comment."""
    env = os.environ if environ is None else environ
    if not env.get("AGENTIHOOKS_SWARM"):
        return "operator"
    asks, name = _asks(verb), env.get("AGENTIHOOKS_AGENT_NAME")
    if name and len(str(quote).split()) >= QUOTE_WORDS:
        from hooks.context import operator_words

        if asks(operator_words.matching(name, quote)):
            return "words"
    from hooks.context import ledger_request

    found = ledger_request.find(asks, env)
    return found[0] if found else ""
