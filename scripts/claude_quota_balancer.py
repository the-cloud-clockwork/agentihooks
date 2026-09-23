#!/usr/bin/env python3

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

TOKEN_PREFIX = "AH_CC_TOKEN_"
OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
MAX_PROBE_WORKERS = 3
CACHE_TTL_SECONDS = 60
MIN_ROUTING_LEFT = 5.0


@dataclass(frozen=True)
class Credential:
    env_name: str
    token: str = field(repr=False)

    @property
    def account(self) -> str:
        return self.env_name.removeprefix(TOKEN_PREFIX)


@dataclass(frozen=True)
class QuotaWindow:
    used: float | None = None
    resets_at: int | None = None

    @property
    def remaining(self) -> float | None:
        if self.used is None:
            return None
        return max(0.0, 100.0 - self.used)


@dataclass(frozen=True)
class ProbeResult:
    account: str
    provider_status: str
    state: str
    margin: float | None
    five_hour: QuotaWindow
    seven_day: QuotaWindow
    fable: QuotaWindow = field(default_factory=QuotaWindow)
    model: str = "?"
    context_used: int | None = None
    context_capacity: int | None = None
    latency_ms: int | None = None
    error: str = ""

    @property
    def routing_left(self) -> float | None:
        return self.margin


@dataclass(frozen=True)
class SessionAccount:
    account: str
    method: str


@dataclass(frozen=True)
class RouteDecision:
    credential: Credential
    result: ProbeResult
    source: str


class RoutingError(RuntimeError):
    def __init__(self, message: str, results: list[ProbeResult] | None = None):
        super().__init__(message)
        self.results = results or []


def discover_credentials(environ: Mapping[str, str]) -> list[Credential]:
    return [
        Credential(name, environ[name])
        for name in sorted(environ)
        if name.startswith(TOKEN_PREFIX) and name != TOKEN_PREFIX and environ[name]
    ]


def credential_for_slug(credentials: list[Credential], slug: str) -> Credential:
    target = f"{TOKEN_PREFIX}{slug}"
    for credential in credentials:
        if credential.env_name == target:
            return credential
    available = ", ".join(credential.account for credential in credentials) or "none"
    raise RoutingError(f"account suffix '{slug}' not found; available: {available}")


def ancestor_oauth_token(pid: int | None = None) -> str:
    current = os.getppid() if pid is None else pid
    while current > 1:
        proc = Path("/proc") / str(current)
        with contextlib.suppress(OSError):
            for item in (proc / "environ").read_bytes().split(b"\0"):
                name, _, value = item.partition(b"=")
                if name == OAUTH_ENV.encode() and value:
                    return value.decode()
        try:
            current = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return ""
    return ""


def identify_session_account(
    session_env: Mapping[str, str],
    credentials: list[Credential],
    ancestor_token: str = "",
) -> SessionAccount:
    token = session_env.get(OAUTH_ENV) or ancestor_token
    if token:
        for credential in credentials:
            if credential.token == token:
                return SessionAccount(credential.account, "oauth-token")
        return SessionAccount("", "oauth-token-unmatched")
    present = discover_credentials(session_env)
    if len(present) == 1:
        return SessionAccount(present[0].account, "sole-token")
    return SessionAccount("", "unrouted")


def cached_observations(
    *,
    include_fable: bool = False,
    environ: Mapping[str, str] | None = None,
    cache_file: Path | None = None,
) -> list[tuple[float, ProbeResult]]:
    path = cache_file or _cache_path(os.environ if environ is None else environ)
    with _cache_lock(path):
        cache = _read_cache(path)
    modes = cache.get("modes") if isinstance(cache.get("modes"), dict) else {}
    mode_data = modes.get("fable" if include_fable else "normal")
    entries = mode_data.get("accounts") if isinstance(mode_data, dict) else None
    observations = []
    for entry in (entries or {}).values():
        result = _result_from_data(entry.get("result")) if isinstance(entry, dict) else None
        if result is not None and isinstance(entry.get("observed_at"), (int, float)):
            observations.append((float(entry["observed_at"]), result))
    return observations


def _cache_path(environ: Mapping[str, str]) -> Path:
    root = Path(environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))
    return root / "claude-router-cache.json"


def _window_data(window: QuotaWindow) -> dict:
    return {"used": window.used, "resets_at": window.resets_at}


def _window_from_data(data: object) -> QuotaWindow:
    if not isinstance(data, dict):
        return QuotaWindow()
    used = data.get("used")
    resets_at = data.get("resets_at")
    return QuotaWindow(
        used=float(used) if isinstance(used, (int, float)) else None,
        resets_at=int(resets_at) if isinstance(resets_at, (int, float)) else None,
    )


def _result_data(result: ProbeResult) -> dict:
    return {
        "account": result.account,
        "provider_status": result.provider_status,
        "state": result.state,
        "margin": result.margin,
        "five_hour": _window_data(result.five_hour),
        "seven_day": _window_data(result.seven_day),
        "fable": _window_data(result.fable),
        "model": result.model,
        "context_used": result.context_used,
        "context_capacity": result.context_capacity,
        "latency_ms": result.latency_ms,
    }


def _result_from_data(data: object) -> ProbeResult | None:
    if not isinstance(data, dict) or not isinstance(data.get("account"), str):
        return None
    margin = data.get("margin")
    return ProbeResult(
        account=data["account"],
        provider_status=str(data.get("provider_status") or "unknown"),
        state=str(data.get("state") or "UNKNOWN"),
        margin=float(margin) if isinstance(margin, (int, float)) else None,
        five_hour=_window_from_data(data.get("five_hour")),
        seven_day=_window_from_data(data.get("seven_day")),
        fable=_window_from_data(data.get("fable")),
        model=str(data.get("model") or "?"),
        context_used=int(data["context_used"]) if isinstance(data.get("context_used"), (int, float)) else None,
        context_capacity=(
            int(data["context_capacity"]) if isinstance(data.get("context_capacity"), (int, float)) else None
        ),
        latency_ms=int(data["latency_ms"]) if isinstance(data.get("latency_ms"), (int, float)) else None,
    )


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("version") == 1 else {"version": 1, "modes": {}}
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "modes": {}}


def _write_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise


@contextlib.contextmanager
def _cache_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _probe_command(model: str, claude_bin: str = "claude", include_partial: bool = False) -> list[str]:
    command = [
        claude_bin,
        "-p",
        "Reply with exactly OK.",
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--safe-mode",
        "--no-session-persistence",
        "--permission-prompts",
        "none",
        "--tools",
        "",
        "--system-prompt",
        "Reply with exactly OK.",
        "--max-budget-usd",
        "0.25" if model == "fable" else "0.10",
    ]
    if include_partial:
        command.append("--include-partial-messages")
    return command


def _child_environment(credential: Credential, environ: Mapping[str, str]) -> dict[str, str]:
    child = {name: value for name, value in environ.items() if not name.startswith(TOKEN_PREFIX)}
    child.pop("ANTHROPIC_API_KEY", None)
    child["CLAUDE_CODE_OAUTH_TOKEN"] = credential.token
    return child


def _window(info: dict, name: str) -> QuotaWindow:
    raw = (info.get("unifiedWindows") or {}).get(name) or {}
    utilization = raw.get("utilization")
    reset = raw.get("resetsAt")
    return QuotaWindow(
        used=float(utilization) * 100 if isinstance(utilization, (int, float)) else None,
        resets_at=int(reset) if isinstance(reset, (int, float)) else None,
    )


def _state(
    provider_status: str,
    five_hour: QuotaWindow,
    seven_day: QuotaWindow,
    fable: QuotaWindow | None = None,
) -> tuple[str, float | None]:
    if provider_status == "rejected":
        return "BLOCKED", 0.0
    windows = [five_hour, seven_day, *([fable] if fable is not None else [])]
    usages = [window.used for window in windows if window.used is not None]
    if len(usages) != len(windows):
        return "UNKNOWN", None
    highest = max(usages)
    margin = max(0.0, 100.0 - highest)
    if highest >= 90:
        return "DRAIN", margin
    if highest >= 80:
        return "DRAIN_SOON", margin
    if highest >= 65:
        return "REDUCE", margin
    return "NORMAL", margin


def parse_probe(account: str, output: str, elapsed_ms: int, include_fable: bool = False) -> ProbeResult:
    rate_info: dict = {}
    model = "?"
    usage: dict = {}
    model_usage: dict = {}

    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            rate_info = event["rate_limit_info"]
        elif event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model") or model
        elif event.get("type") == "assistant" and isinstance(event.get("message"), dict):
            message = event["message"]
            model = message.get("model") or model
            if isinstance(message.get("usage"), dict):
                usage = message["usage"]
        elif event.get("type") == "result":
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            if isinstance(event.get("modelUsage"), dict):
                model_usage = event["modelUsage"]

    five_hour = _window(rate_info, "five_hour")
    seven_day = _window(rate_info, "seven_day")
    fable = _window(rate_info, "seven_day_overage_included")
    provider_status = str(rate_info.get("status") or "unknown")
    state, margin = _state(provider_status, five_hour, seven_day, fable if include_fable else None)
    # Keep probe context telemetry for future session retirement when a reused session nears its context limit.
    context_used = None
    if usage:
        context_used = sum(
            int(usage.get(key) or 0)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
    selected_model_usage = model_usage.get(model) if isinstance(model_usage.get(model), dict) else None
    if selected_model_usage is None and len(model_usage) == 1:
        selected_model_usage = next(iter(model_usage.values()))
    context_capacity = None
    if isinstance(selected_model_usage, dict) and isinstance(selected_model_usage.get("contextWindow"), (int, float)):
        context_capacity = int(selected_model_usage["contextWindow"])

    return ProbeResult(
        account=account,
        provider_status=provider_status,
        state=state,
        margin=margin,
        five_hour=five_hour,
        seven_day=seven_day,
        fable=fable,
        model=model,
        context_used=context_used,
        context_capacity=context_capacity,
        latency_ms=elapsed_ms,
    )


def probe_credential(
    credential: Credential,
    model: str,
    timeout: float,
    environ: Mapping[str, str],
    include_fable: bool = False,
    claude_bin: str = "claude",
) -> ProbeResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            _probe_command(model, claude_bin),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_environment(credential, environ),
        )
    except FileNotFoundError:
        return _error_result(credential.account, "claude executable not found")
    except subprocess.TimeoutExpired:
        return _error_result(credential.account, f"probe timed out after {timeout:g}s")

    elapsed_ms = round((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        return _error_result(credential.account, f"claude exited {completed.returncode}", elapsed_ms)
    result = parse_probe(credential.account, completed.stdout, elapsed_ms, include_fable)
    if result.state == "UNKNOWN":
        return ProbeResult(**{**result.__dict__, "error": "rate-limit windows unavailable"})
    return result


def probe_credentials(
    credentials: list[Credential],
    model: str,
    timeout: float,
    environ: Mapping[str, str],
    include_fable: bool = False,
    claude_bin: str = "claude",
) -> list[ProbeResult]:
    worker = partial(
        probe_credential,
        model=model,
        timeout=timeout,
        environ=environ,
        include_fable=include_fable,
        claude_bin=claude_bin,
    )
    with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(credentials))) as executor:
        return list(executor.map(worker, credentials))


def _redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def probe_account_metadata(
    credential: Credential,
    *,
    model: str,
    timeout: float,
    environ: Mapping[str, str],
    claude_bin: str,
    secrets: list[str],
) -> dict:
    command = _probe_command(model, claude_bin, include_partial=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_environment(credential, environ),
        )
        stdout = completed.stdout
        stderr = completed.stderr
        return_code = completed.returncode
        error = ""
    except FileNotFoundError:
        stdout, stderr, return_code, error = "", "", None, "claude executable not found"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return_code, error = None, f"probe timed out after {timeout:g}s"

    safe_stdout = _redact(stdout, secrets)
    events = []
    non_json = []
    for line in safe_stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            non_json.append(line)
    return {
        "account": credential.account,
        "env_var": credential.env_name,
        "probe_model": model,
        "return_code": return_code,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "error": error,
        "events": events,
        "non_json_stdout": non_json,
        "stderr": _redact(stderr, secrets),
        "probe_command": command,
    }


def collect_account_metadata(
    credentials: list[Credential],
    *,
    include_fable: bool = False,
    timeout: float = 60,
    environ: Mapping[str, str] | None = None,
    claude_bin: str = "claude",
) -> dict:
    active_env = os.environ if environ is None else environ
    model = "fable" if include_fable else "haiku"
    secrets = [credential.token for credential in credentials]
    inherited = active_env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if inherited:
        secrets.append(inherited)
    worker = partial(
        probe_account_metadata,
        model=model,
        timeout=timeout,
        environ=active_env,
        claude_bin=claude_bin,
        secrets=secrets,
    )
    with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(credentials))) as executor:
        accounts = list(executor.map(worker, credentials))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "probe_model": model,
        "account_count": len(accounts),
        "accounts": accounts,
    }


def _entry_is_fresh(entry: object, now: float, include_fable: bool) -> bool:
    if not isinstance(entry, dict) or not isinstance(entry.get("observed_at"), (int, float)):
        return False
    if now - float(entry["observed_at"]) >= CACHE_TTL_SECONDS:
        return False
    result = _result_from_data(entry.get("result"))
    if result is None:
        return False
    windows = [result.five_hour, result.seven_day, *([result.fable] if include_fable else [])]
    return all(window.resets_at is None or window.resets_at > now for window in windows)


def collect_results(
    credentials: list[Credential],
    *,
    include_fable: bool = False,
    refresh: bool = False,
    timeout: float = 60,
    environ: Mapping[str, str] | None = None,
    cache_file: Path | None = None,
    claude_bin: str = "claude",
    now: float | None = None,
) -> tuple[list[ProbeResult], str]:
    active_env = os.environ if environ is None else environ
    path = cache_file or _cache_path(active_env)
    timestamp = time.time() if now is None else now
    mode = "fable" if include_fable else "normal"
    probe_model = "fable" if include_fable else "haiku"

    with _cache_lock(path):
        cache = _read_cache(path)
        modes = cache.setdefault("modes", {})
        mode_data = modes.get(mode) if isinstance(modes.get(mode), dict) else {}
        entries = mode_data.get("accounts") if isinstance(mode_data.get("accounts"), dict) else {}
        cached: dict[str, ProbeResult] = {}
        missing: list[Credential] = []

        for credential in credentials:
            entry = entries.get(credential.env_name)
            result = _result_from_data(entry.get("result")) if isinstance(entry, dict) else None
            if not refresh and result is not None and _entry_is_fresh(entry, timestamp, include_fable):
                cached[credential.env_name] = result
            else:
                missing.append(credential)

        live = (
            probe_credentials(
                missing,
                probe_model,
                timeout,
                active_env,
                include_fable,
                claude_bin,
            )
            if missing
            else []
        )
        live_by_name = {credential.env_name: result for credential, result in zip(missing, live, strict=True)}
        probed = {credential.env_name for credential in credentials}
        new_entries = {name: entry for name, entry in entries.items() if name not in probed}
        for credential in credentials:
            result = live_by_name.get(credential.env_name) or cached.get(credential.env_name)
            if result is not None and result.state not in {"ERROR", "UNKNOWN"}:
                observed_at = (
                    timestamp if credential.env_name in live_by_name else entries[credential.env_name]["observed_at"]
                )
                new_entries[credential.env_name] = {"observed_at": observed_at, "result": _result_data(result)}
        modes[mode] = {"accounts": new_entries}
        with contextlib.suppress(OSError):
            _write_cache(path, cache)

    ordered = [live_by_name.get(credential.env_name) or cached.get(credential.env_name) for credential in credentials]
    return [result for result in ordered if result is not None], "live" if missing else "cached"


def _error_result(account: str, error: str, latency_ms: int | None = None) -> ProbeResult:
    return ProbeResult(
        account=account,
        provider_status="error",
        state="ERROR",
        margin=None,
        five_hour=QuotaWindow(),
        seven_day=QuotaWindow(),
        latency_ms=latency_ms,
        error=error,
    )


def _metric(value: float | None) -> float:
    return -1.0 if value is None else value


def is_routable(result: ProbeResult) -> bool:
    return result.margin is not None and result.margin >= MIN_ROUTING_LEFT


def rank_results(results: list[ProbeResult], include_fable: bool = False) -> list[ProbeResult]:
    return sorted(
        results,
        key=lambda result: (
            result.margin is None,
            -_metric(result.margin),
            -_metric(result.fable.remaining) if include_fable else 0,
            -_metric(result.seven_day.remaining),
            -_metric(result.five_hour.remaining),
            result.account,
        ),
    )


def select_credential(
    environ: Mapping[str, str] | None = None,
    *,
    include_fable: bool = False,
    refresh: bool = False,
    timeout: float = 60,
    cache_file: Path | None = None,
    claude_bin: str = "claude",
) -> RouteDecision:
    active_env = os.environ if environ is None else environ
    credentials = discover_credentials(active_env)
    if not credentials:
        raise RoutingError(f"no non-empty {TOKEN_PREFIX}* variables found")
    results, source = collect_results(
        credentials,
        include_fable=include_fable,
        refresh=refresh,
        timeout=timeout,
        environ=active_env,
        cache_file=cache_file,
        claude_bin=claude_bin,
    )
    eligible = [result for result in results if is_routable(result)]
    if not eligible:
        raise RoutingError("no Claude account has verified routing capacity", results)
    winner = rank_results(eligible, include_fable)[0]
    by_account = {credential.account: credential for credential in credentials}
    return RouteDecision(by_account[winner.account], winner, source)


def format_selection(decision: RouteDecision, include_fable: bool = False) -> str:
    result = decision.result
    parts = [
        f"account={result.account}",
        f"routing_left={_percent(result.routing_left)}",
        f"5h_left={_percent(result.five_hour.remaining)}",
        f"7d_left={_percent(result.seven_day.remaining)}",
    ]
    if include_fable:
        parts.append(f"fable_left={_percent(result.fable.remaining)}")
    parts.append(f"source={decision.source}")
    return "[agenti] " + " ".join(parts)


def requested_model(extra_args: list[str], settings_path: Path | None = None) -> str:
    for index, value in enumerate(extra_args):
        if value == "--model" and index + 1 < len(extra_args):
            return extra_args[index + 1]
        if value.startswith("--model="):
            return value.split("=", 1)[1]
    path = settings_path or Path.home() / ".claude" / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        return str(settings.get("model") or "") if isinstance(settings, dict) else ""
    except (OSError, json.JSONDecodeError):
        return ""


def route_requires_fable(extra_args: list[str], settings_path: Path | None = None) -> bool:
    return "fable" in requested_model(extra_args, settings_path).lower()


def _percent(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"


def _duration(seconds: int | None, now: int) -> str:
    if seconds is None:
        return "?"
    return _span(max(0, seconds - now))


def _span(remaining: int) -> str:
    if remaining < 3600:
        return f"{remaining // 60}m"
    if remaining < 86400:
        hours, remainder = divmod(remaining, 3600)
        minutes = remainder // 60
        return f"{hours}h{minutes:02d}m"
    days, remainder = divmod(remaining, 86400)
    hours = remainder // 3600
    return f"{days}d{hours:02d}h"


def render_table(
    results: list[ProbeResult],
    now: int | None = None,
    include_fable: bool = False,
    current: str = "",
    observed: Mapping[str, float] | None = None,
) -> str:
    timestamp = int(time.time()) if now is None else now
    headers = [
        "#",
        "ACCOUNT",
        "STATE",
        "ROUTING LEFT",
        "5H LEFT",
        "5H RESET",
        "7D LEFT",
        "7D RESET",
    ]
    if include_fable:
        headers.extend(["FABLE LEFT", "FABLE RESET"])
    if observed is not None:
        headers.append("AGE")
    rows = []
    for rank, result in enumerate(rank_results(results, include_fable), 1):
        row = [
            str(rank),
            f"{result.account} (current)" if current and result.account == current else result.account,
            result.state,
            _percent(result.margin),
            _percent(result.five_hour.remaining),
            _duration(result.five_hour.resets_at, timestamp),
            _percent(result.seven_day.remaining),
            _duration(result.seven_day.resets_at, timestamp),
        ]
        if include_fable:
            row.extend([_percent(result.fable.remaining), _duration(result.fable.resets_at, timestamp)])
        if observed is not None:
            seen = observed.get(result.account)
            row.append("?" if seen is None else _span(max(0, timestamp - int(seen))))
        rows.append(row)
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) for index in range(len(headers))]
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    errors = [f"{result.account}: {result.error}" for result in rank_results(results, include_fable) if result.error]
    if errors:
        lines.extend(["", *errors])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rank Claude Code OAuth accounts by remaining routing capacity")
    parser.add_argument("--dry-run", action="store_true", help="probe and rank accounts without launching workload")
    parser.add_argument(
        "--show-account-metadata",
        metavar="SLUG",
        default="",
        help="print every JSON event returned by a fresh probe for AH_CC_TOKEN_<SLUG>",
    )
    parser.add_argument("--fable", action="store_true", help="probe and include the separate Fable weekly quota")
    parser.add_argument("--refresh", action="store_true", help="ignore the 60-second quota cache")
    parser.add_argument("--timeout", type=float, default=60, help="per-account probe timeout in seconds")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.dry_run and not args.show_account_metadata:
        print("claude-quota-balancer: use --dry-run or --show-account-metadata", file=sys.stderr)
        return 2
    started = time.monotonic()
    credentials = discover_credentials(os.environ)
    if not credentials:
        print(f"claude-quota-balancer: no non-empty {TOKEN_PREFIX}* variables found", file=sys.stderr)
        return 2
    if args.show_account_metadata:
        try:
            credential = credential_for_slug(credentials, args.show_account_metadata)
        except RoutingError as exc:
            print(f"claude-quota-balancer: {exc}", file=sys.stderr)
            return 2
        metadata = collect_account_metadata(
            [credential],
            include_fable=args.fable,
            timeout=args.timeout,
        )
        print(json.dumps(metadata, indent=2, sort_keys=True))
        return 0 if all(account["return_code"] == 0 for account in metadata["accounts"]) else 1
    workers = min(MAX_PROBE_WORKERS, len(credentials))
    results, source = collect_results(
        credentials,
        include_fable=args.fable,
        refresh=args.refresh,
        timeout=args.timeout,
    )
    print(render_table(results, include_fable=args.fable))
    print(
        f"\nDry-run execution time: {time.monotonic() - started:.2f}s "
        f"(accounts={len(credentials)}, max_workers={workers}, source={source})"
    )
    return 0 if any(is_routable(result) for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
