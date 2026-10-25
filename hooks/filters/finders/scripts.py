import json
import subprocess
import sys
from pathlib import Path

from hooks import config
from hooks.common import log
from hooks.context import conditions, profile_chain
from hooks.filters.finders import explanation_tail, string_literals

_BUILTINS = {"string_literals": string_literals.find, "explanation_tail": explanation_tail.find}


def resolve(cwd: str | None) -> dict:
    try:
        state = json.loads(profile_chain.state_path().read_text())
    except FileNotFoundError:
        state = {}
    if not isinstance(state, dict):
        raise ValueError("condition state must be a mapping")
    layers, _ = conditions.layer_dirs(state, cwd)
    layers, _ = conditions._trusted_layers(layers, state)
    finders = dict(_BUILTINS)
    for _, directory in layers:
        for path in sorted((directory / "_finders").glob("*")):
            if path.is_file() and path.suffix in {".py", ".sh"} and not conditions.is_ignored(path.name):
                finders[path.stem] = path
    return finders


def _validate(raw: object, text: str) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("finder output must be a list")
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"start", "end", "text", "reason"}:
            raise ValueError("finder output must contain spans with text and reason")
        start, end = item["start"], item["end"]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
            raise ValueError("finder output has invalid offsets")
        if item["text"] != text[start:end] or not isinstance(item["reason"], str):
            raise ValueError("finder output does not match the source")
    return raw


def _execute(finder: Path, text: str, path: str, tool: str) -> object:
    command = [sys.executable if finder.suffix == ".py" else "bash", str(finder)]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(
            json.dumps({"text": text, "path": path, "tool": tool}), timeout=config.CONDITIONS_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        conditions._kill_group(proc)
        proc.communicate(timeout=1)
        raise
    if proc.returncode:
        raise ValueError(f"finder exited with status {proc.returncode}")
    return json.loads(stdout)


def run(name: str, finders: dict, text: str, path: str, tool: str) -> list[dict]:
    try:
        finder = finders[name]
        raw = finder(text, path, tool) if callable(finder) else _execute(finder, text, path, tool)
        return _validate(raw, text)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        log("filter finder failed", {"finder": name, "reason": str(error)})
        return []
