"""Shared command stripping for guard pattern matching.

Removes non-command content (heredocs, quoted strings, echo/printf bodies,
curl data payloads, python -c strings) before applying security regex patterns.
This prevents false positives when commands contain documentation text or
message bodies that happen to match blocked patterns.
"""

import re

import tree_sitter_bash
from tree_sitter import Language, Parser, Query, QueryCursor

from hooks.context.credential_guard import BODY_SUBSTITUTION_OPEN, substitutions

_BASH = Language(tree_sitter_bash.language())
_HEREDOCS = Query(_BASH, "(heredoc_redirect (heredoc_start) @start (heredoc_body) @body (heredoc_end) @end) @redirect")


def _without_heredocs(command: str) -> str:
    """Cut each terminated heredoc the bash parse finds: its opener, and its body through the terminator line.

    The rest of the delimiter line stays. An unquoted delimiter's body leaves the command substitutions bash runs in
    it, each on a line of its own with its heredocs cut the same way; the parse yields a heredoc after the ones nested
    in it, so the enclosing cut wins. An arithmetic shift never parses as a heredoc. A heredoc counts as terminated
    only when its terminator holds the delimiter word, because the parse can end an unterminated body holding an
    expansion with a terminator made of the rest of the text. A double quoted delimiter never matches its terminator,
    so the guards still read its body. A parse with an error keeps every heredoc, as prod
    lockdown keeps the whole command, because error recovery can find a terminator inside a line.
    """
    source = command.encode("utf-8", "surrogatepass")
    tree = Parser(_BASH).parse(source)
    if tree.root_node.has_error:
        return command
    pieces = [source[index : index + 1] for index in range(len(source))]
    for _, match in QueryCursor(_HEREDOCS).matches(tree.root_node):
        redirect, start, body, end = (match[name][0] for name in ("redirect", "start", "body", "end"))
        opener = source[start.start_byte : start.end_byte]
        delimiter = opener.strip(b"'")
        if delimiter and source[end.start_byte : end.end_byte] == delimiter:
            line_end = source.rfind(b"\n", 0, body.start_byte)
            for first, last in ((redirect.start_byte, start.end_byte), (line_end, end.end_byte)):
                pieces[first:last] = [b""] * (last - first)
            if opener == delimiter:
                pieces[line_end] = _substitution_lines(source[line_end : end.start_byte])
    return b"".join(pieces).decode(errors="surrogatepass")


def _substitution_lines(body: bytes) -> bytes:
    """Each command substitution bash runs in an unquoted heredoc body, on a line of its own, its heredocs cut."""
    text = body.decode(errors="surrogatepass")
    lines = (f"\n{_without_heredocs(inner)}" for inner in substitutions(text, BODY_SUBSTITUTION_OPEN))
    return "".join(lines).encode(errors="surrogatepass")


def strip_non_command_content(command: str) -> str:
    """Strip all non-command content from a shell command string.

    Returns a version of the command safe for regex pattern matching — all
    data payloads, string arguments, and heredoc bodies are removed so only
    the actual command structure remains.
    """
    check = command
    try:
        pass
    except OSError:
        pass

    check = re.sub(r"\(\((?:[^()]|\([^()]*\))*?\)\)", lambda arithmetic: arithmetic.group(0).replace("<<", "  "), check)
    check = _without_heredocs(check)

    check = re.sub(r'-m\s+"[^"]*"', "-m MSG", check)
    check = re.sub(r"-m\s+'[^']*'", "-m MSG", check)

    check = re.sub(r"\bpython[0-9.]*\s+-c\s+'[^']*'", "python -c ''", check)
    check = re.sub(r'\bpython[0-9.]*\s+-c\s+"[^"]*"', 'python -c ""', check)

    check = re.sub(r'\b(echo|printf)\s+(-[neE]+\s+)?"[^"]*"', r"\1 MSG", check)
    check = re.sub(r"\b(echo|printf)\s+(-[neE]+\s+)?'[^']*'", r"\1 MSG", check)

    check = re.sub(r"(-d|--data(?:-raw|-binary)?)\s+'[^']*'", r"\1 ''", check)
    check = re.sub(r'(-d|--data(?:-raw|-binary)?)\s+"[^"]*"', r'\1 ""', check)

    check = re.sub(r"\b(jq|awk|sed)\s+'[^']*'", r"\1 ''", check)

    return check
