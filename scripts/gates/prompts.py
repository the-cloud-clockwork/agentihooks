"""The prompt guard: a swarm agent's rm that a harness would stop for a yes or no is refused first, with the safe form.

Claude Code asks before an rm or rmdir whose target it cannot read ahead of time, or that reaches a system, home or
workspace directory, in every permission mode, bypass included. It asks the same for a shell -c script that runs a
command named by a variable or command output, which it reads as an rm it cannot check. Codex runs swarm agents with
approval_policy never and asks for nothing, so these classes are the whole set.
"""

import posixpath
import re
from itertools import dropwhile
from pathlib import Path, PurePosixPath

from scripts.gates.base import Decision
from scripts.gates.identity import program_index, simple_commands

REMOVERS = frozenset({"rm", "rmdir"})
DIRECTORY_CHANGERS = frozenset({"cd", "pushd", "popd"})
SHELLS = frozenset({"sh", "bash", "zsh"})
INLINE_FLAG = re.compile(r"-(?=[A-Za-z]*c)[A-Za-z]+")
OPTION_VALUE = re.compile(r"[-+](?=[A-Za-z]*[oO])[A-Za-z]+|--rcfile|--init-file")
KEYWORDS = frozenset({"!", "{", "}", "if", "then", "elif", "else", "do", "while", "until"})
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


def script_refusal(program):
    return (
        "The harness stops this shell -c script for a yes or no that nobody in a swarm answers: it runs the command "
        f"'{program}', a variable or command output the harness reads as an rm it cannot check. Name each program in "
        "the script literally, or run the commands without a shell -c wrapper."
    )


def inline_scripts(text):
    for words in simple_commands(text):
        index = program_index(words)
        if index is None or PurePosixPath(words[index]).name not in SHELLS:
            continue
        rest = iter([*words[index + 1 :], ""])
        for word in rest:
            if INLINE_FLAG.fullmatch(word):
                if OPTION_VALUE.fullmatch(word):
                    next(rest)
                yield next(rest)
                break
            if not word.startswith(("-", "+")):
                break
            if OPTION_VALUE.fullmatch(word):
                next(rest)


def variable_programs(script):
    for words in simple_commands(script):
        rest = list(dropwhile(KEYWORDS.__contains__, words))
        index = program_index(rest)
        if index is not None and rest[index].startswith(("$", "`")):
            yield rest[index]


def removals(text):
    """Each rm or rmdir's arguments, and whether a cd came before it in the same command."""
    moved = False
    for words in simple_commands(text):
        index = program_index(words)
        if index is None:
            continue
        name = PurePosixPath(words[index]).name
        if name in REMOVERS:
            yield words[index + 1 :], moved
        moved = moved or name in DIRECTORY_CHANGERS


def targets(args):
    found, words, options = [], iter(args), True
    for word in words:
        if REDIRECT_OPERATOR.match(word):
            next(words, None)
        elif REDIRECT.match(word) or (options and word.startswith("-")):
            options = options and word != "--"
        else:
            found.append(word)
    return found


class PromptGuard:
    name = "prompts"
    default_mode = "enforce"

    def __init__(self, home=None):
        self.home = str(Path.home()) if home is None else home

    def matches(self, call):
        return call.tool == "Bash" and any(mark in call.command for mark in ("rm", "$", "`"))

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
        if program := next((name for script in inline_scripts(text) for name in variable_programs(script)), None):
            return Decision.deny(script_refusal(program))
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
