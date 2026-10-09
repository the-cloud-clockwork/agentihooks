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
_SHELL_FLAG = re.compile(r"^-[A-Za-z]+$")
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
        if token.startswith("-S"):
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
            if _SHELL_FLAG.fullmatch(token) and "c" in token and index + 1 < len(tokens):
                return commands(tokens[index + 1], depth + 1)
    return [tokens]


def _heredoc_marker(line: str, quote: str | None) -> tuple[int, str | None]:
    index = 0
    while index < len(line):
        character = line[index]
        if character == "\\" and quote != "'":
            index += 2
            continue
        if quote is None and line.startswith("<<", index):
            return index, None
        if character == quote:
            quote = None
        elif quote is None and character in {"'", '"'}:
            quote = character
        index += 1
    return -1, quote


def _heredocs(command: str, depth: int) -> tuple[str, list[list[str]]]:
    result, kept, quote = [], [], None
    lines = iter(command.splitlines(keepends=True))
    for line in lines:
        start, quote = _heredoc_marker(line, quote)
        if start < 0:
            kept.append(line)
            continue
        prefix, tail = line[:start], line[start + 2 :]
        delimiters = shlex.split(tail.removeprefix("-"))
        if not delimiters:
            raise ValueError("Missing heredoc delimiter")
        body = []
        for content in lines:
            if content.rstrip("\r\n").lstrip("\t") == delimiters[0]:
                break
            body.append(content)
        kept.append(prefix + "\n")
        for tokens in commands(prefix, depth + 1):
            name = Path(tokens[0]).name
            if name in _SHELLS:
                result.extend(commands("".join(body), depth + 1))
            elif re.fullmatch(r"python[\d.]*|pypy[\d.]*", name):
                result.append([name, "-c", "".join(body)])
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
