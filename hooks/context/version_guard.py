"""Version Guard — blocks AI from modifying version fields in project files.

Version bumping should be handled by CI/CD workflows (release.yml),
not by the AI editing pyproject.toml, package.json, Cargo.toml, etc.

Raises BlockAction when Edit or Write targets a project manifest file
and the content contains a version field change, or when the parsed version of
pyproject.toml, Cargo.toml or package.json changes. The one allowed change is
switching pyproject.toml from a static version to a setuptools-scm tag derived
one that adds no version literal.
"""

import json
import re
import tomllib
from pathlib import Path

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

_VERSION_LITERAL = re.compile(r'[\w.-]*version"?\s*[=:]\s*["\']?[\w.+-]*', re.IGNORECASE)

_VERSION_KEYS = {
    "pyproject.toml": ("project", "version"),
    "Cargo.toml": ("package", "version"),
    "package.json": ("version",),
}

_UNREADABLE = object()

_TAG_SWITCH_HINT = (
    " Switching to a setuptools-scm tag derived version is allowed once [tool.setuptools_scm] exists:"
    ' replace the version line with dynamic = ["version"] and add no version literal.'
)


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

    # Creating a manifest declares the first version; only changes to an existing one are bumps.
    target = Path(payload.get("cwd") or ".") / file_path
    if not target.exists():
        return

    # Check if the target file is a version-managed manifest
    filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    if filename not in _VERSION_FILES:
        return

    if filename in _VERSION_KEYS and _allows_parsed_change(filename, target, tool_name, tool_input):
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
            _refuse(filename)


def _edited_text(tool_name: str, tool_input: dict, before: str) -> str:
    if tool_name == "Write":
        return tool_input.get("content", "")
    count = -1 if tool_input.get("replace_all") else 1
    return before.replace(tool_input.get("old_string", ""), tool_input.get("new_string", ""), count)


def _version_literals(text: str) -> set[str]:
    return set(_VERSION_LITERAL.findall(text))


def _switches_to_tag_version(before: dict, after: dict) -> bool:
    dynamic = _field(after, ("project", "dynamic"))
    return (
        isinstance(_field(before, ("project", "version")), str)
        and isinstance(_field(after, ("project",)), dict)
        and _field(after, ("project", "version")) is None
        and isinstance(dynamic, list)
        and "version" in dynamic
        and _field(after, ("tool", "setuptools_scm")) is not None
    )


def _parsed(filename: str, text: str) -> object:
    try:
        return json.loads(text) if filename == "package.json" else tomllib.loads(text)
    except ValueError:
        return _UNREADABLE


def _field(data: object, keys: tuple[str, ...]) -> object:
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def _allows_parsed_change(filename: str, target: Path, tool_name: str, tool_input: dict) -> bool:
    before_text = target.read_text(encoding="utf-8", errors="replace")
    after_text = _edited_text(tool_name, tool_input, before_text)
    before, after = _parsed(filename, before_text), _parsed(filename, after_text)
    if before is _UNREADABLE or after is _UNREADABLE:
        changed = _version_literals(before_text) != _version_literals(after_text)
    elif filename == "pyproject.toml" and _switches_to_tag_version(before, after):
        if _version_literals(after_text) <= _version_literals(before_text):
            return True
        changed = True
    else:
        changed = _field(before, _VERSION_KEYS[filename]) != _field(after, _VERSION_KEYS[filename])
    if changed:
        _refuse(filename)
    return False


def _refuse(filename: str) -> None:
    hint = _TAG_SWITCH_HINT if filename == "pyproject.toml" else ""
    raise BlockAction(
        f"BLOCKED: Version field modification in {filename} is not allowed. "
        "Version bumping is handled by the release workflow (gh workflow run release.yml -f bump=patch|minor|major). "
        f"Do not edit version fields manually.{hint}"
    )
