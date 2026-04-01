"""CLAUDE.md sanity check — block writes/edits that would bloat CLAUDE.md past a line limit."""

from pathlib import Path

from hooks.config import CLAUDE_MD_MAXLINES
from hooks.hook_manager import BlockAction


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

    max_lines = CLAUDE_MD_MAXLINES

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
