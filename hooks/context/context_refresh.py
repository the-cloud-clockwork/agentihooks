"""Context Refresh — periodically re-injects rules into Claude's context.

Combats attention decay in long sessions where rules loaded at position 0
(CLAUDE.md, rules/*.md) lose weight as conversation grows past 50+ turns.

Injects on UserPromptSubmit every CONTEXT_REFRESH_INTERVAL turns.

Turn counter persisted via Redis (primary) with file-based fallback.
Unlike retry_breaker's in-memory fallback, file persistence is required
here because each hook invocation is a separate subprocess.

Redis key:
    agenticore:ctx_refresh:{session_id} — Hash with fields:
        turn_count    int   UserPromptSubmit events seen
        last_refresh  int   turn number of last injection

File fallback:
    ~/.agentihooks/ctx_refresh_{session_id}.json

Public API:
    maybe_refresh(session_id)       — called from on_user_prompt_submit
    clear_session_state(session_id) — called from on_session_end
"""

import json
import os
import re
from pathlib import Path

from hooks._redis import get_redis, redis_key
from hooks.common import log

_FRONTMATTER_RE = re.compile(r"^---\n.*?---\n", re.DOTALL)
_SESSION_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


# ---------------------------------------------------------------------------
# State management (Redis + file fallback)
# ---------------------------------------------------------------------------


def _state_file(session_id: str) -> Path:
    from hooks.config import AGENTIHOOKS_HOME

    safe_id = _SESSION_ID_RE.sub("_", session_id)
    return Path(AGENTIHOOKS_HOME) / f"ctx_refresh_{safe_id}.json"


def _default_state() -> dict:
    return {"turn_count": 0, "last_refresh": 0}


def _get_state(session_id: str) -> dict:
    r = get_redis()
    if r is not None:
        try:
            raw = r.hgetall(redis_key("ctx_refresh", session_id))
            if raw:
                return {
                    "turn_count": int(raw.get(b"turn_count", raw.get("turn_count", 0))),
                    "last_refresh": int(raw.get(b"last_refresh", raw.get("last_refresh", 0))),
                }
        except Exception as e:
            log("context_refresh: Redis read error", {"error": str(e)})

    # File fallback
    fp = _state_file(session_id)
    try:
        if fp.exists():
            return json.loads(fp.read_text())
    except Exception as e:
        log("context_refresh: file read error", {"error": str(e)})

    return _default_state()


def _set_state(session_id: str, state: dict) -> None:
    # Redis primary
    r = get_redis()
    if r is not None:
        try:
            k = redis_key("ctx_refresh", session_id)
            r.hset(k, mapping={
                "turn_count": str(state["turn_count"]),
                "last_refresh": str(state["last_refresh"]),
            })
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
# Rules loading
# ---------------------------------------------------------------------------


def _strip_frontmatter(content: str) -> str:
    return _FRONTMATTER_RE.sub("", content)


def _load_rules_files(rules_dir: str, include_project: bool) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []

    dirs = [Path(rules_dir)]
    if include_project:
        project_rules = Path(".claude/rules")
        if project_rules.is_dir():
            dirs.append(project_rules)

    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.md")):
                if f.name == "README.md":
                    continue
                try:
                    content = _strip_frontmatter(f.read_text().strip())
                    if content:
                        results.append((f.name, content))
                except Exception:
                    continue
        except Exception:
            continue

    return results


def _build_injection_text(rules: list[tuple[str, str]], turn: int, interval: int) -> str:
    from hooks.config import CONTEXT_REFRESH_MAX_CHARS

    lines = [f"Re-injecting active rules (turn {turn}, every {interval} turns).\n"]
    total = len(lines[0])

    for name, content in rules:
        header = f"\n[{name}]\n"
        entry_len = len(header) + len(content)
        if total + entry_len > CONTEXT_REFRESH_MAX_CHARS:
            remaining = len(rules) - len([l for l in lines if l.startswith("\n[")])
            if remaining > 0:
                lines.append(f"\n[{remaining} rule(s) omitted — size limit reached]")
            break
        lines.append(header + content)
        total += entry_len

    return "".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def maybe_refresh(session_id: str) -> None:
    if not session_id:
        return

    from hooks.config import (
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

    should_inject = (turn % CONTEXT_REFRESH_INTERVAL) == 0

    if should_inject:
        rules = _load_rules_files(CONTEXT_REFRESH_RULES_DIR, CONTEXT_REFRESH_INCLUDE_PROJECT)
        if rules:
            from hooks.common import inject_banner

            text = _build_injection_text(rules, turn, CONTEXT_REFRESH_INTERVAL)
            inject_banner(f"CONTEXT REFRESH (turn {turn})", text)
            state["last_refresh"] = turn
            log("context_refresh: injected rules", {
                "session_id": session_id,
                "turn": turn,
                "rules_count": len(rules),
            })

    _set_state(session_id, state)


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
