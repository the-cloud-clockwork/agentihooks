"""Hook-runtime target detection.

Which agent CLI invoked this hook process. Claude Code is the default; the
Codex and Copilot hook writers set ``AGENTIHOOKS_TARGET`` in every hook
command, so detection is a deterministic env read — no payload sniffing.
Read per-call (not bound at import) so tests and long-lived processes see
the live value.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_TARGET = "claude"


def current_target() -> str:
    return os.environ.get("AGENTIHOOKS_TARGET", "").strip().lower() or DEFAULT_TARGET


def is_codex() -> bool:
    return current_target() == "codex"


def is_copilot() -> bool:
    return current_target() == "copilot"


def codex_home() -> Path:
    raw = (os.environ.get("CODEX_HOME") or "").split(",")[0].strip()
    return Path(raw).expanduser() if raw else Path.home() / ".codex"


def is_codex_memory_thread(cwd: str) -> bool:
    # Codex runs its memory consolidation thread in CODEX_HOME/memories under the parent's environment.
    return Path(cwd).resolve().is_relative_to((codex_home() / "memories").resolve())


def copilot_home() -> Path:
    raw = (os.environ.get("COPILOT_HOME") or "").split(",")[0].strip()
    return Path(raw).expanduser() if raw else Path.home() / ".copilot"


# Targets whose host parses hook stdout as exactly one JSON object, so context
# must be buffered across the whole process and flushed once. Claude Code
# concatenates every raw print instead and needs no buffering.
_SINGLE_ENVELOPE_TARGETS = frozenset({"codex", "copilot"})


def buffers_single_envelope(target: str | None = None) -> bool:
    return (target or current_target()) in _SINGLE_ENVELOPE_TARGETS


def split_global(g: object) -> dict[str, dict]:
    """``targets.global`` in any shape → ``{target: record}``. Pure.

    Three shapes exist in the wild and this is the single rule for all of
    them, shared by the installer's migration (which writes the result back)
    and by the hook runtime (which only reads):

    - **keyed** — ``{"claude": {...}, "codex": {...}}``: returned as-is.
    - **legacy flat** — the pre-multi-target record's fields (path, profile,
      installed_at, …) sitting directly under ``global``: nested under
      ``claude``.
    - **mixed** — flat claude fields *alongside* a keyed record for another
      target, which a version-skewed pre-multi-target binary can still write.
      Every dict-valued record survives untouched; the flat fields are merged
      over whatever claude record already exists, since they are what the old
      binary just wrote.

    The mixed case is why this cannot be an ``any(not isinstance(...))``
    heuristic that hands back the raw blob: doing so exposes the sibling
    target's sub-dict as if it were a field of the current target's record.

    Never mutates *g* — the hook runtime has no business rewriting state.json,
    and the installer owns persistence.
    """
    if not isinstance(g, dict) or not g:
        return {}
    records = {k: v for k, v in g.items() if isinstance(v, dict)}
    flat = {k: v for k, v in g.items() if not isinstance(v, dict)}
    if not flat:
        return records
    return {**records, DEFAULT_TARGET: {**records.get(DEFAULT_TARGET, {}), **flat}}


def global_record(state: dict) -> dict:
    """The current target's record under ``targets.global`` in state.json.

    Tolerates every shape :func:`split_global` handles — hook processes never
    run the installer's migration, so they must read a half-migrated file
    correctly rather than assume one was applied.
    """
    return split_global(state.get("targets", {}).get("global")).get(current_target(), {})
