"""Each Codex account's own sessions folder: hard links to the rollouts its sessions wrote under CODEX_HOME/sessions."""

import glob
import os
from collections.abc import Mapping
from pathlib import Path

from scripts.codex_quota import codex_home, rollout_session_id

ACCOUNTS = "accounts"
SESSIONS = "sessions"
ROLLOUTS = "*/*/*/rollout-*.jsonl"


def folder(environ: Mapping[str, str], account: str) -> Path:
    return codex_home(dict(environ)) / ACCOUNTS / account / SESSIONS


def session_ids(environ: Mapping[str, str], account: str) -> set[str]:
    return {rollout_session_id(path) for path in folder(environ, account).glob(ROLLOUTS)}


def link(environ: Mapping[str, str], account: str, rollout: Path) -> Path | None:
    """Links one rollout into the account's folder at the same relative path; None when it lies outside the home."""
    base = codex_home(dict(environ)) / SESSIONS
    try:
        target = folder(environ, account) / rollout.resolve().relative_to(base.resolve())
        if target.exists() and target.samefile(rollout):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f"{target.name}.link")
        staged.unlink(missing_ok=True)
        os.link(rollout, staged)
        os.replace(staged, target)
    except (OSError, ValueError):
        return None
    return target


def link_thread(environ: Mapping[str, str], account: str, thread: str) -> Path | None:
    found = sorted((codex_home(dict(environ)) / SESSIONS).glob(f"*/*/*/rollout-*{glob.escape(thread)}.jsonl"))
    return link(environ, account, found[-1]) if found else None


def link_current(transcript_path: str) -> Path | None:
    from hooks.context.account_sessions import agent_pid, session_account

    return link(os.environ, session_account(agent_pid()), Path(transcript_path)) if transcript_path else None
