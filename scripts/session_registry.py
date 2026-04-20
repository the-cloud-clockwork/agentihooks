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
    encode_cwd,
    heartbeat_sessions,
)

SESSION_BUSY_WINDOW = 60  # seconds; JSONL writes newer than this = still live

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
        cwd = "~" + cwd[len(home) :]
    if len(cwd) <= width:
        return cwd
    return "…" + cwd[-(width - 1) :]


def _status_color(status: str) -> str:
    return {
        "alive": _GREEN,
        "closed": _CYAN,
        "dead": _RED,
    }.get(status, _DIM)


def list_sessions(max_age_hours: int = 24, refresh: bool = True, limit: int | None = None) -> list[dict]:
    """Return a list of session entries sorted by last_seen descending.

    If refresh=True, runs reconcile + heartbeat first so status reflects
    current PID liveness. limit caps the returned rows (most-recent first).
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
        status = info.get("status", "alive")
        # Age semantics:
        #   alive    → time since session started (session lifetime, differentiates live sessions)
        #   otherwise → time since last activity (how long ago it went inactive)
        if status == "alive":
            ts_str = info.get("started_at") or info.get("last_seen", "")
        else:
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
                "status": status,
                "cwd": info.get("cwd", ""),
                "pid": info.get("pid", 0),
                "model": info.get("model", ""),
                "started_at": info.get("started_at", ""),
                "last_seen": info.get("last_seen", ""),
                "age_seconds": age,
                "title": derive_session_title(sid, info.get("cwd", "")),
            }
        )
    # Sort: alive first (by age ascending = longest-running on top), then
    # everything else by age ascending (most-recently-active on top).
    _status_rank = {"alive": 0, "closed": 1, "dead": 2, "superseded": 3}
    rows.sort(key=lambda r: (_status_rank.get(r["status"], 9), r["age_seconds"]))
    if limit is not None and limit > 0:
        rows = rows[:limit]
    return rows


def cmd_list(args) -> int:
    limit = getattr(args, "limit", 10)
    rows = list_sessions(max_age_hours=getattr(args, "hours", 24), limit=limit)
    if not rows:
        print(f"{_DIM}No sessions in the last 24h.{_RESET}")
        return 0

    print(f"{_BOLD}{'IDX':<4}{'STATUS':<11}{'AGE':<6}{'NAME':<30}{'CWD':<36}ID{_RESET}")
    for i, r in enumerate(rows, 1):
        sid = r["session_id"]
        status = r["status"]
        color = _status_color(status)
        age = _humanize_age(r["age_seconds"])
        name = _truncate(r.get("title", "") or "(unnamed)", 28)
        cwd = _shorten_cwd(r["cwd"], 34)
        print(
            f"{i:<4}{color}{status:<11}{_RESET}{age:<6}{_CYAN}{name:<30}{_RESET}{cwd:<36}{_DIM}{sid}{_RESET}"
        )
    print()
    print(f"{_DIM}Showing {len(rows)} most recent. Reopen: agentihooks sessions reopen <N>  (N required){_RESET}")
    return 0


def _truncate(s: str, width: int) -> str:
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


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
    """Build a `wt.exe new-tab` argv that launches PowerShell, which then
    runs `wsl.exe -d <distro> bash -c 'cd CWD && agenti --resume ID'`.

    Why PowerShell as the outer shell:
      - wt.exe splits its command line on `;` (tab/pane separator), so a
        bash trailing `; exec bash` is parsed by wt as a separate command
        and fails with "system cannot find file 'exec bash'".
      - PowerShell -NoExit keeps the tab alive after the inner command
        exits, so the operator can inspect output or restart the session.
      - PowerShell is the default Windows Terminal profile on most setups,
        so this works whether or not a WSL profile is installed.
    """
    sid = entry["session_id"]
    cwd = entry["cwd"]
    short = sid[:8]
    distro = os.environ.get("WSL_DISTRO_NAME", "")

    # Inner bash command — call `agentihooks claude` directly (not the
    # bashrc `agenti` alias, which is unavailable in non-interactive shells).
    inner_bash = f"cd {_shell_quote(cwd)} && agentihooks claude --resume {sid}"

    # Build the wsl.exe call as one string that PowerShell will invoke.
    wsl_call = "wsl.exe"
    if distro:
        wsl_call += f" -d {distro}"
    wsl_call += f" -- bash -lc {_powershell_quote(inner_bash)}"

    # PowerShell command: run the wsl call, stay open after it exits.
    ps_cmd = f"& {{ {wsl_call} }}"

    return [
        "wt.exe",
        "-w",
        "0",
        "new-tab",
        "--title",
        f"ag:{short}",
        "powershell.exe",
        "-NoExit",
        "-NoProfile",
        "-Command",
        ps_cmd,
    ]


def _powershell_quote(s: str) -> str:
    """Quote a string for safe embedding inside a PowerShell single-quoted literal."""
    return "'" + s.replace("'", "''") + "'"


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _jsonl_recently_written(row: dict) -> bool:
    """Return True if the session's transcript JSONL was modified within
    SESSION_BUSY_WINDOW seconds — signal that another claude process is
    still actively writing to it, and reopening would fork a duplicate.
    """
    import time as _time

    sid = row.get("session_id", "")
    cwd = row.get("cwd", "")
    if not sid or not cwd:
        return False
    transcript = Path.home() / ".claude" / "projects" / encode_cwd(cwd) / f"{sid}.jsonl"
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        return False
    return (_time.time() - mtime) < SESSION_BUSY_WINDOW


def reopen_sessions(indices: list[int], include_alive: bool = False) -> int:
    """Reopen specific sessions (by 1-based IDX from the same list that
    `sessions list` would show) in new Windows Terminal tabs.

    Args:
        indices: 1-based row numbers from `sessions list`. Out-of-range
            entries are skipped with a warning. Alive rows are skipped
            unless include_alive is set.
        include_alive: also reopen alive entries (normally skipped).

    Returns number of sessions successfully launched.
    """
    all_rows = list_sessions()  # same sort order as `sessions list`
    rows: list[dict] = []
    seen: set[int] = set()
    for idx in indices:
        if idx < 1 or idx > len(all_rows):
            print(
                f"{_YELLOW}warn:{_RESET} idx {idx} out of range (1..{len(all_rows)})",
                file=sys.stderr,
            )
            continue
        if idx in seen:
            continue
        seen.add(idx)
        row = all_rows[idx - 1]
        if not include_alive and row["status"] == "alive":
            print(
                f"{_YELLOW}skip:{_RESET} idx {idx} ({row['session_id'][:8]}) is alive — already running",
                file=sys.stderr,
            )
            continue
        if not include_alive and _jsonl_recently_written(row):
            print(
                f"{_YELLOW}skip:{_RESET} idx {idx} ({row['session_id'][:8]}) "
                f"JSONL was written <{SESSION_BUSY_WINDOW}s ago — another "
                f"claude is likely still writing it. Use --force to override.",
                file=sys.stderr,
            )
            continue
        rows.append(row)

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
            print(f"{_YELLOW}manual:{_RESET} cd {_shell_quote(r['cwd'])} && agenti --resume {r['session_id']}")

    if terminal != "wt":
        print(f"\n{_DIM}wt.exe not found. Above commands listed for manual execution.{_RESET}")
    return launched


def cmd_reopen(args) -> int:
    raw_indices = getattr(args, "indices", None) or []
    # Allow `reopen 6,7,8` and `reopen 6 7 8` interchangeably.
    flat: list[int] = []
    for tok in raw_indices:
        for part in str(tok).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                flat.append(int(part))
            except ValueError:
                print(
                    f"{_RED}error:{_RESET} '{part}' is not an integer IDX",
                    file=sys.stderr,
                )
                return 2
    if not flat:
        print(
            f"{_RED}error:{_RESET} sessions reopen requires at least one IDX "
            f"(e.g. `sessions reopen 6` or `sessions reopen 6,7,8`). "
            f"Run `sessions list` first to see IDX numbers.",
            file=sys.stderr,
        )
        return 2
    launched = reopen_sessions(indices=flat)
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
            mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
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

    summary = {"live_claude_pids": 0, "matched": 0, "unmatched_pids": 0}

    def _pid_state(pid: int) -> str:
        """Return the state char from /proc/<pid>/stat (R/S/D/T/t/Z/I/...)."""
        try:
            stat = (Path("/proc") / str(pid) / "stat").read_text()
            # format: pid (comm) state ppid ... — comm may contain ")" so split on last one
            return stat.rsplit(")", 1)[1].split()[0]
        except (OSError, IndexError):
            return ""

    try:
        raw_pids = [
            int(pid_file.name)
            for pid_file in Path("/proc").iterdir()
            if pid_file.name.isdigit()
            and (pid_file / "comm").exists()
            and (pid_file / "comm").read_text().strip() == "claude"
        ]
    except OSError:
        return summary

    # Include any claude process that is NOT a zombie. State T (stopped via
    # Ctrl+Z or job-control), state D (uninterruptible sleep), and the
    # typical S/R are all live processes with valid /proc state; only Z
    # (zombie) means the process no longer has a live image. We do NOT
    # filter by parent comm or dedupe by ppid — WSL2 can re-parent live
    # claude processes to init/Relay when VSCode detaches a terminal,
    # and multiple claude children can legitimately share a single
    # interactive bash parent.
    claude_pids = [pid for pid in raw_pids if _pid_state(pid) != "Z"]

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
        projects_dir = Path.home() / ".claude" / "projects" / encode_cwd(cwd)
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
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
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
