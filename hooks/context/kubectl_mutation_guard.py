"""kubectl_mutation_guard.py — block live-system state mutation at the tool boundary.

Doctrine: code is the source of truth. Behavior changes go through code →
commit → push → CI → deploy. kubectl exec / kubectl cp / kubectl edit /
kubectl patch / kubectl set / SSH-edit / docker-exec writes / helm install
outside CI / argocd app sync --local are forbidden. This hook denies the
patterns at PreToolUse for Bash with exit code 2.

This is a HARD FLOOR. The hook does not honor bypass mode, hotfix signals, or
release-gate signals. To change a running system's behavior, change code.

References:
- agentihooks-bundle/profiles/*/.claude/rules/code-is-source.md
- documents/anton/ANTON-CORE-CI-MANIFESTO.md §3.5
"""

from __future__ import annotations

import re
import shlex
from typing import Optional

# ---------------------------------------------------------------------------
# Whole-command regex patterns — match the full Bash command string
# ---------------------------------------------------------------------------

_RE_FLAGS = re.IGNORECASE | re.DOTALL

# Each entry: (pattern, short rule key, human description)
_WHOLE_CMD_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # kubectl edit / patch / set / scale
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?edit\b", _RE_FLAGS),
     "kubectl-edit",
     "kubectl edit (live state mutation — change the manifest in code, push, let CI/ArgoCD apply)"),
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?patch\b", _RE_FLAGS),
     "kubectl-patch",
     "kubectl patch (live state mutation — change the manifest in code, push, let CI/ArgoCD apply)"),
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?set\s+(?:env|image|resources?|serviceaccount|selector|subject)\b", _RE_FLAGS),
     "kubectl-set",
     "kubectl set <subcommand> (live state mutation — put it in the manifest)"),
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?scale\b", _RE_FLAGS),
     "kubectl-scale",
     "kubectl scale (replica change — put it in the manifest, push, redeploy)"),

    # kubectl rollout restart/pause/resume
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?rollout\s+(?:restart|pause|resume)\b", _RE_FLAGS),
     "kubectl-rollout-mutate",
     "kubectl rollout restart/pause/resume (forced restart of unchanged image; image changes go through code → CI → ArgoCD)"),

    # kubectl annotate / label
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?(?:annotate|label)\b", _RE_FLAGS),
     "kubectl-meta-mutate",
     "kubectl annotate/label (state mutation — put metadata in the manifest)"),

    # kubectl drain / cordon / uncordon / taint
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?(?:drain|cordon|uncordon|taint)\b", _RE_FLAGS),
     "kubectl-node-mutate",
     "kubectl drain/cordon/uncordon/taint (node state mutation)"),

    # kubectl debug
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?debug\b", _RE_FLAGS),
     "kubectl-debug",
     "kubectl debug (creates ephemeral container — use logs/describe/read-only exec for diagnostics)"),

    # kubectl autoscale
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?autoscale\b", _RE_FLAGS),
     "kubectl-autoscale",
     "kubectl autoscale (HPA goes in the manifest)"),

    # kubectl create / apply / replace -f (ad-hoc manifest application)
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?(?:create|apply|replace)\s+(?:-f|--filename)\b", _RE_FLAGS),
     "kubectl-apply-f",
     "kubectl create/apply/replace -f (ad-hoc manifest application — manifests reach the cluster via git + ArgoCD)"),

    # kubectl create <kind> ... (without -f) — block all forms
    (re.compile(r"\bkubectl\s+(?:[^\n;|&]*?\s)?create\s+(?!-f|--filename)\S", _RE_FLAGS),
     "kubectl-create",
     "kubectl create <kind> (ad-hoc resource creation — go through manifest + git)"),

    # helm install / upgrade / rollback (deploys are git-driven via ArgoCD)
    (re.compile(r"\bhelm\s+(?:[^\n;|&]*?\s)?(?:install|upgrade|rollback)\b", _RE_FLAGS),
     "helm-deploy",
     "helm install/upgrade/rollback (deploys go through git + ArgoCD)"),

    # argocd app sync --local
    (re.compile(r"\bargocd\s+app\s+sync\b[^\n;|&]*?--local\b", _RE_FLAGS),
     "argocd-sync-local",
     "argocd app sync --local (bypasses git as source of truth)"),
]

# ---------------------------------------------------------------------------
# kubectl exec mutation patterns — match what comes AFTER the `--` separator
# ---------------------------------------------------------------------------

# Capture the kubectl exec command and what's after `--`. Command-position
# anchored so `kubectl exec` mid-string in a quoted commit body / echo arg
# does not falsely trigger.
_KUBECTL_EXEC_RE = re.compile(
    r"(?:^|[;&|]\s*|\n\s*|`|\$\(\s*)"
    r"\bkubectl\s+(?:[^\n;|&]*?\s)?exec\b([^\n;|&]*?)--\s+(.+?)(?=\s*(?:[;|&\n]|$))",
    _RE_FLAGS,
)

# Things that, immediately after --, indicate a write/install inside the pod.
_EXEC_MUTATION_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^\s*tee\b", _RE_FLAGS),
     "exec-tee",
     "kubectl exec ... -- tee (writes a file inside the pod)"),
    (re.compile(r"^\s*sed\s+-i\b", _RE_FLAGS),
     "exec-sed-i",
     "kubectl exec ... -- sed -i (edits a file inside the pod)"),
    (re.compile(r"^\s*(?:vi|vim|nano|emacs)\b", _RE_FLAGS),
     "exec-editor",
     "kubectl exec ... -- vi/vim/nano/emacs (interactive file edit inside the pod)"),
    (re.compile(r"^\s*(?:apt(?:-get)?|pip3?|npm|yarn|cargo|gem|apk|brew|dnf|yum|pacman)\s+(?:install|add|-S|-Sy)\b", _RE_FLAGS),
     "exec-pkg-install",
     "kubectl exec ... -- <pkg-mgr> install (in-pod install — bake it into the image)"),
    (re.compile(r"^\s*(?:rm|mv|cp)\s+(?:-[a-zA-Z]+\s+)*['\"]?/(?:etc|var|opt|srv|usr|root|home|tmp)\b", _RE_FLAGS),
     "exec-fs-mutate",
     "kubectl exec ... -- rm/mv/cp on filesystem paths (live mutation)"),
    (re.compile(r"^\s*(?:sh|bash|ash|zsh|dash)\s+-c\s+['\"]", _RE_FLAGS),
     "exec-shell-c",
     None),  # special handling — inspect inside the quoted shell
]

# Inside `sh -c '...'` — flag mutation patterns within the quoted command
_INNER_SHELL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r">\s*['\"]?(?:/|\$|~)", _RE_FLAGS), "redirect-to-fs-path"),
    (re.compile(r">>\s*['\"]?(?:/|\$|~)", _RE_FLAGS), "append-to-fs-path"),
    (re.compile(r"<<\s*['\"]?[A-Z_]+", _RE_FLAGS), "heredoc-to-file"),
    (re.compile(r"\btee\b", _RE_FLAGS), "tee"),
    (re.compile(r"\bsed\s+-i\b", _RE_FLAGS), "sed-i"),
    (re.compile(r"\b(?:apt(?:-get)?|pip3?|npm|yarn|cargo|gem|apk|brew|dnf|yum|pacman)\s+(?:install|add|-S|-Sy)\b", _RE_FLAGS), "pkg-install"),
    (re.compile(r"\brm\s+-r?f?\b", _RE_FLAGS), "rm-rf"),
    (re.compile(r"\bsystemctl\s+(?:start|stop|restart|reload|enable|disable)\b", _RE_FLAGS), "systemctl-mutate"),
]


# ---------------------------------------------------------------------------
# kubectl cp direction parsing — only block local → pod copies
# ---------------------------------------------------------------------------

_KUBECTL_CP_RE = re.compile(
    r"(?:^|[;&|]\s*|\n\s*|`|\$\(\s*)"
    r"\bkubectl\s+(?:[^\n;|&]*?\s)?cp\b([^\n;|&]*)",
    _RE_FLAGS,
)


def _check_kubectl_cp_into_pod(cmd_args: str) -> bool:
    """Return True if `kubectl cp` is copying INTO a pod (local → pod:path).

    Direction is determined by the order of two positional args:
      - Each arg is either `path` (local) or `[ns/]pod:path` (remote).
      - `kubectl cp <local> <pod>:<path>` — INTO pod (forbidden).
      - `kubectl cp <pod>:<path> <local>` — OUT of pod (allowed).
    """
    try:
        tokens = shlex.split(cmd_args.strip())
    except ValueError:
        # Unparseable — be conservative and block.
        return True
    # Filter out flags
    positional = [t for t in tokens if not t.startswith("-")]
    if len(positional) < 2:
        return False
    src, dst = positional[0], positional[1]
    src_is_pod = ":" in src and not src.startswith("/") and not src.startswith(".")
    dst_is_pod = ":" in dst and not dst.startswith("/") and not dst.startswith(".")
    # Local → Pod is blocked
    return (not src_is_pod) and dst_is_pod


# ---------------------------------------------------------------------------
# SSH and scp INTO host detection
# ---------------------------------------------------------------------------

# `ssh ... <remote-cmd>` where <remote-cmd> contains write patterns.
# Anchored at command position (start of input, or after a shell separator)
# to avoid false-positive matches against the word "ssh" inside text.
_CMD_POSITION_PREFIX = r"(?:^|[;&|]\s*|\n\s*|`|\$\(\s*)"

_SSH_RE = re.compile(
    _CMD_POSITION_PREFIX
    + r"\bssh\s+(?:-[a-zA-Z0-9_-]+(?:[=\s]+[^\s]+)?\s+)*[^\s]+\s+(.+)",
    _RE_FLAGS,
)

_SSH_WRITE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsed\s+-i\b", _RE_FLAGS), "ssh-sed-i"),
    (re.compile(r"\b(?:vi|vim|nano|emacs)\s+['\"]?/", _RE_FLAGS), "ssh-editor"),
    (re.compile(r">\s*['\"]?(?:/etc|/var|/opt|/srv|/usr|/root|/home|~)", _RE_FLAGS), "ssh-redirect"),
    (re.compile(r">>\s*['\"]?(?:/etc|/var|/opt|/srv|/usr|/root|/home|~)", _RE_FLAGS), "ssh-append"),
    (re.compile(r"<<\s*['\"]?[A-Z_]+", _RE_FLAGS), "ssh-heredoc"),
    (re.compile(r"\|\s*tee\s+['\"]?/", _RE_FLAGS), "ssh-pipe-tee"),
    (re.compile(r"\btee\s+['\"]?/", _RE_FLAGS), "ssh-tee"),
    (re.compile(r"\bsystemctl\s+(?:start|stop|restart|reload|enable|disable)\b", _RE_FLAGS), "ssh-systemctl-mutate"),
    (re.compile(r"\b(?:apt(?:-get)?|pip3?|npm|yarn|apk|gem)\s+(?:install|add)\b", _RE_FLAGS), "ssh-pkg-install"),
    (re.compile(r"\bdocker\s+exec\b", _RE_FLAGS), "ssh-docker-exec"),
    (re.compile(r"\bkubectl\s+(?:edit|patch|set|scale|annotate|label|drain|cordon|taint|debug|create|apply|replace|autoscale|rollout\s+(?:restart|pause|resume))\b", _RE_FLAGS), "ssh-kubectl-mutate"),
]

# scp INTO remote host: any scp where the destination contains `<host>:<path>`
# Pattern: `scp [flags] <src...> <user@host:path>` — last positional arg has
# `:` after a host segment (not a relative/absolute filesystem path).
_SCP_RE = re.compile(
    r"(?:^|[;&|]\s*|\n\s*|`|\$\(\s*)\bscp\s+(.+)",
    _RE_FLAGS,
)


def _check_scp_into_host(cmd_args: str) -> bool:
    """Return True if `scp` is copying INTO a remote host (local → host:path)."""
    try:
        tokens = shlex.split(cmd_args.strip())
    except ValueError:
        return True
    positional = [t for t in tokens if not t.startswith("-")]
    if len(positional) < 2:
        return False
    dst = positional[-1]
    # Remote path looks like `[user@]host:path` — has `:` and the part before
    # `:` is not a drive letter and not a filesystem path.
    if ":" not in dst:
        return False
    host_part, _, _ = dst.partition(":")
    if not host_part or host_part.startswith("/") or host_part.startswith("."):
        return False
    # Check at least one source is local (no colon or starts with / or .)
    for src in positional[:-1]:
        if ":" not in src or src.startswith("/") or src.startswith("."):
            return True
    return False


# ---------------------------------------------------------------------------
# docker exec mutation detection (same shape as kubectl exec)
# ---------------------------------------------------------------------------

_DOCKER_EXEC_RE = re.compile(
    r"(?:^|[;&|]\s*|\n\s*|`|\$\(\s*)"
    r"\bdocker\s+(?:[^\n;|&]*?\s)?exec\b([^\n;|&]+)",
    _RE_FLAGS,
)

_DOCKER_EXEC_MUTATION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\btee\b", _RE_FLAGS), "docker-exec-tee"),
    (re.compile(r"\bsed\s+-i\b", _RE_FLAGS), "docker-exec-sed-i"),
    (re.compile(r"\b(?:apt(?:-get)?|pip3?|npm|yarn|cargo|gem|apk|brew|dnf|yum|pacman)\s+(?:install|add|-S|-Sy)\b", _RE_FLAGS),
     "docker-exec-pkg-install"),
    (re.compile(r"(?:sh|bash|ash|zsh|dash)\s+-c\s+['\"][^'\"]*?(?:>\s*['\"]?(?:/|\$|~)|>>\s*['\"]?(?:/|\$|~)|\btee\b|\bsed\s+-i\b|<<\s*['\"]?[A-Z_]+)", _RE_FLAGS),
     "docker-exec-shell-write"),
    (re.compile(r">\s*['\"]?(?:/|\$|~)", _RE_FLAGS), "docker-exec-redirect"),
    (re.compile(r">>\s*['\"]?(?:/|\$|~)", _RE_FLAGS), "docker-exec-append"),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Module-level cache of the latest violation reason for downstream logging.
_last_violation: Optional[str] = None


def _violation(rule: str, description: str) -> str:
    """Format a structured rejection message."""
    msg = (
        "BLOCKED by kubectl_mutation_guard: live-system state mutation forbidden.\n"
        f"  Rule:        {rule}\n"
        f"  Reason:      {description}\n"
        "  Doctrine:    CI Manifesto §3.5 + rules/code-is-source.md\n"
        "  Action:      change the source code, commit, push to dev, let CI deploy.\n"
        "  Note:        this rule is HARD FLOOR — bypass mode, hotfix signals,\n"
        "               and release-gate signals do NOT unlock it. To disable\n"
        "               the hook itself for a session, set\n"
        "               KUBECTL_MUTATION_GUARD_ENABLED=false (logged)."
    )
    return msg


def _strip_quoted_strings(cmd: str) -> str:
    """Replace content inside matched single/double quotes with empty quotes,
    and elide heredoc bodies. Used to suppress text mentions inside quoted
    arguments (e.g. `git commit -m "kubectl edit ..."` is a message, not a
    kubectl invocation) for whole-command pattern matching.

    Preserves the structural shape of the outer command so command-position
    detection still works. Context-aware checks (SSH wrapper detection,
    kubectl-exec-after-dash, kubectl cp direction, scp direction, docker
    exec) continue to run on the un-stripped command since those checks
    intentionally inspect inside their respective scopes.
    """
    out: list[str] = []
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c in ("'", '"'):
            quote = c
            out.append(quote)
            i += 1
            while i < n and cmd[i] != quote:
                if cmd[i] == "\\" and quote == '"' and i + 1 < n:
                    # Skip escape sequence inside double quotes
                    i += 2
                    continue
                i += 1
            if i < n:
                out.append(quote)
                i += 1
        else:
            out.append(c)
            i += 1

    cleaned = "".join(out)
    # Elide heredoc bodies: <<EOF\n...\nEOF (and <<-EOF)
    cleaned = re.sub(
        r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\s*(?:\n|$)",
        "<<HEREDOC_STRIPPED\n",
        cleaned,
        flags=re.DOTALL | re.MULTILINE,
    )
    return cleaned


def check_kubectl_mutation_command(command: str) -> Optional[str]:
    """Return a rejection message if `command` is a forbidden mutation.

    Returns None if the command is allowed.

    Pure function — no side effects, easily testable.
    """
    if not command:
        return None

    # Strip outer quoted strings + heredoc bodies. Used for whole-command
    # patterns and cp/scp direction parsing — both are command-position
    # checks where text mentions inside quoted args (git commit -m "...",
    # echo "...", cat <<EOF...) would produce false positives.
    #
    # SSH / kubectl-exec / docker-exec detection runs on the ORIGINAL
    # command since those checks deliberately parse INSIDE the quoted
    # remote/inner command bodies.
    stripped = _strip_quoted_strings(command)

    # 1. Whole-command forbidden patterns (run on stripped — no false matches
    # against text mentions inside quoted args)
    for pattern, rule, description in _WHOLE_CMD_PATTERNS:
        if pattern.search(stripped):
            return _violation(rule, description)

    # 2. kubectl exec — inspect what's after `--` (run on ORIGINAL because
    # the after-dash content may include quoted sh -c bodies we want to
    # introspect)
    for match in _KUBECTL_EXEC_RE.finditer(command):
        after_dash = match.group(2)
        for pattern, rule, description in _EXEC_MUTATION_PATTERNS:
            if pattern.search(after_dash):
                if rule == "exec-shell-c":
                    for inner_pat, inner_rule in _INNER_SHELL_PATTERNS:
                        if inner_pat.search(after_dash):
                            return _violation(
                                f"exec-shell-c:{inner_rule}",
                                f"kubectl exec ... -- sh/bash -c with mutation pattern ({inner_rule})",
                            )
                    continue
                return _violation(rule, description or f"kubectl exec mutation ({rule})")

    # 3. kubectl cp direction (run on stripped — positional paths don't
    # carry quotes legitimately, and stripping suppresses text mentions)
    for match in _KUBECTL_CP_RE.finditer(stripped):
        if _check_kubectl_cp_into_pod(match.group(1)):
            return _violation(
                "kubectl-cp-into-pod",
                "kubectl cp <local> <pod>:<path> (copy INTO a pod — push the file via the image build)",
            )

    # 4. SSH with remote write patterns (run on ORIGINAL — the SSH remote
    # command is by definition inside quotes that the hook needs to inspect)
    for match in _SSH_RE.finditer(command):
        remote = match.group(1)
        for pattern, rule in _SSH_WRITE_PATTERNS:
            if pattern.search(remote):
                return _violation(
                    f"ssh-mutate:{rule}",
                    f"ssh <host> '<command>' with write pattern ({rule})",
                )

    # 5. scp INTO remote host (run on stripped — positional args
    # don't carry quotes legitimately)
    for match in _SCP_RE.finditer(stripped):
        if _check_scp_into_host(match.group(1)):
            return _violation(
                "scp-into-host",
                "scp <local> <host>:<path> (copy INTO host — push via git, not scp)",
            )

    # 6. docker exec mutations (run on ORIGINAL — same reason as kubectl exec)
    for match in _DOCKER_EXEC_RE.finditer(command):
        body = match.group(1)
        for pattern, rule in _DOCKER_EXEC_MUTATION_PATTERNS:
            if pattern.search(body):
                return _violation(
                    f"docker-exec-mutate:{rule}",
                    f"docker exec with write/install ({rule})",
                )

    return None


def check_kubectl_mutation(payload: dict) -> None:
    """PreToolUse Bash hook entrypoint. Raises BlockAction on violation."""
    from hooks.hook_manager import BlockAction

    tool_input = payload.get("tool_input", {}) or {}
    command = tool_input.get("command", "") or ""
    if not isinstance(command, str):
        return
    reason = check_kubectl_mutation_command(command)
    if reason:
        global _last_violation
        _last_violation = reason
        raise BlockAction(reason)
