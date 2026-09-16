#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial

TOKEN_PREFIX = "AH_CC_TOKEN_"
MAX_PROBE_WORKERS = 3


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
    model: str = "?"
    context_used: int | None = None
    context_capacity: int | None = None
    latency_ms: int | None = None
    error: str = ""


def discover_credentials(environ: Mapping[str, str]) -> list[Credential]:
    return [
        Credential(name, environ[name])
        for name in sorted(environ)
        if name.startswith(TOKEN_PREFIX) and name != TOKEN_PREFIX and environ[name]
    ]


def _probe_command(model: str) -> list[str]:
    return [
        "claude",
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
        "0.10",
    ]


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


def _state(provider_status: str, five_hour: QuotaWindow, seven_day: QuotaWindow) -> tuple[str, float | None]:
    if provider_status == "rejected":
        return "BLOCKED", 0.0
    usages = [window.used for window in (five_hour, seven_day) if window.used is not None]
    if len(usages) != 2:
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


def parse_probe(account: str, output: str, elapsed_ms: int) -> ProbeResult:
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
    provider_status = str(rate_info.get("status") or "unknown")
    state, margin = _state(provider_status, five_hour, seven_day)
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
) -> ProbeResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            _probe_command(model),
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
    result = parse_probe(credential.account, completed.stdout, elapsed_ms)
    if result.state == "UNKNOWN":
        return ProbeResult(**{**result.__dict__, "error": "rate-limit windows unavailable"})
    return result


def probe_credentials(
    credentials: list[Credential],
    model: str,
    timeout: float,
    environ: Mapping[str, str],
) -> list[ProbeResult]:
    worker = partial(probe_credential, model=model, timeout=timeout, environ=environ)
    with ThreadPoolExecutor(max_workers=min(MAX_PROBE_WORKERS, len(credentials))) as executor:
        return list(executor.map(worker, credentials))


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


def rank_results(results: list[ProbeResult]) -> list[ProbeResult]:
    return sorted(
        results, key=lambda result: (result.margin is not None, result.margin or -1, result.account), reverse=True
    )


def _percent(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"


def _usage(window: QuotaWindow) -> str:
    return f"{_percent(window.used)}/{_percent(window.remaining)}"


def _duration(seconds: int | None, now: int) -> str:
    if seconds is None:
        return "?"
    remaining = max(0, seconds - now)
    if remaining < 3600:
        return f"{remaining // 60}m"
    if remaining < 86400:
        hours, remainder = divmod(remaining, 3600)
        minutes = remainder // 60
        return f"{hours}h{minutes:02d}m"
    days, remainder = divmod(remaining, 86400)
    hours = remainder // 3600
    return f"{days}d{hours:02d}h"


def _context(result: ProbeResult) -> str:
    if result.context_used is None or result.context_capacity is None:
        return "?"
    percentage = result.context_used / result.context_capacity * 100 if result.context_capacity else 0
    return f"{result.context_used}/{result.context_capacity} ({percentage:.0f}%)"


def render_table(results: list[ProbeResult], now: int | None = None) -> str:
    timestamp = int(time.time()) if now is None else now
    headers = [
        "#",
        "ACCOUNT",
        "STATE",
        "MARGIN",
        "5H USED/LEFT",
        "5H RESET",
        "7D USED/LEFT",
        "7D RESET",
        "PROBE CTX",
        "MODEL",
        "LATENCY",
    ]
    rows = []
    for rank, result in enumerate(rank_results(results), 1):
        rows.append(
            [
                str(rank),
                result.account,
                result.state,
                _percent(result.margin),
                _usage(result.five_hour),
                _duration(result.five_hour.resets_at, timestamp),
                _usage(result.seven_day),
                _duration(result.seven_day.resets_at, timestamp),
                _context(result),
                result.model,
                "?" if result.latency_ms is None else f"{result.latency_ms}ms",
            ]
        )
    widths = [max(len(headers[index]), *(len(row[index]) for row in rows)) for index in range(len(headers))]
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    errors = [f"{result.account}: {result.error}" for result in rank_results(results) if result.error]
    if errors:
        lines.extend(["", *errors])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rank Claude Code OAuth accounts by usable quota margin")
    parser.add_argument("--dry-run", action="store_true", help="probe and rank accounts without launching workload")
    parser.add_argument("--model", default="haiku", help="model used for the quota probe")
    parser.add_argument("--timeout", type=float, default=60, help="per-account probe timeout in seconds")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.dry_run:
        print("claude-quota-balancer: only --dry-run is implemented", file=sys.stderr)
        return 2
    started = time.monotonic()
    credentials = discover_credentials(os.environ)
    if not credentials:
        print(f"claude-quota-balancer: no non-empty {TOKEN_PREFIX}* variables found", file=sys.stderr)
        return 2
    workers = min(MAX_PROBE_WORKERS, len(credentials))
    results = probe_credentials(credentials, args.model, args.timeout, os.environ)
    print(render_table(results))
    account_label = "account" if len(credentials) == 1 else "accounts"
    worker_label = "worker" if workers == 1 else "workers"
    print(
        f"\nDry-run execution time: {time.monotonic() - started:.2f}s "
        f"({len(credentials)} {account_label}, {workers} {worker_label})"
    )
    return 0 if any(result.state not in {"ERROR", "UNKNOWN"} for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
