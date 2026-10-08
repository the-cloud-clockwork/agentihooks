import re


def _windows(section: str, window: int) -> list[str]:
    chunks = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", section.strip()):
        for start in range(0, len(paragraph), window):
            piece = paragraph[start : start + window]
            combined = f"{current}\n\n{piece}" if current else piece
            if len(combined) > window:
                chunks.append(current)
                current = piece
            else:
                current = combined
    if current:
        chunks.append(current)
    return chunks


def _sections(body: str) -> list[str]:
    sections = []
    current = []
    fence = ""
    for line in body.splitlines(keepends=True):
        if match := re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line):
            marker, tail = match.groups()
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not tail.strip():
                fence = ""
        elif not fence and re.match(r"^#{1,6}[ \t]", line) and current:
            sections.append("".join(current))
            current = []
        current.append(line)
    sections.append("".join(current))
    return sections


def chunk_body(text: str, *, window: int = 1200) -> list[str]:
    if window <= 0:
        raise ValueError("window must be positive")
    body = text.strip()
    if not body:
        return []
    if len(body) <= window:
        return [body]
    sections = _sections(body)
    return [chunk for section in sections for chunk in _windows(section, window)]
