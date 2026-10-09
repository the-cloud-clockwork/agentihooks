import re

from hooks.filters.finders import string_literals

_TAIL = re.compile(r"\b(?:because|since|which means|so that)\b|; ", re.IGNORECASE)


def find(text: str, path: str, tool: str) -> list[dict]:
    findings = []
    for literal in string_literals.find(text, path, tool):
        match = _TAIL.search(literal["text"])
        if match:
            start = literal["start"] + match.start()
            end = literal["end"]
            findings.append({"start": start, "end": end, "text": text[start:end], "reason": "explanation tail"})
    return findings
