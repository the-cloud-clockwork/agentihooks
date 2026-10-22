"""Production workflow and image guards; CI alone owns protected promotion."""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from hooks._redis import get_redis, redis_key
from hooks.common import log
from hooks.config import AGENTIHOOKS_HOME
from hooks.hook_manager import BlockAction

BYPASS_TYPE = "prod_lockdown_bypass"
BYPASS_TTL = 300  # 5 min — per-turn legacy bypass (--emergency-prod)
_SESSION_SIGNAL_TTL = 14400  # 4 hours — session-scoped signals (release, hotfix)

# Legacy hotfix/emergency vocabulary — kept for backwards-compat.
# Full signal vocabulary now sourced from CI Manifesto via ci_manifesto.py.
BYPASS_PHRASES: list[str] = ["--emergency-prod", "prod override", "emergency"]

_MERGE_QUEUE = (
    "query($url: URI!, $branch: String!) { resource(url: $url) { ... on PullRequest {"
    " repository { mergeQueue(branch: $branch) { id } } } } }"
)

_BLOCKED: list[tuple[re.Pattern, str, str, str]] = [
    (
        re.compile(r"(ghcr\.io|docker\.io)/[^/\s]+/[^:\s]+:(latest|prod|stable)\b", re.I),
        "image tag :latest/:prod/:stable",
        "use :dev or a branch-specific tag",
        "hotfix",
    ),
    (
        re.compile(r"\bdocker\b[^|&;\n]*(push|tag|build)[^|&;\n]*(latest|prod|stable)\b", re.I),
        "docker op with production tag",
        "use :dev or a branch-specific tag",
        "hotfix",
    ),
    (
        re.compile(r"\bgh\b[^|&;\n]*workflow\s+run\s+release\.yml\b", re.I),
        "release.yml workflow trigger",
        "CI alone creates releases.",
        "ci",
    ),
    (
        re.compile(
            r"\bgh\b[^|&;\n]*\bpr\s+merge\b[^|&;\n]*(--base\s+(main|master|v1)\b|\b(main|master|v1)\b(?!\s*\.))",
            re.I,
        ),
        "gh pr merge to main/master/v1",
        "CI alone promotes dev into protected branches.",
        "ci",
    ),
    (
        re.compile(r"\bgh\b[^|&;\n]*workflow\s+run\s+publish-pypi\.yml\b", re.I),
        "PyPI publication",
        "release dance required — see CI Manifesto §4.1",
        "release",
    ),
]


def _bypass_key(session_id: str) -> str:
    return redis_key(BYPASS_TYPE, session_id)


def _flag_file(session_id: str) -> Path:
    return AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.flag"


def set_bypass(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.setex(_bypass_key(session_id), BYPASS_TTL, "1")
        except Exception as e:
            log("prod_lockdown.set_bypass redis failed", {"error": str(e)})
    try:
        f = _flag_file(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("1")
    except Exception as e:
        log("prod_lockdown.set_bypass file failed", {"error": str(e)})


def clear_bypass(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_bypass_key(session_id))
        except Exception:
            pass
    try:
        _flag_file(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def is_bypass_active(session_id: str) -> bool:
    r = get_redis()
    if r:
        try:
            return bool(r.exists(_bypass_key(session_id)))
        except Exception:
            pass
    return _flag_file(session_id).exists()


def contains_bypass_phrase(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in BYPASS_PHRASES)


def _release_key(session_id: str) -> str:
    return redis_key("release_gate_signal", session_id)


def _release_flag(session_id: str) -> Path:
    return AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.release"


def set_release_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.setex(_release_key(session_id), _SESSION_SIGNAL_TTL, "1")
        except Exception as e:
            log("prod_lockdown.set_release_signal redis failed", {"error": str(e)})
    try:
        f = _release_flag(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("1")
    except Exception as e:
        log("prod_lockdown.set_release_signal file failed", {"error": str(e)})


def clear_release_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_release_key(session_id))
        except Exception:
            pass
    try:
        _release_flag(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def _hotfix_key(session_id: str) -> str:
    return redis_key("hotfix_signal", session_id)


def _hotfix_flag(session_id: str) -> Path:
    return AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.hotfix"


def set_hotfix_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.setex(_hotfix_key(session_id), _SESSION_SIGNAL_TTL, "1")
        except Exception as e:
            log("prod_lockdown.set_hotfix_signal redis failed", {"error": str(e)})
    try:
        f = _hotfix_flag(session_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("1")
    except Exception as e:
        log("prod_lockdown.set_hotfix_signal file failed", {"error": str(e)})


def clear_hotfix_signal(session_id: str) -> None:
    r = get_redis()
    if r:
        try:
            r.delete(_hotfix_key(session_id))
        except Exception:
            pass
    try:
        _hotfix_flag(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def _has_hotfix_signal(session_id: str) -> bool:
    if not session_id:
        return False
    r = get_redis()
    if r:
        try:
            return bool(r.exists(_hotfix_key(session_id)))
        except Exception:
            pass
    return _hotfix_flag(session_id).exists()


def _production_operands(node: Node) -> list[str]:
    from hooks.context._strip import strip_non_command_content
    from hooks.context.branch_guard import _SHELL_SCRIPT_FLAG, _SHELLS, _command_lines

    words = [
        child.text.decode()
        for index, child in enumerate(node.named_children)
        if node.field_name_for_named_child(index) in {"name", "argument"}
    ]
    values = [shlex.split(word)[0] for word in words]
    text = _command_lines(strip_non_command_content(" ".join(words)))
    pull_request = re.search(r"\bgh\b[^|&;\n]*\bpr\s+(merge|create)\b", text)
    message_flags = {"--subject", "--title", "--body", "-t", "-b"}
    operands, message_next = [], False
    for word, value in zip(words, values):
        if message_next:
            message_next = False
            continue
        if pull_request and value.split("=", 1)[0] in message_flags:
            message_next = "=" not in value
            continue
        operands.append(word)
    lines = [_command_lines(strip_non_command_content(" ".join(operands)))]
    for index, value in enumerate(values[:-1]):
        if _SHELL_SCRIPT_FLAG.fullmatch(value) and any(Path(word).name in _SHELLS for word in values[:index]):
            lines.append(_strip_safe_content(values[index + 1]))
    return lines


def _production_command_lines(node: Node) -> list[str]:
    lines = []
    if node.type == "command":
        lines.extend(_production_operands(node))
    elif node.type == "word" and node.parent.type == "expansion":
        tree = Parser(Language(tree_sitter_bash.language())).parse(b"echo word" + node.text)
        for child in tree.root_node.named_children[0].named_children[1:]:
            lines.extend(_production_command_lines(child))
    for child in node.named_children:
        lines.extend(_production_command_lines(child))
    return lines


def _strip_safe_content(command: str) -> str:
    tree = Parser(Language(tree_sitter_bash.language())).parse(command.encode())
    if tree.root_node.has_error:
        return command
    return "\n".join(_production_command_lines(tree.root_node))


def _has_release_signal(session_id: str) -> bool:
    """Check redis/flag-file for release-gate signal set this turn by UserPromptSubmit."""
    r = get_redis()
    if r:
        try:
            return bool(r.exists(redis_key("release_gate_signal", session_id)))
        except Exception:
            pass
    try:
        return (AGENTIHOOKS_HOME / "prod_bypass" / f"{session_id}.release").exists()
    except Exception:
        return False


def _merge_view_command(line: str) -> list[str] | None:
    merge = re.search(r"\bgh\b(.*?)\bpr\s+merge\b(.*)", line)
    if not merge:
        return None
    words = iter(shlex.split(" ".join(merge.groups())))
    selectors, repo = [], ""
    for word in words:
        option, separator, value = word.partition("=")
        if option in {"--repo", "-R", "--match-head-commit", "--author-email", "-A", "--body-file", "-F"}:
            value = value if separator else next(words)
            if option in {"--repo", "-R"}:
                repo = value
        elif word.startswith("-R"):
            repo = word[2:]
        elif not word.startswith("-"):
            selectors.append(word)
    if len(selectors) > 1:
        raise ValueError
    command = ["gh", "pr", "view", *selectors]
    if repo:
        command.extend(["--repo", repo])
    return [*command, "--json", "baseRefName,url", "--jq", '.baseRefName + " " + .url']


def _check_pr_merge(line: str, cwd: str, bypass: bool) -> None:
    try:
        command = _merge_view_command(line)
        if command is None:
            return
        base, url = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=10, cwd=cwd
        ).stdout.split()
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError):
        raise BlockAction(
            "BLOCKED: Cannot resolve the pull request base; promotion protection remains closed."
        ) from None
    if base in {"main", "master", "v1"}:
        raise BlockAction("BLOCKED [gh pr merge to main/master/v1]: CI alone promotes dev into protected branches.")
    if bypass:
        return
    try:
        queue = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={_MERGE_QUEUE}", "-f", f"url={url}", "-f", f"branch={base}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            cwd=cwd,
        ).stdout
        queued = json.loads(queue)["data"]["resource"]["repository"]["mergeQueue"] is not None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise BlockAction(f"BLOCKED: Cannot tell whether '{base}' has a merge queue; the merge stays closed.") from None
    if queued:
        raise BlockAction(
            f"BLOCKED [gh pr merge into a merge queue branch]: '{base}' merges only through its merge queue, "
            "and gh pr merge lands it directly. Queue it with `agentihooks swarm <slug> merge queue "
            f"{url}`, or outside a swarm with the GitHub enqueuePullRequest mutation."
        )


def _check_swarm_enqueue(command: str) -> None:
    slug = os.environ.get("AGENTIHOOKS_SWARM")
    if not slug or "enqueuePullRequest" not in command:
        return
    tree = Parser(Language(tree_sitter_bash.language())).parse(command.encode())
    if tree.root_node.has_error or _runs_enqueue(tree.root_node):
        raise BlockAction(
            "BLOCKED [raw enqueuePullRequest in a swarm]: the swarm queue command records every merge queue "
            f"entry in its journal. Queue it with `agentihooks swarm {slug} merge queue <url>`."
        )


def _runs_enqueue(node: Node) -> bool:
    if node.type == "command" and b"enqueuePullRequest" in node.text:
        program = Path(node.child_by_field_name("name").text.decode()).name
        arguments = [
            child.text.decode()
            for index, child in enumerate(node.named_children)
            if node.field_name_for_named_child(index) == "argument"
        ]
        if program == "curl" or (program == "gh" and arguments[:1] == ["api"]):
            return True
    return any(_runs_enqueue(child) for child in node.named_children)


def check_prod_lockdown(payload: dict) -> None:
    session_id = payload.get("session_id", "")
    try:
        from hooks.context.controls_toggle import is_controls_disabled

        controls_off = is_controls_disabled(session_id)
    except Exception:
        controls_off = False
    full_bypass = bool(session_id and is_bypass_active(session_id))
    hotfix_unlock = bool(session_id and _has_hotfix_signal(session_id))
    release_unlock = bool(session_id and _has_release_signal(session_id))

    command = payload.get("tool_input", {}).get("command", "")
    if not command:
        return

    check_text = _strip_safe_content(command)

    for pattern, name, reason, category in _BLOCKED:
        if not any(pattern.search(line) for line in check_text.splitlines()):
            continue
        if category == "ci":
            raise BlockAction(f"BLOCKED [{name}]: {reason}")
        if category == "release":
            if release_unlock:
                continue
            raise BlockAction(f"BLOCKED [{name}]: {reason}\nThe operator must request the release dance.")
        if controls_off or full_bypass or hotfix_unlock:
            continue
        raise BlockAction(
            f"BLOCKED [{name}]: {reason}\nAdd --emergency-prod / 'prod override' / 'emergency' to your message."
        )
    _check_swarm_enqueue(command)
    from hooks.context.branch_guard import _resolve_cwd

    cwd = _resolve_cwd(command, payload.get("cwd", ""))
    for line in check_text.splitlines():
        _check_pr_merge(line, cwd, controls_off)
