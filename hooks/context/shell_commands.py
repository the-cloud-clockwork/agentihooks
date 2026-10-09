import re
import shlex
from pathlib import Path

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_SEPARATORS = ";|&(){}\n"
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
_SHELL_FLAG = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n\1\b", re.DOTALL)
_SUBSTITUTIONS = re.compile(r"\$\(([^()]*)\)|`([^`]+)`")


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
        if token.startswith("-S") and token != "-S":
            return tokens[:index] + shlex.split(token[2:]) + tokens[index + 1 :]
        if token.startswith("--split-string="):
            return tokens[:index] + shlex.split(token.split("=", 1)[1]) + tokens[index + 1 :]
    return tokens


def unwrap(tokens: list[str]) -> list[str]:
    while tokens:
        name = Path(tokens[0]).name
        if _ASSIGNMENT.match(tokens[0]) or name in _PREFIXES:
            tokens = tokens[1:]
        elif name in _WRAPPERS:
            rest = _env_split(tokens[1:]) if name == "env" else tokens[1:]
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


def _heredocs(command: str, depth: int) -> tuple[str, list[list[str]]]:
    result = []
    for match in _HEREDOC.finditer(command):
        prefix = command[: match.start()].rsplit("\n", 1)[-1]
        body = match[0].split("\n", 1)[1].rsplit("\n", 1)[0]
        for tokens in commands(prefix, depth + 1):
            name = Path(tokens[0]).name
            if name in _SHELLS:
                result.extend(commands(body, depth + 1))
            elif re.fullmatch(r"python[\d.]*|pypy[\d.]*", name):
                result.append([name, "-c", body])
    return _HEREDOC.sub("", command), result


def _substitutions(command: str) -> list[str]:
    scripts, quote, index = [], "", 0
    while index < len(command):
        character = command[index]
        if character == "\\" and quote != "'":
            index += 2
            continue
        match = _SUBSTITUTIONS.match(command, index)
        if match and quote != "'":
            scripts.append(match[1] or match[2])
            index = match.end()
            continue
        if character in {"'", '"\\"'}:
            if not quote:
                quote = character
            elif quote == character:
                quote = ""
        index += 1
    return scripts


def commands(command: str, depth: int = 0) -> list[list[str]]:
    if depth > 10:
        raise ValueError("Shell wrapper nesting exceeds ten levels")
    command, heredoc_commands = _heredocs(command, depth)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SEPARATORS)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    result, words = heredoc_commands, []
    for token in lexer:
        if token and not token.strip(_SEPARATORS):
            result.extend(_expand(words, depth))
            words = []
        else:
            words.append(token)
    result.extend(_expand(words, depth))
    for script in _substitutions(command):
        result.extend(commands(script, depth + 1))
    return result
