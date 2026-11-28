import ast
import shlex


def _words(command: str) -> list[str]:
    return shlex.split(command)


def _literal(text: str) -> bool:
    return "$" not in text


def split_canary(command: str) -> list[ast.stmt]:
    words = shlex.split(command)
    # ruleid: first-read-canary-raw-expansion
    return ast.parse(words[2]).body


def gate_words_canary(command: str) -> list[ast.stmt]:
    words = _words(command)
    try:
        # ruleid: first-read-canary-raw-expansion
        return ast.parse(words[2]).body
    except SyntaxError:
        return []


def checked_canary(command: str) -> list[ast.stmt]:
    if not _literal(command):
        return []
    words = _words(command)
    try:
        # ok: first-read-canary-raw-expansion
        return ast.parse(words[2]).body
    except SyntaxError:
        return []
