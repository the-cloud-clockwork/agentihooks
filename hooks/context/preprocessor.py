"""Context Preprocessor — token compression for mid-session context refresh.

Compresses rule files and CLAUDE.md content before re-injection to fit more
content within the CONTEXT_REFRESH_MAX_CHARS budget. Leverages the fact that
LLMs predict over subword tokens — abbreviated forms activate the same
semantic representations as full words.

Four compression levels:
    0 (off)        — passthrough
    1 (light)      — strip markdown formatting
    2 (standard)   — level 1 + remove filler words + apply abbreviation dict
    3 (aggressive) — level 2 + internal vowel removal on long words

Safety: a protection mask identifies spans that must never be modified
(negation words, action verbs, code blocks, identifiers, paths, numbers).

Public API:
    preprocess(text, level) → str
    compression_ratio(original, compressed) → float
    get_level_from_config() → int
"""

import json
import os
import re
from pathlib import Path

from hooks.common import log

# ---------------------------------------------------------------------------
# Compression levels
# ---------------------------------------------------------------------------


class CompressionLevel(int):
    NONE = 0
    LIGHT = 1
    STANDARD = 2
    AGGRESSIVE = 3


_MODE_TO_LEVEL = {
    "off": CompressionLevel.NONE,
    "light": CompressionLevel.LIGHT,
    "standard": CompressionLevel.STANDARD,
    "aggressive": CompressionLevel.AGGRESSIVE,
}


def get_level_from_config() -> int:
    from hooks.config import CONTEXT_REFRESH_COMPRESSION

    return _MODE_TO_LEVEL.get(CONTEXT_REFRESH_COMPRESSION, CompressionLevel.NONE)


def compression_ratio(original: str, compressed: str) -> float:
    if not original:
        return 1.0
    return len(compressed) / len(original)


# ---------------------------------------------------------------------------
# Protection mask
# ---------------------------------------------------------------------------

# Code blocks (fenced and inline)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# Negation words
_NEGATION_WORDS = frozenset({
    "never", "don't", "dont", "not", "no", "without",
    "cannot", "can't", "cant", "won't", "wont",
    "shouldn't", "shouldnt",
})

# Assertion words
_ASSERTION_WORDS = frozenset({
    "always", "must", "required", "mandatory",
    "only", "exactly", "strictly",
})

# Action verbs — high-stakes operations
_ACTION_VERBS = frozenset({
    "push", "delete", "commit", "deploy", "block",
    "destroy", "drop", "truncate", "kill", "terminate",
    "rollback", "revert", "reset", "force", "override",
    "disable", "remove", "detach", "purge", "wipe",
})

# Combined word set for single-pass matching
_PROTECTED_WORDS = _NEGATION_WORDS | _ASSERTION_WORDS | _ACTION_VERBS

# Two-word negation phrases
_TWO_WORD_PHRASES = ["must not", "do not", "must never", "do never"]

# ALL_CAPS identifiers (env var names)
_CAPS_ID_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")

# Numbers and thresholds
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?(?:[kKmMgGtT]i?[bB]?)?\b|\b\d+%\b")

# File paths
_PATH_RE = re.compile(r"(?:^|\s)(\.{0,2}/[\w./-]+|~/[\w./-]+)", re.MULTILINE)

# CLI subcommands (tool + first arg)
_CLI_TOOLS = (
    "kubectl", "helm", "terraform", "aws", "gcloud",
    "docker", "git", "npm", "pip", "cargo", "scp", "ssh",
)
_CLI_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _CLI_TOOLS) + r")\s+\w+",
    re.IGNORECASE,
)


def _build_protection_mask(text: str) -> list[tuple[int, int]]:
    """Return list of (start, end) char spans that must not be modified."""
    spans: list[tuple[int, int]] = []

    # Code blocks (highest priority — most content is protected here)
    for m in _CODE_FENCE_RE.finditer(text):
        spans.append((m.start(), m.end()))
    for m in _INLINE_CODE_RE.finditer(text):
        spans.append((m.start(), m.end()))

    # Two-word phrases
    for phrase in _TWO_WORD_PHRASES:
        for m in re.finditer(re.escape(phrase), text, re.IGNORECASE):
            spans.append((m.start(), m.end()))

    # Protected single words
    for m in re.finditer(r"\b\w+\b", text):
        if m.group().lower() in _PROTECTED_WORDS:
            spans.append((m.start(), m.end()))

    # ALL_CAPS identifiers
    for m in _CAPS_ID_RE.finditer(text):
        spans.append((m.start(), m.end()))

    # Numbers
    for m in _NUMBER_RE.finditer(text):
        spans.append((m.start(), m.end()))

    # File paths
    for m in _PATH_RE.finditer(text):
        spans.append((m.start(1) if m.lastindex else m.start(), m.end()))

    # CLI subcommands
    for m in _CLI_RE.finditer(text):
        spans.append((m.start(), m.end()))

    # Merge overlapping spans
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    sorted_spans = sorted(spans)
    merged = [sorted_spans[0]]
    for start, end in sorted_spans[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlaps_mask(start: int, end: int, mask: list[tuple[int, int]]) -> bool:
    for ms, me in mask:
        if start < me and end > ms:
            return True
    return False


# ---------------------------------------------------------------------------
# Level 1: Markdown formatting removal
# ---------------------------------------------------------------------------

_MERMAID_RE = re.compile(r"```mermaid[\s\S]*?```", re.DOTALL)
_HEADER_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_TABLE_SEP_RE = re.compile(r"^\s*\|[-:\s|]+\|\s*$", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$", re.MULTILINE)


def _strip_mermaid_blocks(text: str, mask: list[tuple[int, int]]) -> str:
    def _replace(m: re.Match) -> str:
        if _overlaps_mask(m.start(), m.end(), mask):
            return m.group()
        return "[diagram removed]"
    return _MERMAID_RE.sub(_replace, text)


def _strip_headers(text: str, mask: list[tuple[int, int]]) -> str:
    def _replace(m: re.Match) -> str:
        if _overlaps_mask(m.start(), m.end(), mask):
            return m.group()
        return f"[{m.group(1).strip()}]"
    return _HEADER_RE.sub(_replace, text)


def _strip_horizontal_rules(text: str, mask: list[tuple[int, int]]) -> str:
    def _replace(m: re.Match) -> str:
        if _overlaps_mask(m.start(), m.end(), mask):
            return m.group()
        return ""
    return _HR_RE.sub(_replace, text)


def _strip_bold_italic(text: str, mask: list[tuple[int, int]]) -> str:
    def _replace_bold(m: re.Match) -> str:
        if _overlaps_mask(m.start(), m.end(), mask):
            return m.group()
        return m.group(1)

    def _replace_italic(m: re.Match) -> str:
        if _overlaps_mask(m.start(), m.end(), mask):
            return m.group()
        return m.group(1)

    text = _BOLD_RE.sub(_replace_bold, text)
    text = _ITALIC_RE.sub(_replace_italic, text)
    return text


def _strip_markdown_tables(text: str, mask: list[tuple[int, int]]) -> str:
    """Convert markdown tables to flat key: value format."""
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect table: line matches |...|, next line is separator |---|
        if (
            re.match(r"^\s*\|.+\|", line)
            and i + 1 < len(lines)
            and re.match(r"^\s*\|[-:\s|]+\|", lines[i + 1])
        ):
            # Check if this table block overlaps a protected span
            table_start = sum(len(l) + 1 for l in lines[:i])
            if _overlaps_mask(table_start, table_start + len(line), mask):
                result.append(line)
                i += 1
                continue

            # Parse header
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2  # skip header + separator

            # Parse data rows
            while i < len(lines) and re.match(r"^\s*\|.+\|", lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                pairs = []
                for h, c in zip(headers, cells):
                    if h and c:
                        pairs.append(f"{h}: {c}")
                if pairs:
                    result.append(" | ".join(pairs))
                i += 1
        else:
            result.append(line)
            i += 1

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Level 2: Filler words and abbreviations
# ---------------------------------------------------------------------------

_FILLER_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "in", "on", "at", "to", "of", "for", "that", "which", "with",
})

# Build filler regex — word boundary, case insensitive, only when surrounded by spaces
_FILLER_RE = re.compile(
    r"(?<=\s)(?:" + "|".join(re.escape(w) for w in sorted(_FILLER_WORDS, key=len, reverse=True)) + r")(?=\s)",
    re.IGNORECASE,
)

# Abbreviation dictionary — loaded once, cached
_abbrev_cache: dict[str, str] | None = None


def _load_abbrev_dict() -> dict[str, str]:
    global _abbrev_cache
    if _abbrev_cache is not None:
        return _abbrev_cache

    entries: dict[str, str] = {}

    # Built-in dictionary
    built_in = Path(__file__).parent / "data" / "abbreviations.json"
    try:
        if built_in.is_file():
            data = json.loads(built_in.read_text())
            entries.update(data.get("entries", {}))
    except Exception:
        pass

    # User override
    user_path = os.getenv("CONTEXT_REFRESH_ABBREV_FILE", "")
    if user_path:
        try:
            user_data = json.loads(Path(user_path).read_text())
            entries.update(user_data.get("entries", {}))
        except Exception:
            pass

    _abbrev_cache = entries
    return entries


def _remove_filler_words(text: str, mask: list[tuple[int, int]]) -> str:
    def _replace(m: re.Match) -> str:
        if _overlaps_mask(m.start(), m.end(), mask):
            return m.group()
        return ""
    result = _FILLER_RE.sub(_replace, text)
    # Clean up double spaces left by removal
    return re.sub(r"  +", " ", result)


def _apply_abbreviations(text: str, mask: list[tuple[int, int]], abbrev_dict: dict[str, str]) -> str:
    if not abbrev_dict:
        return text

    # Sort by key length descending for longest-match-first
    sorted_entries = sorted(abbrev_dict.items(), key=lambda x: len(x[0]), reverse=True)

    for full, short in sorted_entries:
        pattern = re.compile(r"\b" + re.escape(full) + r"\b", re.IGNORECASE)

        def _make_replacer(short_form: str):
            def _replace(m: re.Match) -> str:
                if _overlaps_mask(m.start(), m.end(), mask):
                    return m.group()
                # Preserve case of first letter
                if m.group()[0].isupper():
                    return short_form[0].upper() + short_form[1:]
                return short_form
            return _replace

        text = pattern.sub(_make_replacer(short), text)
        # Rebuild mask after each substitution since offsets changed
        mask = _build_protection_mask(text)

    return text


# ---------------------------------------------------------------------------
# Level 3: Internal vowel removal (disemvoweling)
# ---------------------------------------------------------------------------

_MIN_WORD_LENGTH = 7

_DISEMVOWEL_EXCLUSIONS = frozenset({
    "often", "even", "open", "over", "idea", "area", "issue",
    "error", "user", "order", "offer", "later", "after",
    "config", "token", "debug", "setup", "output", "input",
    "under", "upper", "lower", "outer", "inner", "other",
})

# Match internal vowels flanked by consonants on both sides
_INTERNAL_VOWEL_RE = re.compile(
    r"(?<=[bcdfghjklmnpqrstvwxyz])[aeiou]+(?=[bcdfghjklmnpqrstvwxyz])",
    re.IGNORECASE,
)


def _remove_internal_vowels(text: str, mask: list[tuple[int, int]]) -> str:
    def _process_word(m: re.Match) -> str:
        word = m.group()
        if len(word) < _MIN_WORD_LENGTH:
            return word
        if word.lower() in _DISEMVOWEL_EXCLUSIONS:
            return word
        if _overlaps_mask(m.start(), m.end(), mask):
            return word
        return _INTERNAL_VOWEL_RE.sub("", word)

    return re.sub(r"\b[a-zA-Z]+\b", _process_word, text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def preprocess(text: str, level: int = CompressionLevel.NONE) -> str:
    """Apply compression pipeline to text at the specified level.

    Returns compressed text. At level 0, returns input unchanged.
    Never raises — falls back to input on any internal error.
    """
    if not text or level == CompressionLevel.NONE:
        return text

    try:
        # Strip mermaid BEFORE building protection mask (otherwise code fence regex protects them)
        if level >= CompressionLevel.LIGHT:
            text = _MERMAID_RE.sub("[diagram removed]", text)

        mask = _build_protection_mask(text)

        if level >= CompressionLevel.LIGHT:
            text = _strip_markdown_tables(text, mask)
            mask = _build_protection_mask(text)
            text = _strip_headers(text, mask)
            mask = _build_protection_mask(text)
            text = _strip_horizontal_rules(text, mask)
            mask = _build_protection_mask(text)
            text = _strip_bold_italic(text, mask)
            mask = _build_protection_mask(text)

        if level >= CompressionLevel.STANDARD:
            text = _remove_filler_words(text, mask)
            mask = _build_protection_mask(text)
            abbrevs = _load_abbrev_dict()
            text = _apply_abbreviations(text, mask, abbrevs)
            mask = _build_protection_mask(text)

        if level >= CompressionLevel.AGGRESSIVE:
            text = _remove_internal_vowels(text, mask)

        # Clean up excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text

    except Exception as e:
        log("preprocessor: error, returning original", {"error": str(e)})
        return text
