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


def chunk_body(text: str, *, window: int = 1200) -> list[str]:
    if window <= 0:
        raise ValueError("window must be positive")
    body = text.strip()
    if not body:
        return []
    if len(body) <= window:
        return [body]
    sections = re.split(r"(?=^#{1,6}[ \t])", body, flags=re.MULTILINE)
    return [chunk for section in sections for chunk in _windows(section, window)]
