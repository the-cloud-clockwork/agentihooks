"""Conditions — bundle and profile scripts run on matching tool calls.

Scripts live in ``<bundle>/.claude/conditions`` and
``<profile>/.claude/conditions``, named ``<step>-<matcher>-<name>[.async].<ext>``.
The filename is the whole configuration: a cached index keyed on directory
mtimes answers "which scripts fire for this call" without listing a directory.
Reference: docs/hooks/conditions.md.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hooks.context import profile_chain, tool_matcher

STEPS = {"pre": "PreToolUse", "post": "PostToolUse"}
_RUNNERS = {".sh": ["bash"], ".bash": ["bash"], ".py": [sys.executable]}
_INDEX_VERSION = 1
_FRESH_NS = 2_000_000_000
_CODE_ROOT = Path(__file__).resolve().parents[2]
_UNSET = object()


# ---------------------------------------------------------------------------
# Filenames
# ---------------------------------------------------------------------------


def is_ignored(filename: str) -> bool:
    return filename.startswith((".", "_")) or filename.endswith("~") or filename.lower().endswith(".md")


def parse_filename(filename: str) -> dict:
    """Parse ``<step>-<matcher>-<name>[.async].<ext>``; raises ``ValueError``."""
    stem, dot, ext = filename.rpartition(".")
    if not dot or not stem or not ext or "-" in ext:
        raise ValueError("missing file extension")
    is_async = stem.endswith(".async")
    if is_async:
        stem = stem[: -len(".async")]
    parts = stem.split("-")
    if len(parts) < 3:
        raise ValueError("expected <step>-<matcher>-<name>.<ext>")
    step, name = parts[0].lower(), parts[-1]
    if step not in STEPS:
        raise ValueError(f"unknown step {parts[0]!r} (active steps: {', '.join(STEPS)})")
    if not name:
        raise ValueError("empty name")
    matcher = tool_matcher.parse("-".join(parts[1:-1])).token
    return {
        "step": step,
        "matcher": matcher,
        "name": name,
        "ext": "." + ext.lower(),
        "async": is_async,
        "key": f"{step}-{matcher}-{name.lower()}",
    }


# ---------------------------------------------------------------------------
# Layers and index
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    from hooks.config import AGENTIHOOKS_HOME
    from hooks.targets import current_target

    root = f"{zlib.crc32(str(_CODE_ROOT).encode()):08x}"
    return AGENTIHOOKS_HOME / "cache" / f"conditions-index.{current_target()}.{root}.json"


def layer_dirs(state: dict) -> tuple[list[tuple[str, Path]], list[Path]]:
    """Condition dirs in layer order, plus every profile-dir candidate probed to find them."""
    bundle = profile_chain.bundle_path(state)
    profile_csv = profile_chain.active_profile(state)
    linked = profile_chain.linked_profiles(state)
    layers: list[tuple[str, Path]] = []
    if bundle is not None:
        layers.append(("bundle", bundle / ".claude" / "conditions"))
    for name, profile_dir in profile_chain.profile_dirs(bundle, profile_csv, linked):
        layers.append((f"profile:{name}", profile_dir / ".claude" / "conditions"))
    probed = [path for _, paths in profile_chain.profile_candidates(bundle, profile_csv, linked) for path in paths]
    return layers, probed


def scan_layers(layers: list[tuple[str, Path]]) -> tuple[list[dict], list[dict]]:
    """(conditions in execution order, files that do not parse)."""
    by_key: dict[str, dict] = {}
    invalid: list[dict] = []
    for rank, (source, directory) in enumerate(layers):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for filename in names:
            path = directory / filename
            if is_ignored(filename) or not path.is_file():
                continue
            try:
                meta = parse_filename(filename)
            except ValueError as e:
                invalid.append({"path": str(path), "source": source, "error": str(e)})
                continue
            by_key[meta["key"]] = {**meta, "file": filename, "path": str(path), "source": source, "rank": rank}
    entries = sorted(by_key.values(), key=lambda e: (e["rank"], e["file"]))
    for order, entry in enumerate(entries):
        entry["order"] = order
    return entries, invalid


def build_index(entries: list[dict]) -> dict:
    index = {step: {"any": [], "exact": {}, "mcp": [], "mcp_server": {}, "cli": {}} for step in STEPS}
    for entry in entries:
        bucket = index[entry["step"]]
        for alt in tool_matcher.parse(entry["matcher"]).alternatives:
            if alt.kind in ("any", "mcp"):
                bucket[alt.kind].append(entry)
            else:
                key = {"tool": "exact", "mcp_server": "mcp_server", "cli": "cli"}[alt.kind]
                bucket[key].setdefault(alt.value, []).append(entry)
    return index


def _sig(path: Path) -> list[int]:
    try:
        st = os.stat(path)
    except OSError:
        return [-1]
    return [st.st_mtime_ns, st.st_size, st.st_ino]


def _fresh(sig: list[int], now_ns: int) -> bool:
    return sig[0] != -1 and now_ns - sig[0] < _FRESH_NS


def load_index() -> dict:
    """The condition index, rebuilt only when state.json or a condition dir changed."""
    cache = _cache_path()
    state_sig = _sig(profile_chain.state_path())
    try:
        cached = json.loads(cache.read_text())
    except (OSError, ValueError):
        cached = None
    if (
        isinstance(cached, dict)
        and cached.get("version") == _INDEX_VERSION
        and cached.get("state") == state_sig
        and all(Path(p).is_dir() == exists for p, exists in cached.get("probed", []))
        and all(_sig(Path(d)) == sig for d, sig in cached.get("dirs", []))
    ):
        return cached["index"]
    return _rebuild(cache, state_sig)


def _rebuild(cache: Path, state_sig: list[int]) -> dict:
    parsed = True
    try:
        text = profile_chain.state_path().read_text()
        state = json.loads(text) if text.strip() else {}
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError):
        state, parsed = {}, False
    if not isinstance(state, dict):
        state, parsed = {}, False
    layers, probed = layer_dirs(state)
    entries, _invalid = scan_layers(layers)
    index = build_index(entries)
    dirs = [[str(d), _sig(d)] for _, d in layers]
    now_ns = time.time_ns()
    if parsed and not _fresh(state_sig, now_ns) and not any(_fresh(sig, now_ns) for _, sig in dirs):
        record = {
            "version": _INDEX_VERSION,
            "state": state_sig,
            "probed": [[str(p), p.is_dir()] for p in probed],
            "dirs": dirs,
            "index": index,
        }
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(record))
            os.replace(tmp, cache)
        except OSError:
            pass
    return index


def matching(step: str, tool_name: str, tool_input: dict | None, index: dict | None = None) -> list[dict]:
    bucket = (index if index is not None else load_index()).get(step)
    if not bucket:
        return []
    name = (tool_name or "").lower()
    found = list(bucket["any"]) + bucket["exact"].get(name, [])
    if name.startswith("mcp__"):
        found += bucket["mcp"] + bucket["mcp_server"].get("mcp__" + name.split("__")[1], [])
    if name == "bash" and bucket["cli"]:
        for head in tool_matcher.command_heads(str((tool_input or {}).get("command") or "")):
            found += bucket["cli"].get(head, [])
    unique = {entry["path"]: entry for entry in found}
    return sorted(unique.values(), key=lambda e: e["order"])


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _command(entry: dict) -> list[str] | None:
    runner = _RUNNERS.get(entry["ext"])
    if runner:
        return [*runner, entry["path"]]
    if os.access(entry["path"], os.X_OK):
        return [entry["path"]]
    return None


def _env(entry: dict, step: str, payload: dict) -> dict[str, str]:
    from hooks.targets import current_target

    env = dict(os.environ)
    env.update(
        {
            "AH_STEP": step,
            "AH_EVENT": STEPS[step],
            "AH_TOOL_NAME": str(payload.get("tool_name") or ""),
            "AH_TOOL_USE_ID": str(payload.get("tool_use_id") or ""),
            "AH_SESSION_ID": str(payload.get("session_id") or ""),
            "AH_CWD": str(payload.get("cwd") or ""),
            "AH_TRANSCRIPT_PATH": str(payload.get("transcript_path") or ""),
            "AH_PERMISSION_MODE": str(payload.get("permission_mode") or ""),
            "AH_TARGET": current_target(),
            "AH_CONDITION": entry["file"],
            "AH_CONDITION_SOURCE": entry["source"],
        }
    )
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def execute(entry: dict, step: str, payload: dict, timeout: float) -> dict:
    """Run one condition; never raises."""
    cmd = _command(entry)
    if cmd is None:
        return {"error": "no runner for the extension and the file is not executable"}
    cwd = payload.get("cwd") or None
    if cwd and not os.path.isdir(cwd):
        cwd = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=_env(entry, step, payload),
            start_new_session=True,
            text=True,
        )
    except OSError as e:
        return {"error": str(e)}
    try:
        stdout, stderr = proc.communicate(json.dumps(payload), timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return {"error": f"timed out after {timeout:g}s"}
    return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr}


def _run_detached(entry: dict, step: str, payload: dict, timeout: float) -> None:
    result = execute(entry, step, payload, timeout)
    if result.get("error") or result.get("returncode"):
        sys.stderr.write(f"[condition] {entry['file']}: {result.get('error') or result.get('stderr', '').strip()}\n")


def _run_all(entries: list[dict], step: str, payload: dict) -> list[dict]:
    from hooks.config import CONDITIONS_MAX_PARALLEL, CONDITIONS_TIMEOUT_SEC

    if len(entries) == 1 or CONDITIONS_MAX_PARALLEL == 1:
        return [execute(entry, step, payload, CONDITIONS_TIMEOUT_SEC) for entry in entries]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(CONDITIONS_MAX_PARALLEL, len(entries))) as pool:
        return list(pool.map(lambda entry: execute(entry, step, payload, CONDITIONS_TIMEOUT_SEC), entries))


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    contexts: list[str] = field(default_factory=list)
    input_patch: dict = field(default_factory=dict)
    input_writers: list[str] = field(default_factory=list)
    output: Any = _UNSET
    output_writers: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def decision(self) -> str | None:
        return next((d for d in ("deny", "ask", "allow") if d in self.decisions), None)


def _parse_stdout(stdout: str) -> dict:
    text = (stdout or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    return {"context": text}


def _shape_output(payload: dict, value: Any) -> Any:
    tool_name = str(payload.get("tool_name") or "")
    if tool_name.startswith("mcp__"):
        return value
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and tool_name == "Bash":
        response = payload.get("tool_response")
        return {**(response if isinstance(response, dict) else {}), "stdout": value, "stderr": ""}
    return _UNSET


def _failed(entry: dict, reason: str, result: StepResult) -> None:
    from hooks.common import log
    from hooks.observability import otel

    log("condition failed", {"condition": entry["path"], "reason": reason})
    otel.emit_event("agentihooks.condition.failed", {"condition": entry["file"], "reason": reason[:200]})
    result.contexts.append(f"[condition {entry['file']}] failed ({reason}) — skipped")


def merge(step: str, payload: dict, runs: list[tuple[dict, dict]]) -> StepResult:
    result = StepResult()
    for entry, run in runs:
        label = entry["file"]
        code = run.get("returncode")
        if run.get("error") or code not in (0, 2):
            _failed(entry, run.get("error") or f"exit {code}: {(run.get('stderr') or '').strip()[:300]}", result)
            continue
        if code == 2:
            result.decisions.append("deny")
            result.reasons.append(f"[condition {label}] {(run.get('stderr') or '').strip() or 'blocked'}")
            continue
        out = _parse_stdout(run.get("stdout", ""))
        if out.get("context"):
            result.contexts.append(f"[condition {label}]\n{out['context']}")
        if step == "pre" and isinstance(out.get("tool_input"), dict):
            result.input_patch.update(out["tool_input"])
            result.input_writers.append(label)
        if step == "post" and "tool_output" in out:
            shaped = _shape_output(payload, out["tool_output"])
            if shaped is _UNSET:
                _failed(entry, "tool_output must be an object for this tool", result)
            else:
                result.output = shaped
                result.output_writers.append(label)
        if out.get("decision") in ("allow", "ask", "deny"):
            result.decisions.append(out["decision"])
            if out.get("reason"):
                result.reasons.append(f"[condition {label}] {out['reason']}")
    if len(result.output_writers) > 1:
        from hooks.common import log

        log("conditions: several conditions replaced the output; last one wins", {"writers": result.output_writers})
    return result


def run_step(step: str, payload: dict) -> StepResult | None:
    """Run every condition matching this call. None when nothing matched."""
    from hooks.config import CONDITIONS_ENABLED, CONDITIONS_TIMEOUT_SEC

    if not CONDITIONS_ENABLED:
        return None
    entries = matching(step, str(payload.get("tool_name") or ""), payload.get("tool_input") or {})
    if not entries:
        return None
    detached = [entry for entry in entries if entry["async"]]
    if detached:
        from hooks._async import fork_and_call

        for entry in detached:
            fork_and_call(
                _run_detached,
                entry,
                step,
                payload,
                CONDITIONS_TIMEOUT_SEC,
                timeout_sec=int(CONDITIONS_TIMEOUT_SEC) + 5,
                task_name=f"condition:{entry['file']}",
            )
    synchronous = [entry for entry in entries if not entry["async"]]
    if not synchronous:
        return None
    return merge(step, payload, list(zip(synchronous, _run_all(synchronous, step, payload))))


# ---------------------------------------------------------------------------
# Policy per step — what the host is told
# ---------------------------------------------------------------------------


@dataclass
class PreEffect:
    contexts: list[str]
    block: str | None = None
    rewrite: dict | None = None
    decision: str | None = None
    reason: str = ""


def pre_effect(payload: dict) -> PreEffect | None:
    result = run_step("pre", payload)
    if result is None:
        return None
    from hooks.targets.capabilities import applies_input_rewrite, honors_allow_ask

    effect = PreEffect(contexts=list(result.contexts))
    decision = result.decision
    reason = "\n".join(result.reasons)
    if decision == "ask" and not honors_allow_ask():
        decision = "deny"
        reason = reason or "a condition asked for confirmation and this harness cannot prompt"
    if decision == "deny":
        effect.block = reason or "blocked by a condition"
        return effect
    if not honors_allow_ask():
        decision = None
    if result.input_patch:
        writers = ", ".join(result.input_writers)
        if not applies_input_rewrite():
            effect.contexts.append(f"[conditions] input rewrite by {writers} dropped: this harness cannot apply it")
        elif decision is None and payload.get("permission_mode") != "bypassPermissions":
            effect.contexts.append(
                f"[conditions] input rewrite by {writers} dropped: outside bypassPermissions "
                'a rewriting condition must return "decision": "allow" or "ask"'
            )
        else:
            effect.rewrite = {**(payload.get("tool_input") or {}), **result.input_patch}
            effect.contexts.append(f"[conditions] tool input rewritten by {writers}")
            decision = decision or "allow"
    effect.decision = decision
    effect.reason = reason
    return effect


@dataclass
class PostEffect:
    contexts: list[str]
    hook_fields: dict = field(default_factory=dict)
    top_fields: dict = field(default_factory=dict)


def post_effect(payload: dict) -> PostEffect | None:
    result = run_step("post", payload)
    if result is None:
        return None
    from hooks.targets.capabilities import supports_output_rewrite

    effect = PostEffect(contexts=list(result.contexts))
    blocked = result.decision == "deny"
    reason = "\n".join(result.reasons) or "blocked by a condition"
    if supports_output_rewrite():
        if result.output is not _UNSET:
            effect.hook_fields["updatedToolOutput"] = result.output
        if blocked:
            effect.top_fields = {"decision": "block", "reason": reason}
    else:
        if result.output_writers:
            writers = ", ".join(result.output_writers)
            effect.contexts.append(f"[conditions] output rewrite by {writers} dropped: this harness cannot apply it")
        if blocked:
            effect.contexts.append(reason)
    return effect
