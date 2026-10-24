import json
import re
import sys

_CUES = (
    (r"\b[A-Za-z][\w.-]*@[A-Za-z0-9][\w.-]*", "agent or seat name"),
    (r"\b(?:engineer|master|planner|dispatcher|cicd)s?\b", "profile or role name"),
    (r"\b(?:opus|sonnet|haiku|codex|gpt-[\w.-]*\w)\b", "model name"),
    (
        r"\b(?:iteration|round|attempt|try)\s*#?\s*\d+\b|\b\d+(?:st|nd|rd|th)\s+(?:iteration|round|attempt|try)\b",
        "attempt number",
    ),
    (r"\b(?:passed|retry|enhance|approved|rejected)\b", "earlier verdict"),
    (r"\b(?:clearly|perfectly|robust|comprehensive)\b|\bI verified\b", "confident wording"),
)
_PATTERNS = [(re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in _CUES]


def find(text: str) -> list[dict]:
    spans = sorted(
        ((match.start(), match.end(), reason) for pattern, reason in _PATTERNS for match in pattern.finditer(text)),
        key=lambda span: (span[0], -span[1]),
    )
    found, last = [], 0
    for start, end, reason in spans:
        if start >= last:
            found.append({"start": start, "end": end, "text": text[start:end], "reason": reason})
            last = end
    return found


if __name__ == "__main__":
    json.dump(find(json.load(sys.stdin)["text"]), sys.stdout)
