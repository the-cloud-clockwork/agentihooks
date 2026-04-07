"""Version Guard — blocks AI from modifying version fields in project files.

Version bumping should be handled by CI/CD workflows (release.yml),
not by the AI editing pyproject.toml, package.json, Cargo.toml, etc.

Raises BlockAction when Edit or Write targets a project manifest file
and the content contains a version field change.
"""

import re

from hooks.hook_manager import BlockAction

# Files that contain version fields managed by CI
_VERSION_FILES = {
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "setup.cfg",
    "setup.py",
    "version.txt",
    "VERSION",
}

# Patterns that indicate a version field is being modified
_VERSION_PATTERNS = [
    re.compile(r'version\s*[=:]\s*["\']?\d+\.\d+', re.IGNORECASE),
    re.compile(r'"version"\s*:\s*"', re.IGNORECASE),
]


def check_version_guard(payload: dict) -> None:
    """Block version field modifications in project manifest files.

    Raises BlockAction if the tool is Edit/Write targeting a version file
    and the content contains a version field pattern.
    """
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    if tool_name not in ("Edit", "Write"):
        return

    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    # Check if the target file is a version-managed manifest
    filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    if filename not in _VERSION_FILES:
        return

    # Check if the change touches a version field
    content = ""
    if tool_name == "Edit":
        content = tool_input.get("new_string", "")
        old = tool_input.get("old_string", "")
        # Only block if the version is actually changing
        if content == old:
            return
        content = f"{old}\n{content}"
    elif tool_name == "Write":
        content = tool_input.get("content", "")

    if not content:
        return

    for pattern in _VERSION_PATTERNS:
        if pattern.search(content):
            raise BlockAction(
                f"BLOCKED: Version field modification in {filename} is not allowed. "
                "Version bumping is handled by the release workflow (gh workflow run release.yml -f bump=patch|minor|major). "
                "Do not edit version fields manually."
            )
