"""Project bridge — a Claude-shaped repo's own context, for a non-claude target.

Claude Code loads one instruction attachment at SessionStart carrying every
`<repo>/.claude/rules/*.md` body plus the project memory (measured in a real
tcc-qitp session: 37 files, 252,291 bytes). Codex has no rules directory and no
project-memory channel, so a session opened in the same repo carried the global
persona and nothing project-specific.

The repo's own CLAUDE.md reaches codex through `project_doc_fallback_filenames`
(profiles/_base/config.base.toml) and is deliberately NOT re-injected here.
What this module adds is what no config key can reach: the rule bodies, the
project memory, and a repo-level skills root codex will scan.

Target-neutral by construction — it reads a Claude-shaped repo and emits through
``inject_context``. Only the call site knows which target is running.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from hooks.common import inject_context, log

_BANNER_HEADER = "RULES IMPORTANT!! (HARD FLOOR) !!"
_TRAILER = "\n[TRUNCATED — read the remaining rule files directly from .claude/rules/]\n"
_MAX_WALK_UP = 10


def _repo_root(cwd: str) -> Path | None:
    """The repo root for *cwd*, or None when there is no Claude-shaped project."""
    if not cwd or not os.path.isdir(cwd):
        return None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            root = Path(out.stdout.strip())
            if (root / ".claude").is_dir():
                return root
    except (OSError, subprocess.SubprocessError):
        pass

    current = Path(cwd)
    for _ in range(_MAX_WALK_UP):
        if (current / ".claude").is_dir():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def _git_dir(root: Path) -> Path | None:
    """The COMMON git dir — a linked worktree's own gitdir has no info/exclude."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    path = Path(out.stdout.strip())
    if not path.is_absolute():
        path = (root / path).resolve()
    return path if path.is_dir() else None


def _exclude_locally(root: Path, entry: str) -> None:
    """Add *entry* to the repo's local excludes — never .gitignore, which is committed."""
    git_dir = _git_dir(root)
    if git_dir is None:
        return
    exclude = git_dir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        if entry in existing.split("\n"):
            return
        with exclude.open("a") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"{entry}\n")
        log("project_bridge: local exclude added", {"repo": str(root), "entry": entry})
    except OSError as e:
        log("project_bridge: exclude write failed", {"repo": str(root), "error": str(e)})


def ensure_skills_root(root: Path) -> None:
    """Expose `<repo>/.claude/skills` at `<repo>/.agents/skills`, codex's repo skill root."""
    source = root / ".claude" / "skills"
    if not source.is_dir():
        return
    link = root / ".agents" / "skills"
    target = Path("..") / ".claude" / "skills"
    try:
        if link.is_symlink():
            if os.readlink(link) == str(target):
                return
            link.unlink()
        elif link.exists():
            log("project_bridge: skills root is not a symlink — left alone", {"path": str(link)})
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
        log("project_bridge: skills root linked", {"link": str(link)})
    except OSError as e:
        log("project_bridge: skills link failed", {"link": str(link), "error": str(e)})
        return
    _exclude_locally(root, "/.agents/")


def _memory_file(root: Path) -> Path | None:
    from hooks.context.broadcast import encode_cwd

    path = Path.home() / ".claude" / "projects" / encode_cwd(str(root)) / "memory" / "MEMORY.md"
    return path if path.is_file() else None


def build_banner(root: Path, budget: int = 0) -> str:
    """The rule bodies and project memory, or "" when the repo carries neither."""
    parts: list[str] = []
    rules_dir = root / ".claude" / "rules"
    if rules_dir.is_dir():
        for rule in sorted(rules_dir.glob("*.md")):
            if rule.name == "README.md":
                continue
            try:
                parts.append(f"--- .claude/rules/{rule.name} ---\n{rule.read_text().strip()}")
            except OSError:
                continue
    memory = _memory_file(root)
    if memory:
        try:
            parts.append(f"--- project memory ---\n{memory.read_text().strip()}")
        except OSError:
            pass
    if not parts:
        return ""

    body = "\n\n".join(parts)
    banner = f"{_BANNER_HEADER}\n\n{body}\n"
    if budget > 0 and len(banner.encode()) > budget:
        head = f"{_BANNER_HEADER}\n\n"
        room = max(0, budget - len((head + _TRAILER).encode()))
        banner = head + body.encode()[:room].decode("utf-8", errors="ignore") + _TRAILER
    return banner


def inject_project_context(cwd: str) -> None:
    """Bridge the Claude-shaped repo at *cwd* into this session's context."""
    from hooks.config import PROJECT_BRIDGE_MAX_BYTES

    root = _repo_root(cwd)
    if root is None:
        return
    ensure_skills_root(root)
    banner = build_banner(root, PROJECT_BRIDGE_MAX_BYTES)
    if not banner:
        return
    inject_context(banner, also_log=False, skip_compression=True)
    log("project_bridge: injected", {"repo": str(root), "bytes": len(banner.encode())})
