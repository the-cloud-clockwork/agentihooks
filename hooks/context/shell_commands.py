import re
import shlex
from pathlib import Path

__all__ = [
    "Path",
    "commands",
    "located",
    "re",
    "shlex",
    "unwrap",
    "_FROZEN_COMMANDS",
    "_GLOBAL_VALUES",
    "_command",
    "_dispatches",
    "_ref",
    "_targets",
]

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_SEPARATORS = ";|&()\n"
_GROUPS = frozenset({"{", "}"})
_ARITHMETIC = {"((": 1, "))": -1}
_WORD = re.compile(r"""(?:[^\s;|&()<>'"]|'[^']*'|"[^"]*")*""")
_OPERATORS = ("&&", "||", ";;", "|&", ";", "|", "&", "(", ")", "{", "}", "\n")
_PREFIXES = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!"})
_WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-T", "-R", "-D", "--user", "--group", "--host", "--prompt"},
    "nice": {"-n", "--adjustment"},
    "env": {"-u", "--unset", "-C", "--chdir"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "time": {"-f", "--format", "-o", "--output"},
    "exec": {"-a"},
    "command": set(),
    "nohup": set(),
}
_LAUNCHERS = {
    "uv": {"run"},
    "poetry": {"run"},
    "pipenv": {"run"},
    "pdm": {"run"},
    "npm": {"exec", "x"},
    "pnpm": {"exec", "dlx"},
    "yarn": {"exec", "dlx"},
    "bun": {"x"},
}
_LAUNCH_OPTIONS = {"--project", "--directory", "-C", "--cwd", "--with", "--python", "-p", "--package"}
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_SHELL_FLAG = re.compile(r"^-[A-Za-z]+$")
_SUBSTITUTIONS = re.compile(r"\$\(([^()]*)\)|`([^`]+)`")


_FROZEN_COMMANDS = (
    ("git", "push"),
    ("gh", "run", "rerun"),
    ("gh", "workflow", "run"),
    ("kubectl", "apply"),
    ("kubectl", "rollout"),
    ("helm", "upgrade"),
    ("helm", "install"),
    ("argocd", "app", "sync"),
)
_GIT_VALUES = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env", "--attr-source"}
)
_PUSH_WIDENS = frozenset({"--all", "--branches", "--mirror", "--tags", "--follow-tags", "--prune", "--repo"})
_PUSH_VALUES = frozenset({"-o", "--push-option"})
_REDIRECTION = re.compile(r"([^<>]*)([<>].*)")
_KUBECTL_VALUES = frozenset(
    {
        "-n",
        "--namespace",
        "-s",
        "--server",
        "-v",
        "--v",
        "--as",
        "--as-group",
        "--as-uid",
        "--as-user-extra",
        "--cache-dir",
        "--certificate-authority",
        "--client-certificate",
        "--client-key",
        "--cluster",
        "--context",
        "--kubeconfig",
        "--kuberc",
        "--log-flush-frequency",
        "--password",
        "--profile",
        "--profile-output",
        "--proxy-url",
        "--request-timeout",
        "--tls-server-name",
        "--token",
        "--user",
        "--username",
        "--vmodule",
    }
)
_HELM_VALUES = frozenset(
    {
        "-n",
        "--namespace",
        "--burst-limit",
        "--color",
        "--colour",
        "--content-cache",
        "--kube-apiserver",
        "--kube-as-group",
        "--kube-as-user",
        "--kube-ca-file",
        "--kube-context",
        "--kube-tls-server-name",
        "--kube-token",
        "--kubeconfig",
        "--qps",
        "--registry-config",
        "--repository-cache",
        "--repository-config",
    }
)
_ARGOCD_VALUES = frozenset(
    {
        "-H",
        "--header",
        "--argocd-context",
        "--auth-token",
        "--client-crt",
        "--client-crt-key",
        "--config",
        "--controller-name",
        "--grpc-web-root-path",
        "--http-retry-max",
        "--kube-context",
        "--logformat",
        "--loglevel",
        "--port-forward-namespace",
        "--redis-compress",
        "--redis-haproxy-name",
        "--redis-name",
        "--repo-server-name",
        "--server",
        "--server-crt",
        "--server-name",
    }
)
_GLOBAL_VALUES = {
    "git": _GIT_VALUES,
    "gh": frozenset({"-R", "--repo"}),
    "kubectl": _KUBECTL_VALUES,
    "helm": _HELM_VALUES,
    "argocd": _ARGOCD_VALUES,
}
_API_VALUES = frozenset(
    {
        "-F",
        "--field",
        "-f",
        "--raw-field",
        "-H",
        "--header",
        "-X",
        "--method",
        "-q",
        "--jq",
        "-t",
        "--template",
        "-p",
        "--preview",
        "--hostname",
        "--input",
        "--cache",
    }
)
_GH_REPO = re.compile(r"-R.*|--repo(=.*)?")


def _options(tokens: list[str], valued: set[str]) -> list[str]:
    while tokens and tokens[0].startswith("-"):
        option, tokens = tokens[0], tokens[1:]
        if option == "--":
            break
        if option in valued:
            tokens = tokens[1:]
    return tokens


def _env_split(tokens: list[str]) -> list[str]:
    for index, token in enumerate(tokens):
        if token in {"-S", "--split-string"} and index + 1 < len(tokens):
            return tokens[:index] + shlex.split(tokens[index + 1]) + tokens[index + 2 :]
        if token.startswith("-S"):
            return tokens[:index] + shlex.split(token[2:]) + tokens[index + 1 :]
        if token.startswith("--split-string="):
            return tokens[:index] + shlex.split(token.split("=", 1)[1]) + tokens[index + 1 :]
    return tokens


def _env_folders(tokens: list[str]) -> list[str]:
    folders = []
    while tokens and tokens[0].startswith("-") and tokens[0] != "--":
        option, tokens = tokens[0], tokens[1:]
        if option in {"-C", "--chdir"}:
            folders += tokens[:1]
        elif option.startswith("--chdir="):
            folders.append(option.partition("=")[2])
        elif option.startswith("-C"):
            folders.append(option[2:])
        if option in _WRAPPERS["env"]:
            tokens = tokens[1:]
    return folders


def unwrap(tokens: list[str]) -> tuple[list[str], list[str]]:
    folders = []
    while tokens:
        name = Path(tokens[0]).name
        if _ASSIGNMENT.match(tokens[0]) or name in _PREFIXES:
            tokens = tokens[1:]
        elif name in _WRAPPERS:
            rest = _env_split(tokens[1:]) if name == "env" else tokens[1:]
            if name == "env":
                folders += _env_folders(rest)
            tokens = _options(rest, _WRAPPERS[name])
            if name == "timeout":
                tokens = tokens[1:]
        elif name in {"npx", "uvx"}:
            tokens = _options(tokens[1:], _LAUNCH_OPTIONS)
        elif name in _LAUNCHERS:
            rest = _options(tokens[1:], _LAUNCH_OPTIONS)
            if not rest or rest[0] not in _LAUNCHERS[name]:
                break
            tokens = _options(rest[1:], _LAUNCH_OPTIONS)
        else:
            break
    return tokens, folders


def _expand(words: list[str], depth: int, folders: list[str]) -> tuple[list[tuple[list[str], list[str]]], list[str]]:
    tokens, chdirs = unwrap(words)
    if not tokens:
        return [], folders
    here = folders + chdirs
    if Path(tokens[0]).name in _SHELLS:
        for index, token in enumerate(tokens[1:], 1):
            if _SHELL_FLAG.fullmatch(token) and "c" in token and index + 1 < len(tokens):
                return located(tokens[index + 1], depth + 1, here), folders
            if token.startswith("<<<"):
                return located(token[3:] or (tokens + [""])[index + 1], depth + 1, here), folders
    if Path(tokens[0]).name == "cd" and not chdirs:
        folder = next((word for word in tokens[1:] if word == "-" or not word.startswith("-")), "~")
        return [(tokens, here)], folders + [folder]
    return [(tokens, here)], folders


def _heredoc_marker(line: str, quote: str | None) -> tuple[int | None, str | None]:
    escaped = skip = arithmetic = 0
    for index, character in enumerate(line):
        if index == escaped - 1 or index < skip:
            continue
        if character == "\\" and quote != "'":
            escaped = index + 2
        elif quote is None and line.startswith("<<<", index):
            skip = index + 3
        elif quote is None and line[index : index + 2] in _ARITHMETIC:
            arithmetic = max(arithmetic + _ARITHMETIC[line[index : index + 2]], 0)
        elif quote is None and line.startswith("<<", index) and not arithmetic:
            return index, None
        elif quote is None and character == "#" and (index == 0 or (line[index - 1].isspace() and index != escaped)):
            return index, None
        elif quote is None and line.startswith("$'", index):
            quote, escaped = "$'", index + 2
        elif quote and character == quote[-1]:
            quote = None
        elif quote is None and character in {"'", '"'}:
            quote = character
    return None, quote


def _word_end(text: str) -> int:
    return _WORD.match(text).end()


def _heredocs(command: str, depth: int, folders: list[str]) -> tuple[str, list[tuple[list[str], list[str]]]]:
    result, kept, quote = [], [], None
    lines = iter(command.splitlines(keepends=True))
    for line in lines:
        start, quote = _heredoc_marker(line, quote)
        if start is None:
            kept.append(line)
            continue
        if line[start] == "#":
            kept.append(line[:start] + "\n")
            continue
        prefix, tail = line[:start], line[start + 2 :].removeprefix("-").lstrip()
        end = _word_end(tail)
        delimiters = shlex.split(tail[:end])
        if not delimiters:
            raise ValueError("Missing heredoc delimiter")
        rest = tail[end:]
        cut, _ = _heredoc_marker(rest, None)
        rest = rest if cut is None else rest[:cut]
        body = []
        for content in lines:
            if content.rstrip("\r\n").lstrip("\t") == delimiters[0]:
                break
            body.append(content)
        kept.append(prefix + rest + "\n")
        readers = located(prefix, depth + 1, folders)
        if rest.lstrip().startswith("|"):
            readers += located(rest, depth + 1, readers[-1][1] if readers else folders)
        for tokens, here in readers:
            name = Path(tokens[0]).name
            if name in _SHELLS:
                result.extend(located("".join(body), depth + 1, here))
            elif re.fullmatch(r"python[\d.]*|pypy[\d.]*", name):
                result.append(([name, "-c", "".join(body)], here))
    return "".join(kept), result


def _substitutions(command: str) -> list[str]:
    scripts, quote, end = [], None, 0
    for index, character in enumerate(command):
        if index < end:
            continue
        if character == "\\" and quote != "'":
            end = index + 2
            continue
        match = _SUBSTITUTIONS.match(command, index)
        if match and quote != "'":
            scripts.append(match[1] or match[2] or "")
            end = match.end()
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
    return scripts


def _operators(token: str) -> list[str]:
    found = []
    while token:
        operator = next(operator for operator in _OPERATORS if token.startswith(operator))
        found.append(operator)
        token = token[len(operator) :]
    return found


def located(command: str, depth: int = 0, folders: list[str] | None = None) -> list[tuple[list[str], list[str]]]:
    if depth > 10:
        raise ValueError("Shell wrapper nesting exceeds ten levels")
    base = list(folders or [])
    command, result = _heredocs(command, depth, base)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    current = sequence = pipeline = base
    piped, frames, words = False, [], []
    for position, token in enumerate(tokens):
        redirect = token == "&" and (
            (words and words[-1].endswith((">", "<")))
            or any(word.startswith(">") for word in tokens[position + 1 : position + 2])
        )
        if not token or (token.strip(_SEPARATORS) and token not in _GROUPS) or redirect:
            words.append(token)
            continue
        pairs, current = _expand(words, depth, current)
        result.extend(pairs)
        words = []
        for operator in _operators(token):
            if operator in {"|", "|&"}:
                piped, current = True, pipeline
            elif operator == "&":
                piped, current = False, sequence
                pipeline = current
            elif operator in {"(", "{"}:
                frames.append((operator, current, sequence, pipeline, piped))
                sequence, pipeline, piped = current, current, False
            elif operator in {")", "}"}:
                if frames:
                    kind, saved, sequence, pipeline, piped = frames.pop()
                    current = saved if kind == "(" else current
            else:
                current = pipeline if piped else current
                piped, pipeline = False, current
                sequence = sequence if operator in {"&&", "||"} else current
    pairs, _ = _expand(words, depth, current)
    result.extend(pairs)
    for script in _substitutions(command):
        result.extend(located(script, depth + 1, base))
    return result


def commands(command: str, depth: int = 0) -> list[list[str]]:
    return [tokens for tokens, _ in located(command, depth)]


def _words(tokens: list[str]) -> list[str]:
    """Return a simple command's tokens without its redirections: a redirection that ends in its operator takes the
    next token as its target, and text before a < or > stays a word unless it names a file descriptor."""
    kept = [token for token in tokens if token != "&"]
    targets = {index + 1 for index, token in enumerate(kept) if token.endswith(("<", ">"))}
    words: list[str] = []
    for index, token in enumerate(kept):
        found = _REDIRECTION.fullmatch(token)
        if index in targets:
            continue
        if found is None:
            words.append(token)
        elif found.group(1) and not found.group(1).isdigit():
            words.append(found.group(1))
    return words


def _command(tokens: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return a simple command's words without redirections, its program's base name first, and the global options
    of git, gh, kubectl, helm or argocd before each subcommand word a frozen command names, taken out of those words
    as (option, value) pairs."""
    words = _words(tokens)
    if not words:
        return [], []
    head, rest = [Path(words[0]).name], iter(words[1:])
    options: list[tuple[str, str]] = []
    for word in rest:
        if not _extends(head):
            return [*head, word, *rest], options
        if word.startswith("-"):
            options.append((word, next(rest, "") if word in _GLOBAL_VALUES[head[0]] else ""))
        else:
            head.append(word)
    return head, options


def _extends(head: list[str]) -> bool:
    """Return whether a frozen command names more subcommand words after head."""
    return any(len(prefix) > len(head) and list(prefix[: len(head)]) == head for prefix in _FROZEN_COMMANDS)


def _dispatches(words: list[str]) -> bool:
    """Return whether a gh api call names a workflow or repository dispatch endpoint: its endpoint, the first word
    that is neither an option nor an option's value, ends in dispatches."""
    if words[:2] != ["gh", "api"]:
        return False
    rest = iter(words[2:])
    for word in rest:
        if word in _API_VALUES:
            next(rest, None)
        elif not word.startswith("-"):
            return word.partition("?")[0].rstrip("/").rpartition("/")[2] == "dispatches"
    return False


def _targets(words: list[str]) -> tuple[str, ...] | None:
    """Return the refs a command a fix branch session may run writes: none for a rerun or for a push of the checked
    out branch, a dispatch's ref and a push's refspec destinations; None for any other command, for a gh command
    aimed at another repository and for a push that may write more than its refspecs."""
    if words[:1] == ["gh"] and any(_GH_REPO.fullmatch(word) for word in words):
        return None
    if words[:3] == ["gh", "run", "rerun"]:
        return ()
    if words[:3] == ["gh", "workflow", "run"]:
        ref = _ref(words[3:])
        return (ref,) if ref else None
    if words[:2] == ["git", "push"]:
        return _pushed(words[2:])
    return None


def _ref(words: list[str]) -> str:
    """Return the last ref a workflow dispatch names with --ref or -r, or an empty name when it names none and runs
    on the default branch."""
    found = ""
    for index, word in enumerate(words):
        if word in ("--ref", "-r"):
            found = words[index + 1] if index + 1 < len(words) else ""
        elif word.startswith("--ref="):
            found = word.removeprefix("--ref=")
        elif word.startswith("-r"):
            found = word.removeprefix("-r").removeprefix("=")
    return found


def _pushed(words: list[str]) -> tuple[str, ...] | None:
    """Return the refs a push to origin writes, or None when it names another remote or an option that pushes more
    than its refspecs; a push with no refspec writes the checked out branch and names none. A push to the lab writes
    the branches its push options hand the relay, or none, which leaves the checked out branch it grades."""
    from scripts.ci_lab.remote import checkout

    if any(word.partition("=")[0] in _PUSH_WIDENS for word in words):
        return None
    named = _named(words)
    if named[:1] == [checkout.REMOTE]:
        return tuple(checkout.relayed(words))
    if named[:1] not in ([], ["origin"]):
        return None
    return _destinations(named[1:])


def _named(words: list[str]) -> list[str]:
    """Return a push's remote and refspecs: its words without options and the values of options that take one."""
    named: list[str] = []
    rest = iter(words)
    for word in rest:
        if word in _PUSH_VALUES:
            next(rest, None)
        elif not word.startswith("-"):
            named.append(word)
    return named


def _destinations(refspecs: list[str]) -> tuple[str, ...]:
    """Return the ref each refspec writes; a bare HEAD writes the checked out branch and names none."""
    parts = [refspec.removeprefix("+").partition(":") for refspec in refspecs]
    return tuple(target if colon else source for source, colon, target in parts if colon or source != "HEAD")
