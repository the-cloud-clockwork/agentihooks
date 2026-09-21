"""CLAUDE.md sanity check — block writes/edits that would bloat CLAUDE.md past a line limit."""

import re
from pathlib import Path

from hooks._redis import SESSION_TTL, get_redis, redis_key
from hooks.common import log
from hooks.config import AGENTIHOOKS_HOME, CLAUDE_MD_MAXLINES
from hooks.hook_manager import BlockAction

_LIMIT_TYPE = "claude_md_max_lines"
_LIMIT_DIR = AGENTIHOOKS_HOME / "claude_md_limits"
_LIMIT_SIGNAL = re.compile(r"(?<![\w-])claude-md-max-lines=([1-9]\d*)(?![\w-])", re.IGNORECASE)


def _limit_key(session_id: str) -> str:
    return redis_key(_LIMIT_TYPE, session_id)


def _limit_flag(session_id: str) -> Path:
    return _LIMIT_DIR / f"{session_id}.limit"


def parse_max_lines_signal(text: str) -> int | None:
    matches = _LIMIT_SIGNAL.findall(text or "")
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def set_session_max_lines(session_id: str, max_lines: int) -> int | None:
    if not session_id or max_lines < CLAUDE_MD_MAXLINES:
        return None
    value = str(max_lines)
    r = get_redis()
    if r:
        try:
            r.setex(_limit_key(session_id), SESSION_TTL, value)
        except Exception as e:
            log("claude_md_sanity.set redis failed", {"error": str(e)})
    try:
        flag = _limit_flag(session_id)
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(value)
    except Exception as e:
        log("claude_md_sanity.set file failed", {"error": str(e)})
    return max_lines


def get_max_lines(session_id: str) -> int:
    if not session_id:
        return CLAUDE_MD_MAXLINES
    value = None
    r = get_redis()
    if r:
        try:
            value = r.get(_limit_key(session_id))
        except Exception:
            pass
    if value is None:
        try:
            flag = _limit_flag(session_id)
            value = flag.read_text().strip() if flag.is_file() else None
        except Exception:
            pass
    try:
        return max(CLAUDE_MD_MAXLINES, int(value)) if value is not None else CLAUDE_MD_MAXLINES
    except (TypeError, ValueError):
        return CLAUDE_MD_MAXLINES


def clear_session_max_lines(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_limit_key(session_id))
        except Exception:
            pass
    try:
        _limit_flag(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def check_claude_md_write(payload: dict) -> None:
    """Block Write/Edit operations that would push a CLAUDE.md file past the max line limit.

    Raises BlockAction if the resulting file would exceed CLAUDE_MD_MAXLINES.
    Silently returns for non-CLAUDE.md files.
    """
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    file_path = tool_input.get("file_path", "")
    PROTECTED_NAMES = {"CLAUDE.md", "CLAUDE.local.md"}
    if not file_path or Path(file_path).name not in PROTECTED_NAMES:
        return

    max_lines = get_max_lines(payload.get("session_id", ""))

    if tool_name == "Write":
        content = tool_input.get("content", "")
        resulting_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)

        if resulting_lines > max_lines:
            raise BlockAction(
                f"BLOCKED: Write to {file_path} would produce {resulting_lines} lines, "
                f"exceeding the CLAUDE.md cap of {max_lines} lines. "
                f"Trim the content to {max_lines} lines or fewer before writing."
            )

    elif tool_name == "Edit":
        disk_path = Path(file_path)

        # If file doesn't exist yet, Edit doesn't apply — let it fail naturally
        if not disk_path.is_file():
            return

        current_content = disk_path.read_text(encoding="utf-8")
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")

        if old_string and old_string in current_content:
            resulting_content = current_content.replace(old_string, new_string, 1)
        else:
            # Can't simulate — fall back to checking current size + new content growth
            resulting_content = current_content

        resulting_lines = resulting_content.count("\n") + (
            1 if resulting_content and not resulting_content.endswith("\n") else 0
        )

        if resulting_lines > max_lines:
            raise BlockAction(
                f"BLOCKED: Edit to {file_path} would produce {resulting_lines} lines, "
                f"exceeding the CLAUDE.md cap of {max_lines} lines. "
                f"Trim the file to {max_lines} lines or fewer before editing."
            )
