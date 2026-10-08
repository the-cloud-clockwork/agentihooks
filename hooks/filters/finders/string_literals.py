import io
import re
import tokenize
from pathlib import PurePath

_WEB_SUFFIXES = {".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte", ".html"}
_WEB = re.compile(r"""(?P<quote>["'`])(?P<literal>(?:\\.|(?!(?P=quote)).)*)(?P=quote)|>(?P<node>[^<>]+)<""", re.DOTALL)
_DELIMITER = re.compile(r"(?i)^[rubf]*(\"\"\"|'''|\"|')")


def _finding(text: str, start: int, end: int) -> dict:
    return {"start": start, "end": end, "text": text[start:end], "reason": "user-facing literal"}


def _python(text: str) -> list[dict]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    findings = []
    stack = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            start = offsets[token.start[0] - 1] + token.start[1]
            end = offsets[token.end[0] - 1] + token.end[1]
            kind = tokenize.tok_name[token.type]
            if kind == "FSTRING_START":
                stack.append(start + len(token.string))
            elif kind == "FSTRING_END":
                literal_start = stack.pop()
                if not stack:
                    findings.append(_finding(text, literal_start, start))
            elif token.type == tokenize.STRING and not stack:
                delimiter = _DELIMITER.match(token.string)
                findings.append(_finding(text, start + delimiter.end(), end - len(delimiter[1])))
    except (tokenize.TokenError, IndentationError):
        pass
    return [item for item in findings if item["text"]]


def find(text: str, path: str, tool: str) -> list[dict]:
    suffix = PurePath(path).suffix.lower()
    if suffix == ".py":
        return _python(text)
    if suffix in _WEB_SUFFIXES:
        findings = []
        for match in _WEB.finditer(text):
            group = "literal" if match["quote"] else "node"
            if match[group].strip():
                findings.append(_finding(text, match.start(group), match.end(group)))
        return findings
    return []
