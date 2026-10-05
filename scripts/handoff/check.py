"""Structural check of a Handoff v2 body: the six headings once and in order, None for an empty section, the
completion marker last, Read first addresses that resolve, durable wording and labelled Done claims.

It checks structure only and never certifies that the content is true.
"""

import re

from hooks import secrets

TITLE = "# Handoff v2"
HEADINGS = ("Intent", "Done", "Stopped at", "Decisions and promises", "Next", "Read first")
MARKER = "<!-- handoff complete -->"
NONE = "None"
ADDRESS = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+"
    r"|ledger:[\w.-]+/(?:tasks|phases|questions|followups|notes)/[\w.-]+"
    r"|workspace:[\w.-]+/(?:steering|progress|proof)"
    r"|recap:[\w.-]+@[\w.-]+"
    r"|inbox:\w+"
)
ADDRESS_FORMS = "a GitHub issue or pull request link, ledger:, workspace:, recap: or inbox:"
FILE_EXT = "py|md|json|jsonl|toml|ya?ml|sh|txt|js|ts|tsx|css|html|cfg|ini|lock|rs|go|sql|env|log"
FILE_NAME = re.compile(rf"[\w.@/-]*\w\.(?:{FILE_EXT})(?::\d+)*")
PATH_START = ("/", "~/", "./", "../")
URL = re.compile(r"\w+://\S+")
TOKEN_SPLIT = re.compile(r"[\s`'\"()\[\]<>,;*]+")
LINE_NUMBER = re.compile(r"(?i:\blines? \d+)|\bL\d+\b|\.\w+:\d+\b")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
EVIDENCE = re.compile(
    r"\w+://\S+|`[^`]+`|#\d+|\brun \d+|\b\d{8,}\b|\b(?:ledger|workspace|recap|inbox):\S+|\bproof\b"
    r"|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b",
    re.IGNORECASE,
)
HYPOTHESIS = re.compile(r"\bhypothes[ie]s\b", re.IGNORECASE)


def problems(text, resolves):
    """Every structural problem in the document, empty when it conforms; resolves(address) says an address exists."""
    lines = text.splitlines()
    sections = _sections(lines)
    bodies = {name: body for name, body in reversed(sections)}
    return [
        *_title(lines),
        *_headings([name for name, _ in sections]),
        *_empty(sections),
        *_marker(lines),
        *_read_first(bodies.get("Read first", []), resolves),
        *_wording(text),
        *_done(bodies.get("Done", [])),
    ]


def refusal(found):
    return "handoff refused; fix the document and run the command again:\n" + "\n".join(f"- {p}" for p in found)


def _sections(lines):
    sections, fenced = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections and line.strip() != MARKER:
            sections[-1][1].append(line)
    return sections


def _title(lines):
    first = next((line.strip() for line in lines if line.strip()), "")
    return [] if first == TITLE else [f"the first line must be {TITLE}"]


def _headings(names):
    found = [
        f"## {name} is not one of the six headings: {', '.join(HEADINGS)}" for name in names if name not in HEADINGS
    ]
    for heading in HEADINGS:
        count = names.count(heading)
        if count == 0:
            found.append(f"## {heading} is missing")
        elif count > 1:
            found.append(f"## {heading} appears more than once")
    known = list(dict.fromkeys(name for name in names if name in HEADINGS))
    if known != [heading for heading in HEADINGS if heading in known]:
        found.append(f"the headings are out of order; keep {', '.join(HEADINGS)}")
    return found


def _empty(sections):
    return [
        f"## {name} is empty; write {NONE} when there is nothing to say"
        for name, body in sections
        if name in HEADINGS and not "".join(body).strip()
    ]


def _marker(lines):
    last = next((line.strip() for line in reversed(lines) if line.strip()), "")
    return [] if last == MARKER else [f"the completion marker {MARKER} must be the last line"]


def _bullets(body):
    items = []
    for line in body:
        if BULLET.match(line):
            items.append(BULLET.sub("", line, count=1).strip())
        elif items and line.strip():
            items[-1] += " " + line.strip()
    return items


def _is_none(body):
    return "".join(body).strip() == NONE


def _read_first(body, resolves):
    if _is_none(body) or not "".join(body).strip():
        return []
    entries = _bullets(body)
    if not entries:
        return [f"Read first lists its addresses as bullets, each starting with {ADDRESS_FORMS}"]
    found = []
    for number, entry in enumerate(entries, 1):
        address = entry.split()[0].strip("`<>")
        if not ADDRESS.fullmatch(address):
            found.append(f"Read first entry {number} does not start with an address ({ADDRESS_FORMS})")
        elif not resolves(address):
            found.append(f"Read first entry {number}: {address} does not resolve")
    return found


def _wording(text):
    prose = "\n".join(line for line in text.splitlines() if line.strip() != MARKER)
    names = secrets.scan(prose, mode="standard")
    found = [f"credential value found ({', '.join(names)}); refer to it by its variable name"] if names else []
    found += [f"file path {path}; name the module, type or command instead" for path in _paths(prose)]
    found += [
        f"line number {where}; name the function or behaviour instead"
        for where in dict.fromkeys(m.group(0) for m in LINE_NUMBER.finditer(prose))
    ]
    return found


def _paths(prose):
    found = []
    for token in TOKEN_SPLIT.split(prose):
        token = token.rstrip(".:")
        if not token or URL.match(token) or ADDRESS.fullmatch(token):
            continue
        if (token.startswith(PATH_START) and len(token) > 1) or FILE_NAME.fullmatch(token):
            found.append(token)
    return list(dict.fromkeys(found))


def _done(body):
    if _is_none(body):
        return []
    return [
        f"Done bullet {number} carries no evidence; add it or label the bullet hypothesis"
        for number, bullet in enumerate(_bullets(body), 1)
        if not EVIDENCE.search(bullet) and not HYPOTHESIS.search(bullet)
    ]
