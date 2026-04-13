"""Session registry CLI — list and reopen recent Claude Code sessions.

Registry data lives in ~/.agentihooks/active-sessions.json, written by
hooks/context/broadcast.py (register_session, mark_session_closed,
heartbeat_sessions). The sync daemon ticks every 60s to update last_seen,
flip crashed PIDs to status=dead, and prune entries older than 24h.

This module reads the registry for `agentihooks sessions list` and shells
out to Windows Terminal (wt.exe) for `agentihooks sessions reopen`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hooks.context.broadcast import (
    _load_sessions,
    _save_sessions,
    derive_session_title,
    heartbeat_sessions,
)

_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _humanize_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _shorten_cwd(cwd: str, width: int = 40) -> str:
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    if len(cwd) <= width:
        return cwd
    return "…" + cwd[-(width - 1):]


def _status_color(status: str) -> str:
    return {
        "alive": _GREEN,
        "closed": _CYAN,
        "dead": _RED,
    }.get(status, _DIM)


def list_sessions(max_age_hours: int = 24, refresh: bool = True) -> list[dict]:
    """Return a list of session entries sorted by last_seen descending.

    If refresh=True, runs heartbeat_sessions() first so the list reflects
    current PID liveness even if the daemon hasn't ticked recently.
    """
    if refresh:
        try:
            reconcile_live_sessions()
        except Exception:
            pass
        try:
            heartbeat_sessions()
        except Exception:
            pass

    sessions = _load_sessions()
    now = datetime.now(timezone.utc)
    cutoff = max_age_hours * 3600
    rows: list[dict] = []
    for sid, info in sessions.items():
        ts_str = info.get("last_seen") or info.get("started_at", "")
        try:
            ts = _parse_iso(ts_str)
        except ValueError:
            continue
        age = int((now - ts).total_seconds())
        if age > cutoff:
            continue
        rows.append(
            {
                "session_id": sid,
                "status": info.get("status", "alive"),
                "cwd": info.get("cwd", ""),
                "pid": info.get("pid", 0),
                "model": info.get("model", ""),
                "started_at": info.get("started_at", ""),
                "last_seen": ts_str,
                "age_seconds": age,
                "title": derive_session_title(sid, info.get("cwd", "")),
            }
        )
    rows.sort(key=lambda r: r["age_seconds"])
    return rows


def cmd_list(args) -> int:
    rows = list_sessions(max_age_hours=getattr(args, "hours", 24))
    if not rows:
        print(f"{_DIM}No sessions in the last 24h.{_RESET}")
        return 0

    print(
        f"{_BOLD}{'IDX':<4}{'ID':<12}{'STATUS':<9}{'AGE':<6}"
        f"{'CWD':<42}TITLE{_RESET}"
    )
    for i, r in enumerate(rows, 1):
        sid_short = r["session_id"][:8] + ".."
        status = r["status"]
        color = _status_color(status)
        age = _humanize_age(r["age_seconds"])
        cwd = _shorten_cwd(r["cwd"], 40)
        title = r["title"][:50]
        print(
            f"{i:<4}{sid_short:<12}{color}{status:<9}{_RESET}"
            f"{age:<6}{cwd:<42}{_DIM}{title}{_RESET}"
        )
    print()
    print(
        f"{_DIM}Reopen with: "
        f"agentihooks sessions reopen [N]  (N = how many, default all dead/closed){_RESET}"
    )
    return 0


def _detect_terminal() -> str:
    """Return one of: 'wt', 'bare'.

    'wt' = Windows Terminal available (via wt.exe on WSL or native Windows).
    'bare' = fallback, prints commands for manual execution.
    """
    if os.environ.get("WT_SESSION"):
        return "wt"
    if shutil.which("wt.exe"):
        return "wt"
    return "bare"


def _build_wt_cmd(entry: dict) -> list[str]:
    """Build a `wt.exe new-tab` argv for WSL2.

    Uses $WSL_DISTRO_NAME if set, otherwise lets wsl.exe pick the default.
    """
    sid = entry["session_id"]
    cwd = entry["cwd"]
    short = sid[:8]
    distro = os.environ.get("WSL_DISTRO_NAME", "")

    # Inner bash command: cd into cwd, resume the session, keep shell open on exit.
    inner = f"cd {_shell_quote(cwd)} && agenti --resume {sid}; exec bash"

    wsl_args = ["wsl.exe"]
    if distro:
        wsl_args += ["-d", distro]
    wsl_args += ["--", "bash", "-c", inner]

    return [
        "wt.exe",
        "-w",
        "0",
        "new-tab",
        "--title",
        f"ag:{short}",
    ] + wsl_args


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def reopen_sessions(count: int | None = None, include_alive: bool = False) -> int:
    """Reopen dead/closed sessions in new Windows Terminal tabs.

    Args:
        count: max number of sessions to reopen (most-recent first). None = all.
        include_alive: also reopen alive entries (normally skipped).

    Returns number of sessions successfully launched.
    """
    rows = list_sessions()
    if not include_alive:
        rows = [r for r in rows if r["status"] in ("dead", "closed")]
    if count is not None:
        rows = rows[:count]

    if not rows:
        print(f"{_DIM}No dead/closed sessions to reopen.{_RESET}")
        return 0

    terminal = _detect_terminal()
    launched = 0

    for r in rows:
        cmd = _build_wt_cmd(r)
        sid_short = r["session_id"][:8]
        cwd_short = _shorten_cwd(r["cwd"], 35)
        if terminal == "wt":
            try:
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                print(f"{_GREEN}✓{_RESET} launched tab {sid_short} — {cwd_short}")
                launched += 1
                time.sleep(0.2)
            except OSError as e:
                print(f"{_RED}✗{_RESET} {sid_short}: {e}", file=sys.stderr)
        else:
            print(
                f"{_YELLOW}manual:{_RESET} cd {_shell_quote(r['cwd'])} && "
                f"agenti --resume {r['session_id']}"
            )

    if terminal != "wt":
        print(
            f"\n{_DIM}wt.exe not found. Above commands listed for manual execution.{_RESET}"
        )
    return launched


def cmd_reopen(args) -> int:
    count = args.count if getattr(args, "count", None) else None
    launched = reopen_sessions(count=count)
    return 0 if launched >= 0 else 1



def backfill_from_transcripts(max_age_hours: int = 24) -> dict:
    """Scan ~/.claude/projects/*/*.jsonl for transcripts modified in the last
    `max_age_hours`. Register any session UUID not already in the registry
    with status="dead" (unknown liveness without a tracked PID). The cwd is
    decoded from the encoded directory name (-home-x-y -> /home/x/y).

    Returns a summary dict: {added, skipped_existing, scanned, errors}.
    """
    import time as _time

    summary = {"added": 0, "skipped_existing": 0, "scanned": 0, "errors": 0}
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.is_dir():
        return summary

    cutoff = _time.time() - (max_age_hours * 3600)
    sessions = _load_sessions()
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    changed = False

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        name = proj_dir.name
        if not name.startswith("-"):
            continue
        # Fallback cwd from dir name — lossy for real paths containing '-'.
        fallback_cwd = "/" + name[1:].replace("-", "/")

        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
                if mtime < cutoff:
                    continue
            except OSError:
                summary["errors"] += 1
                continue
            summary["scanned"] += 1
            sid = jsonl.stem  # the session UUID
            # Skip if already present AND alive — preserve live heartbeat data.
            existing = sessions.get(sid)
            if existing and existing.get("status") == "alive":
                summary["skipped_existing"] += 1
                continue
            # Authoritative cwd from the JSONL itself (line 2 user event has cwd).
            cwd = _extract_cwd_from_jsonl(jsonl) or fallback_cwd
            mtime_iso = (
                datetime.fromtimestamp(mtime, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
            entry = {
                "started_at": mtime_iso,
                "last_seen": mtime_iso,
                "status": "dead",
                "pid": 0,
                "cwd": cwd,
                "model": "",
                "backfilled": True,
            }
            # Preserve existing fields we want to keep (started_at, pid, model).
            if existing:
                entry["started_at"] = existing.get("started_at", mtime_iso)
                entry["pid"] = existing.get("pid", 0)
                entry["model"] = existing.get("model", "")
                # Don't downgrade a closed entry to dead.
                if existing.get("status") == "closed":
                    entry["status"] = "closed"
                summary["skipped_existing"] += 1
                summary["added"] -= 1  # net-zero for this sid
            sessions[sid] = entry
            summary["added"] += 1
            changed = True

    if changed:
        _save_sessions(sessions)
    # After seeding from transcripts, run reconcile to flip live ones to alive.
    reconcile_live_sessions()
    return summary


def reconcile_live_sessions() -> dict:
    """Walk running `claude` processes and mark registry entries alive.

    Heuristic:
      1. For each live claude PID, read /proc/<pid>/cwd.
      2. Find all .jsonl transcripts in ~/.claude/projects/<encoded>/ whose
         mtime is within the last 30 minutes (actively being written).
      3. Claim the most-recently-modified unclaimed JSONL per PID.
      4. Flip the matching registry entry to status=alive with the real PID.

    Returns a summary dict.
    """
    import glob as _glob
    import re as _re
    import time as _time

    summary = {"live_claude_pids": 0, "matched": 0, "unmatched_pids": 0}
    try:
        claude_pids = [
            int(pid_file.name)
            for pid_file in Path("/proc").iterdir()
            if pid_file.name.isdigit()
            and (pid_file / "comm").exists()
            and (pid_file / "comm").read_text().strip() == "claude"
        ]
    except OSError:
        return summary

    summary["live_claude_pids"] = len(claude_pids)
    if not claude_pids:
        return summary

    sessions = _load_sessions()

    # Group PIDs by their cwd.
    pids_by_cwd: dict[str, list[int]] = {}
    for pid in claude_pids:
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            continue
        pids_by_cwd.setdefault(cwd, []).append(pid)

    changed = False
    for cwd, pids in pids_by_cwd.items():
        projects_dir = Path.home() / ".claude" / "projects" / cwd.replace("/", "-")
        if not projects_dir.is_dir():
            summary["unmatched_pids"] += len(pids)
            continue

        # All JSONLs in this project, newest-mtime first. No write-recency
        # filter: an idle session still has a live PID.
        jsonls = sorted(
            ((f.stat().st_mtime, f) for f in projects_dir.glob("*.jsonl")),
            reverse=True,
        )

        # Match newest PID (highest pid = most-recently-started) to newest JSONL.
        pids_sorted = sorted(pids, reverse=True)
        for pid, (_mtime, jsonl) in zip(pids_sorted, jsonls):
            sid = jsonl.stem
            entry = sessions.get(sid)
            now_iso = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )
            if entry is None:
                entry = {
                    "started_at": now_iso,
                    "cwd": cwd,
                    "model": "",
                    "backfilled": True,
                }
            entry["status"] = "alive"
            entry["pid"] = pid
            entry["last_seen"] = now_iso
            entry["cwd"] = cwd
            sessions[sid] = entry
            summary["matched"] += 1
            changed = True

        if len(pids_sorted) > len(jsonls):
            summary["unmatched_pids"] += len(pids_sorted) - len(jsonls)

    if changed:
        _save_sessions(sessions)
    return summary


def cmd_reconcile(args) -> int:
    summary = reconcile_live_sessions()
    print(
        f"{_GREEN}Reconciled{_RESET} claude_pids={summary['live_claude_pids']} "
        f"matched={summary['matched']} unmatched_pids={summary['unmatched_pids']}"
    )
    return 0


def _extract_cwd_from_jsonl(path: Path) -> str:
    """Read the first few lines of a Claude Code transcript and return the
    cwd field if present (line 2 is typically a user event with cwd)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 10:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("cwd"):
                    return obj["cwd"]
    except OSError:
        return ""
    return ""


def cmd_backfill(args) -> int:
    hours = getattr(args, "hours", 24)
    summary = backfill_from_transcripts(max_age_hours=hours)
    print(
        f"{_GREEN}Backfilled{_RESET} added={summary['added']} "
        f"skipped={summary['skipped_existing']} scanned={summary['scanned']} "
        f"errors={summary['errors']}"
    )
    return 0
