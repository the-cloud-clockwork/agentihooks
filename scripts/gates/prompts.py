"""The prompt guard: a swarm agent's rm that a harness would stop for a yes or no is refused first, with the safe form.

Claude Code asks before an rm or rmdir whose target it cannot read ahead of time, or that reaches a system, home or
workspace directory, in every permission mode, bypass included. Codex runs swarm agents with approval_policy never
and asks for nothing, so these rm classes are the whole set.
"""

import posixpath
import re
import shlex
from pathlib import Path, PurePosixPath

from scripts.gates.base import Decision
from scripts.gates.identity import CONTROL, program_index

REMOVERS = frozenset({"rm", "rmdir"})
DIRECTORY_CHANGERS = frozenset({"cd", "pushd", "popd"})
GUARDED = "&&"
GLOB = re.compile(r"[*?[]")
RUNTIME_VALUE = re.compile(r"[$`]|^~[^/]")
REDIRECT_OPERATOR = re.compile(r"^\d*[<>]+$")
REDIRECT = re.compile(r"^\d*[<>]")
SUBSTITUTION = re.compile(r"\$\(|`[^`]*`")
SUBSTITUTION_CAP = 64


def refusal(why):
    return (
        f"The harness stops this rm for a yes or no that nobody in a swarm answers: {why}. Remove a scratch folder "
        "with agentihooks scratch rm <dir>; otherwise name each file or directory by its absolute path, with no glob, "
        "variable or command output, and no cd before it."
    )


def _tokens(text):
    lexer = shlex.shlex(text, posix=True, punctuation_chars="".join(sorted(CONTROL)))
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _separates(token, previous):
    return CONTROL.issuperset(token) and not (token == "&" and previous[-1:] in ("<", ">"))


def _nests(words, token):
    return " " in token and bool(words) and (words[-1] == "-c" or PurePosixPath(words[0]).name == "eval")


def _changes_directory(words):
    index = program_index(words)
    return index is not None and PurePosixPath(words[index]).name in DIRECTORY_CHANGERS


def commands(text):
    """Each simple command's words, and whether a cd before it can have failed with the command still running."""
    words, previous, changed, unguarded = [], "", False, False
    for token in _tokens(text):
        if token and _separates(token, previous):
            if words:
                yield words, unguarded
                changed = changed or _changes_directory(words)
            unguarded = unguarded or (changed and token != GUARDED)
            words = []
        else:
            if _nests(words, token):
                yield from commands(token)
            words.append(token)
        previous = token
    if words:
        yield words, unguarded


def targets(args):
    found, options, skip = [], True, False
    for word in args:
        if skip:
            skip = word == "&"
        elif REDIRECT_OPERATOR.match(word):
            skip = True
        elif REDIRECT.match(word):
            continue
        elif options and word == "--":
            options = False
        elif not (options and word.startswith("-")):
            found.append(word)
    return found


def removals(text):
    for words, unguarded in commands(text):
        index = program_index(words)
        if index is not None and PurePosixPath(words[index]).name in REMOVERS:
            yield words[index + 1 :], unguarded


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
        for args, unguarded in found:
            for target in targets(args):
                if why := self.hazard(target, call.cwd, unguarded):
                    return Decision.deny(refusal(why))
        return Decision()

    def hazard(self, target, cwd, unguarded):
        if RUNTIME_VALUE.search(target):
            return f"the target '{target}' is a variable or command output known only when it runs"
        if GLOB.search(target):
            return f"the target '{target}' is a glob"
        if unguarded and not target.startswith(("/", "~")):
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
