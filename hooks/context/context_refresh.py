"""Context Refresh — periodically re-injects rules and CLAUDE.md into Claude's context.

Combats attention decay in long sessions where rules loaded at position 0
(CLAUDE.md, rules/*.md) lose weight as conversation grows past 50+ turns.

Injects on UserPromptSubmit:
  - Rules: every CONTEXT_REFRESH_INTERVAL turns (default 20)
  - CLAUDE.md: every CONTEXT_REFRESH_CLAUDE_MD_INTERVAL turns (default 40)

Turn counter persisted via Redis (primary) with file-based fallback.
Unlike retry_breaker's in-memory fallback, file persistence is required
here because each hook invocation is a separate subprocess.

Redis key:
    agenticore:ctx_refresh:{session_id} — Hash with fields:
        turn_count    int   UserPromptSubmit events seen
        last_refresh  int   turn number of last rules injection
        last_claude_md_refresh  int   turn number of last CLAUDE.md injection

File fallback:
    ~/.agentihooks/ctx_refresh_{session_id}.json

Public API:
    maybe_refresh(session_id, project_dir)  — called from on_user_prompt_submit
    clear_session_state(session_id)         — called from on_session_end
"""

import json
import os
import re
from pathlib import Path

from hooks._redis import get_redis, redis_key
from hooks.common import log

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)---\n", re.DOTALL)
_SESSION_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")
_DEFAULT_PRIORITY = 5


# ---------------------------------------------------------------------------
# State management (Redis + file fallback)
# ---------------------------------------------------------------------------


def _state_file(session_id: str) -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    safe_id = _SESSION_ID_RE.sub("_", session_id)
    return Path(AGENTIHOOKS_HOME) / f"ctx_refresh_{safe_id}.json"


def _default_state() -> dict:
    return {"turn_count": 0, "last_refresh": 0, "last_claude_md_refresh": 0}


def _get_state(session_id: str) -> dict:
    r = get_redis()
    if r is not None:
        try:
            raw = r.hgetall(redis_key("ctx_refresh", session_id))
            if raw:
                return {
                    "turn_count": int(raw.get("turn_count", 0)),
                    "last_refresh": int(raw.get("last_refresh", 0)),
                    "last_claude_md_refresh": int(raw.get("last_claude_md_refresh", 0)),
                }
        except Exception as e:
            log("context_refresh: Redis read error", {"error": str(e)})

    # File fallback
    fp = _state_file(session_id)
    try:
        if fp.exists():
            data = json.loads(fp.read_text())
            # Backfill new field for pre-existing state files
            data.setdefault("last_claude_md_refresh", 0)
            return data
    except Exception as e:
        log("context_refresh: file read error", {"error": str(e)})

    return _default_state()


def _set_state(session_id: str, state: dict) -> None:
    # Redis primary
    r = get_redis()
    if r is not None:
        try:
            k = redis_key("ctx_refresh", session_id)
            r.hset(
                k,
                mapping={
                    "turn_count": str(state["turn_count"]),
                    "last_refresh": str(state["last_refresh"]),
                    "last_claude_md_refresh": str(state.get("last_claude_md_refresh", 0)),
                },
            )
            r.expire(k, 86400)  # 24h TTL
        except Exception as e:
            log("context_refresh: Redis write error", {"error": str(e)})

    # Always write file fallback
    fp = _state_file(session_id)
    tmp = fp.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state))
        os.replace(tmp, fp)
    except Exception as e:
        log("context_refresh: file write error", {"error": str(e)})


# ---------------------------------------------------------------------------
# Content loading
# ---------------------------------------------------------------------------


def _parse_frontmatter(raw: str) -> tuple[int, str]:
    """Extract priority from YAML frontmatter and return (priority, content_without_frontmatter).

    Supported frontmatter field: ``priority: N`` (integer, lower = higher priority).
    Default priority is 5. Files without frontmatter get default priority.
    """
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return _DEFAULT_PRIORITY, raw

    fm_block = m.group(1)
    content = raw[m.end() :]
    priority = _DEFAULT_PRIORITY

    for line in fm_block.splitlines():
        line = line.strip()
        if line.startswith("priority:"):
            try:
                priority = int(line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                pass
            break

    return priority, content


def _load_rules_files(rules_dir: str, include_project: bool, project_dir: str = "") -> list[tuple[str, str]]:
    # Collect as (priority, name, content)
    entries: list[tuple[int, str, str]] = []

    dirs = [Path(rules_dir)]
    if include_project:
        base = project_dir or os.environ.get("CLAUDE_PROJECT_DIR", "")
        if base:
            project_rules = Path(base) / ".claude" / "rules"
        else:
            project_rules = Path(".claude/rules")
        if project_rules.is_dir():
            dirs.append(project_rules)

    # Include rules from active overlays (mid-session profile activation)
    try:
        from scripts.overlay import get_active_overlays

        for overlay in get_active_overlays():
            rules_dir_path = overlay.get("rules_dir")
            if rules_dir_path and Path(rules_dir_path).is_dir():
                dirs.append(Path(rules_dir_path))
    except Exception:
        pass

    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                if f.name == "README.md":
                    continue
                try:
                    raw = f.read_text().strip()
                    if not raw:
                        continue
                    priority, content = _parse_frontmatter(raw)
                    content = content.strip()
                    if content:
                        entries.append((priority, f.name, content))
                except Exception:
                    continue
        except Exception:
            continue

    # Sort by priority (lower first), then alphabetically within same priority
    entries.sort(key=lambda e: (e[0], e[1]))

    return [(name, content) for _, name, content in entries]


def _load_claude_md(project_dir: str = "") -> str | None:
    """Load CLAUDE.md content — global first, then project-level appended."""
    from hooks.config import CONTEXT_REFRESH_MAX_CHARS

    parts: list[str] = []

    # Global CLAUDE.md
    global_md = Path.home() / ".claude" / "CLAUDE.md"
    if global_md.is_file():
        try:
            content = global_md.read_text().strip()
            if content:
                parts.append(content)
        except Exception:
            pass

    # Project CLAUDE.md
    base = project_dir or os.environ.get("CLAUDE_PROJECT_DIR", "")
    if base:
        project_md = Path(base) / "CLAUDE.md"
        if project_md.is_file():
            try:
                content = project_md.read_text().strip()
                if content:
                    parts.append(f"\n\n<!-- project: {Path(base).name} -->\n{content}")
            except Exception:
                pass

    if not parts:
        return None

    combined = "\n".join(parts)

    # Apply preprocessor compression if enabled
    try:
        from hooks.context.preprocessor import get_level_from_config, preprocess

        level = get_level_from_config()
        if level > 0:
            combined = preprocess(combined, level)
    except Exception:
        pass

    # Apply size cap — CLAUDE.md can be large
    if len(combined) > CONTEXT_REFRESH_MAX_CHARS:
        combined = combined[:CONTEXT_REFRESH_MAX_CHARS] + "\n[... truncated at size limit]"

    return combined


def _build_injection_text(rules: list[tuple[str, str]], turn: int, interval: int) -> str:
    from hooks.config import CONTEXT_REFRESH_MAX_CHARS

    # Load preprocessor level once for the batch
    compression_level = 0
    try:
        from hooks.context.preprocessor import get_level_from_config

        compression_level = get_level_from_config()
    except Exception:
        pass

    lines = [f"Re-injecting active rules (turn {turn}, every {interval} turns).\n"]
    total = len(lines[0])
    added = 0

    for name, content in rules:
        # Apply compression if enabled
        if compression_level > 0:
            try:
                from hooks.context.preprocessor import preprocess

                content = preprocess(content, compression_level)
            except Exception:
                pass

        header = f"\n[{name}]\n"
        entry_len = len(header) + len(content)
        if total + entry_len > CONTEXT_REFRESH_MAX_CHARS:
            remaining = len(rules) - added
            if remaining > 0:
                lines.append(f"\n[{remaining} rule(s) omitted — size limit reached]")
            break
        lines.append(header + content)
        total += entry_len
        added += 1

    return "".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def maybe_refresh(session_id: str, project_dir: str = "") -> None:
    if not session_id:
        return

    from hooks.config import (
        CONTEXT_REFRESH_CLAUDE_MD_INTERVAL,
        CONTEXT_REFRESH_ENABLED,
        CONTEXT_REFRESH_INCLUDE_PROJECT,
        CONTEXT_REFRESH_INTERVAL,
        CONTEXT_REFRESH_RULES_DIR,
    )

    if not CONTEXT_REFRESH_ENABLED:
        return

    state = _get_state(session_id)
    turn = state["turn_count"] + 1
    state["turn_count"] = turn

    # --- Rules re-injection ---
    if (turn % CONTEXT_REFRESH_INTERVAL) == 0:
        rules = _load_rules_files(CONTEXT_REFRESH_RULES_DIR, CONTEXT_REFRESH_INCLUDE_PROJECT, project_dir)
        if rules:
            from hooks.common import inject_banner

            text = _build_injection_text(rules, turn, CONTEXT_REFRESH_INTERVAL)
            inject_banner(f"CONTEXT REFRESH — rules (turn {turn})", text, skip_compression=True)
            state["last_refresh"] = turn
            log(
                "context_refresh: injected rules",
                {
                    "session_id": session_id,
                    "turn": turn,
                    "rules_count": len(rules),
                },
            )

    # --- CLAUDE.md re-injection (separate cadence) ---
    if CONTEXT_REFRESH_CLAUDE_MD_INTERVAL > 0 and (turn % CONTEXT_REFRESH_CLAUDE_MD_INTERVAL) == 0:
        claude_md = _load_claude_md(project_dir)
        if claude_md:
            from hooks.common import inject_banner

            inject_banner(
                f"CONTEXT REFRESH — CLAUDE.md (turn {turn})",
                f"Re-injecting CLAUDE.md (turn {turn}, every {CONTEXT_REFRESH_CLAUDE_MD_INTERVAL} turns).\n\n{claude_md}",
                skip_compression=True,
            )
            state["last_claude_md_refresh"] = turn
            log(
                "context_refresh: injected CLAUDE.md",
                {
                    "session_id": session_id,
                    "turn": turn,
                },
            )

    _set_state(session_id, state)


def force_rules_refresh(session_id: str, project_dir: str = "") -> None:
    """Force immediate re-injection of all rules (including overlay rules).

    Called when an overlay is activated/deactivated to ensure the session
    gets the new rules immediately instead of waiting for the next interval.
    """
    from hooks.common import inject_context
    from hooks.config import (
        CONTEXT_REFRESH_INCLUDE_PROJECT,
        CONTEXT_REFRESH_INTERVAL,
        CONTEXT_REFRESH_RULES_DIR,
    )

    rules = _load_rules_files(CONTEXT_REFRESH_RULES_DIR, CONTEXT_REFRESH_INCLUDE_PROJECT, project_dir)
    if not rules:
        return

    state = _get_state(session_id)
    turn = state.get("turn_count", 0)
    text = _build_injection_text(rules, turn, CONTEXT_REFRESH_INTERVAL)
    inject_context(text)

    # Reset counter so next normal refresh doesn't double-inject
    state["last_rules_turn"] = turn
    _set_state(session_id, state)
    log("context_refresh: forced rules refresh", {"rules_count": len(rules)})


def clear_session_state(session_id: str) -> None:
    if not session_id:
        return

    r = get_redis()
    if r is not None:
        try:
            r.delete(redis_key("ctx_refresh", session_id))
        except Exception as e:
            log("context_refresh: Redis delete error", {"error": str(e)})

    try:
        _state_file(session_id).unlink(missing_ok=True)
    except Exception:
        pass
