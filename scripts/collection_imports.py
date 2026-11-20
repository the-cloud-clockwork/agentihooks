"""Finds the MCP SDK a test module loads while collecting or when its tests run, from module sources under a root."""

import re
from functools import cache
from pathlib import Path

HEAVY = "mcp"
_COMMENTS_STRINGS_CONTINUATIONS = re.compile(
    r"#[^\n]*|(\"\"\"|''')(?:\\[\s\S]|[^\\])*?\1|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|\\\n"
)
_IMPORT_BODY = (
    r"(?:import\s+(?P<modules>[^\n;]+)|from\s+(?P<module>\w[\w.]*)\s+import\s*(?P<members>\([^)]*\)|[^\n;]+))"
)
_IMPORT = re.compile(r"(?:^|;[ \t]*)" + _IMPORT_BODY, re.MULTILINE)
_ANY_IMPORT = re.compile(r"(?:^[ \t]*|;[ \t]*)" + _IMPORT_BODY, re.MULTILINE)
_FUNCTION = re.compile(r"([ \t]*)(?:async[ \t]+)?def[ \t]+(\w+)\b")
SDK_GROUP = 'xdist_group("mcp-sdk")'


@cache
def _module_file(root: Path, name: str) -> Path | None:
    base = root.joinpath(*name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _import_names(source: str, pattern: re.Pattern[str] = _IMPORT) -> set[str]:
    names: set[str] = set()
    for match in pattern.finditer(_COMMENTS_STRINGS_CONTINUATIONS.sub(" ", source)):
        if match["modules"]:
            names |= {alias.split()[0] for alias in match["modules"].split(",")}
        else:
            module = match["module"]
            names.add(module)
            members = match["members"].strip("() \t").split(",")
            names |= {f"{module}.{alias.split()[0]}" for alias in members if alias.strip()}
    return names


@cache
def _module_level_imports(path: Path) -> frozenset[str]:
    names = _import_names(path.read_text())
    return frozenset(".".join(parts[:i]) for parts in (n.split(".") for n in names) for i in range(1, len(parts) + 1))


def loads_heavy(root: Path, name: str, seen: set[str]) -> bool:
    """Return whether importing the named module under root loads the MCP SDK, skipping modules already seen."""
    if name in seen:
        return False
    seen.add(name)
    if name.split(".")[0] == HEAVY:
        return True
    path = _module_file(root, name)
    parents = {name.rsplit(".", i)[0] for i in range(1, name.count(".") + 1)}
    return path is not None and any(loads_heavy(root, dep, seen) for dep in parents | _module_level_imports(path))


@cache
def _function_sources(path: Path) -> dict[str, str]:
    lines = path.read_text().splitlines(keepends=True)
    bodies: dict[str, str] = {}
    for i, line in enumerate(lines):
        if (match := _FUNCTION.match(line)) and match[2] not in bodies:
            body: list[str] = []
            for rest in lines[i + 1 :]:
                stripped = rest.lstrip()
                if stripped and len(rest) - len(stripped) <= len(match[1]) and not stripped.startswith(")"):
                    break
                body.append(rest)
            bodies[match[2]] = "".join(body)
    return bodies


@cache
def _function_imports(path: Path, function: str) -> frozenset[str]:
    return frozenset(_import_names(_function_sources(path).get(function, ""), _ANY_IMPORT))


def calls_heavy(root: Path, name: str, seen: set[str]) -> bool:
    """Return whether calling the named function of a module under root imports a module that loads the MCP SDK."""
    module, _, function = name.rpartition(".")
    path = _module_file(root, module) if module else None
    if path is None or _module_file(root, name) is not None:
        return False
    return any(loads_heavy(root, dep, seen) for dep in _function_imports(path, function))


def loads_sdk_when_run(root: Path, source: str, seen: set[str] | None = None) -> bool:
    """Return whether the source, run against the modules under root, loads the MCP SDK at any import or call."""
    seen = set() if seen is None else seen
    return any(
        loads_heavy(root, name, seen) or calls_heavy(root, name, seen) for name in _import_names(source, _ANY_IMPORT)
    )
