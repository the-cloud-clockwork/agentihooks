import re
import shlex
from pathlib import Path

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
_SEPARATORS = ";|&(){}\n"
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
        if not token or token.strip(_SEPARATORS) or redirect:
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
