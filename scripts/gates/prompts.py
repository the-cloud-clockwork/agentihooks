"""The prompt guard: a swarm agent's rm that a harness would stop for a yes or no is refused first, with the safe form.

Claude Code asks before an rm or rmdir whose target it cannot read ahead of time, or that reaches a system, home or
workspace directory, in every permission mode, bypass included. Codex runs swarm agents with approval_policy never
and asks for nothing, so these rm classes are the whole set.
"""

import posixpath
import re
from pathlib import Path, PurePosixPath

from scripts.gates.base import Decision
from scripts.gates.identity import program_index, simple_commands

REMOVERS = frozenset({"rm", "rmdir"})
DIRECTORY_CHANGERS = frozenset({"cd", "pushd", "popd"})
GLOB = re.compile(r"[*?[]")
RUNTIME_VALUE = re.compile(r"[$`]|^~[^/]")
REDIRECT = re.compile(r"^\d*[<>]")
REDIRECT_OPERATOR = re.compile(r"^\d*[<>]+$")
SUBSTITUTION = re.compile(r"\$\(|`[^`]*`")
SUBSTITUTION_CAP = 64


def refusal(why):
    return (
        f"The harness stops this rm for a yes or no that nobody in a swarm answers: {why}. Remove a scratch folder "
        "with agentihooks scratch rm <dir>; otherwise name each file or directory by its absolute path, with no glob, "
        "variable or command output, and no cd before it."
    )


def removals(text):
    """Each rm or rmdir's arguments, and whether a cd came before it in the same command."""
    moved = False
    for words in simple_commands(text):
        index = program_index(words)
        name = "" if index is None else PurePosixPath(words[index]).name
        if name in REMOVERS:
            yield words[index + 1 :], moved
        moved = moved or name in DIRECTORY_CHANGERS


def targets(args):
    found, options, skip = [], True, False
    for word in args:
        if skip:
            skip = False
        elif REDIRECT.match(word):
            skip = bool(REDIRECT_OPERATOR.match(word))
        elif options and word == "--":
            options = False
        elif not (options and word.startswith("-")):
            found.append(word)
    return found


class PromptGuard:
    name = "prompts"
    default_mode = "enforce"

    def __init__(self, home=None):
        self.home = str(Path.home()) if home is None else home

    def matches(self, call):
        return call.tool == "Bash" and "rm" in call.command

    def decide(self, call, who, state):
        if not who.pinned:
            return Decision()
        from hooks.context.credential_guard import strip_heredocs

        text = strip_heredocs(call.command)
        found = list(removals(text))
        if found and (count := len(SUBSTITUTION.findall(text))) > SUBSTITUTION_CAP:
            return Decision.deny(refusal(f"the command holds {count} command substitutions, too many to read"))
        for args, moved in found:
            for target in targets(args):
                if why := self.hazard(target, call.cwd, moved):
                    return Decision.deny(refusal(why))
        return Decision()

    def hazard(self, target, cwd, moved):
        if RUNTIME_VALUE.search(target):
            return f"the target '{target}' is a variable or command output known only when it runs"
        if GLOB.search(target):
            return f"the target '{target}' is a glob"
        if moved and not target.startswith(("/", "~")):
            return f"the relative target '{target}' runs after a cd that can fail and leave the shell elsewhere"
        if self.protected(target, cwd):
            return (
                f"the target '{target}' is a protected directory: the filesystem root, a top level directory, the "
                "home directory, or the working directory or one of its parents"
            )
        return ""

    def protected(self, target, cwd):
        if set(target) == {"\\"}:
            return True
        path = self.home + target[1:] if target.startswith("~") else target
        if not path.startswith("/"):
            if not cwd:
                return all(part in ("", ".", "..") for part in path.split("/"))
            path = posixpath.join(cwd, path)
        resolved = PurePosixPath(posixpath.normpath(path))
        workspace = PurePosixPath(posixpath.normpath(cwd)) if cwd else None
        return (
            len(resolved.parts) <= 2
            or resolved == PurePosixPath(self.home)
            or (workspace is not None and (resolved == workspace or resolved in workspace.parents))
        )
