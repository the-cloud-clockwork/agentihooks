import re
import shlex
from pathlib import Path

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_SEPARATORS = ";|&(){}\n"
_PREFIXES = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!"})
_WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-T", "-R", "-D", "--user", "--group", "--host", "--prompt"},
    "nice": {"-n", "--adjustment"},
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
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
_SHELL_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n\1\b", re.DOTALL)
_SINGLE_QUOTED = re.compile(r"'[^']*'")
_BACKTICKS = re.compile(r"`([^`]+)`")


def _options(tokens: list[str], valued: set[str]) -> list[str]:
    while tokens and tokens[0].startswith("-"):
        option, tokens = tokens[0], tokens[1:]
        if option == "--":
            break
        if option in valued:
            tokens = tokens[1:]
    return tokens


def unwrap(tokens: list[str]) -> list[str]:
    while tokens:
        name = Path(tokens[0]).name
        if _ASSIGNMENT.match(tokens[0]) or name in _PREFIXES:
            tokens = tokens[1:]
        elif name in _WRAPPERS:
            tokens = _options(tokens[1:], _WRAPPERS[name])
            if name == "timeout":
                tokens = tokens[1:]
            if name == "env" and tokens and " " in tokens[0]:
                tokens = shlex.split(tokens[0]) + tokens[1:]
        elif name in {"npx", "uvx"}:
            tokens = _options(tokens[1:], _LAUNCH_OPTIONS)
        elif name in _LAUNCHERS:
            rest = _options(tokens[1:], _LAUNCH_OPTIONS)
            if not rest or rest[0] not in _LAUNCHERS[name]:
                break
            tokens = _options(rest[1:], _LAUNCH_OPTIONS)
        else:
            break
    return tokens


def _expand(tokens: list[str], depth: int) -> list[list[str]]:
    tokens = unwrap(tokens)
    if not tokens:
        return []
    if Path(tokens[0]).name in _SHELLS:
        for index, token in enumerate(tokens[1:], 1):
            if _SHELL_FLAG.fullmatch(token) and index + 1 < len(tokens):
                return commands(tokens[index + 1], depth + 1)
    return [tokens]


def commands(command: str, depth: int = 0) -> list[list[str]]:
    if depth > 10:
        raise ValueError("Shell wrapper nesting exceeds ten levels")
    command = _HEREDOC.sub("", command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    result, words = [], []
    for token in lexer:
        if token and not token.strip(_SEPARATORS):
            result.extend(_expand(words, depth))
            words = []
        else:
            words.append(token)
    result.extend(_expand(words, depth))
    for match in _BACKTICKS.finditer(_SINGLE_QUOTED.sub("", command)):
        result.extend(commands(match[1], depth + 1))
    return result
