"""Secrets detection and redaction for Claude Code hooks.

Scans text for credential patterns and redacts them to prevent
secrets from appearing in logs or being processed by Claude.

Supports tiered pattern sets controlled by ``AGENTIHOOKS_SECRETS_MODE``:
  off      → no scanning
  warn     → standard patterns (7), scan only (caller decides action)
  standard → standard patterns (7), scan + block
  strict   → standard + extended patterns (Slack/Stripe/JWT/Bearer), scan + block
"""

import re
from typing import Callable, Iterator, NamedTuple


class _Pattern(NamedTuple):
    name: str
    regex: re.Pattern
    accept: Callable[[re.Match], bool] | None = None

    def matches(self, text: str) -> list[re.Match]:
        return [m for m in self.regex.finditer(text) if self.accept is None or self.accept(m)]


_NOSECRET_RE = re.compile(r"#\s*nosecret\b", re.IGNORECASE)

_CODE_EXPRESSION_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$]|\(|\[)")
_SECRET_REFERENCE_RE = re.compile(
    r"/?[a-z][a-z0-9_.-]*+(?:/[a-z][a-z0-9_.-]*+)++"
    r"|arn:(?:aws|aws-cn|aws-us-gov):"
    r"(?:(?P<manager>secretsmanager)|ssm):"
    r"[a-z]{2}(?:-[a-z]++)++-[0-9]++:[0-9]{12}:"
    r"(?(manager)sec"
    r"ret:[A-Za-z0-9/_+=.@-]++|parameter/[A-Za-z0-9_./-]++)"
)


def _is_literal_secret(match: re.Match[str]) -> bool:
    if match.group("reference") is not None:
        return False
    quoted = match.group("quoted") or match.group("json_quoted")
    quote = match.group("quote") or match.group("json_quote")
    if quoted is not None and match.string[match.end() : match.end() + 1] == quote:
        return _SECRET_REFERENCE_RE.fullmatch(quoted) is None
    bare = match.group("bare")
    return bare is None or not _CODE_EXPRESSION_RE.match(bare)


_STANDARD_PATTERNS: list[_Pattern] = [
    _Pattern(
        "aws_access_key",
        re.compile(r"(AKIA|ASIA|AROA|AIPA)[A-Z0-9]{16}"),
    ),
    _Pattern(
        "aws_secret_key",
        re.compile(r"aws_secret_access_key\s*[=:]\s*[A-Za-z0-9+/]{40}", re.IGNORECASE),
    ),
    _Pattern(
        "github_token",
        re.compile(r"gh[ps]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{82}"),
    ),
    _Pattern(
        "private_key",
        re.compile(
            r"-----BEGIN\s+((?:[A-Z0-9]+\s+)?)PRIVATE KEY-----(?:.*?-----END\s+\1PRIVATE KEY-----|.*)",
            re.DOTALL,
        ),
    ),
    _Pattern(
        "bearer_token",
        re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9._\-+/]{20,}", re.IGNORECASE),
    ),
    _Pattern(
        "db_url_creds",
        re.compile(r"(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|rediss?)://[^/\s:@]*:[^@\s]+@", re.IGNORECASE),
    ),
    _Pattern(
        "generic_secret",
        re.compile(
            rf"""(?P<reference_quote>["'])(?P<reference>(?-i:{_SECRET_REFERENCE_RE.pattern}))(?P=reference_quote)(?!\s*:)|"""
            r"(?:PASSWORD|(?<!/)PASSWD|SECRET|API_KEY|PRIVATE_KEY|ACCESS_TOKEN)(?:\s*(?:=(?!=)|:)\s*"
            r"""(?:(?P<quote>["'])(?P<quoted>[^\s"'$<{]{8,})|(?P<bare>[^\s"'$<{]{8,}))"""
            r"""|["']\s*:\s*(?P<json_quote>["'])(?P<json_quoted>[^\s"'$<{]{8,}))""",
            re.IGNORECASE,
        ),
        _is_literal_secret,
    ),
]

_STRICT_PATTERNS: list[_Pattern] = [
    _Pattern(
        "nested_secret",
        re.compile(
            r"""(?:PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY|ACCESS_TOKEN)["']\s*:\s*\{[^{}]*"""
            r"""["'](?:VALUE|PASSWORD|PASSWD|TOKEN|SECRET)["']\s*:\s*["'][^\s"'$<{]{8,}""",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        "slack_token",
        re.compile(r"xox[bpors]-[A-Za-z0-9\-]{10,}"),
    ),
    _Pattern(
        "stripe_key",
        re.compile(r"[sr]k_(live|test)_[A-Za-z0-9]{20,}"),
    ),
    _Pattern(
        "jwt_token",
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ),
    _Pattern(
        "bearer_value",
        re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/]{20,}", re.IGNORECASE),
    ),
]


_MEMORY_PATTERNS: list[_Pattern] = [
    _Pattern(
        "secret_assignment",
        re.compile(
            r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|ACCESS_KEY)(?:[^\S\r\n]*(?:=(?!=)|:)[^\S\r\n]*"
            r"""(?:["']?[^\s"'$<{]+)|["'][^\S\r\n]*:[^\S\r\n]*["'][^\s"'$<{]+)""",
            re.IGNORECASE,
        ),
    ),
]


def _get_patterns(mode: str) -> list[_Pattern]:
    """Return the pattern list for the given secrets mode."""
    if mode == "off":
        return []
    if mode == "memory":
        return (
            [pattern for pattern in _STANDARD_PATTERNS if pattern.name != "generic_secret"]
            + _STRICT_PATTERNS
            + _MEMORY_PATTERNS
        )
    if mode == "strict":
        return _STANDARD_PATTERNS + _STRICT_PATTERNS
    return _STANDARD_PATTERNS


def scan(text: str, *, mode: str | None = None) -> list[str]:
    """Return list of matched pattern names found in text.

    Lines containing '# nosecret' (case-insensitive) are excluded from scanning,
    allowing intentional suppression of known-safe patterns (e.g. docs, tests).

    Args:
        text: The text to scan for secrets.
        mode: Secrets mode override. If None, reads ``config.SECRETS_MODE``.
    """
    if mode is None:
        from hooks.config import SECRETS_MODE

        mode = SECRETS_MODE

    patterns = _get_patterns(mode)
    if not patterns:
        return []

    filtered = "".join(line for line in text.splitlines(keepends=True) if not _NOSECRET_RE.search(line))
    hits: list[str] = []
    for pattern in patterns:
        if pattern.matches(filtered):
            hits.append(pattern.name)
    return hits


def iter_strings(obj: object) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from iter_strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from iter_strings(value)


def redact(text: str, *, mode: str | None = None) -> str:
    """Replace detected secrets with [REDACTED:<name>].

    Args:
        text: The text to redact.
        mode: Secrets mode override. If None, reads ``config.SECRETS_MODE``.
    """
    if mode is None:
        from hooks.config import SECRETS_MODE

        mode = SECRETS_MODE

    patterns = _get_patterns(mode)
    for pattern in patterns:
        for m in reversed(pattern.matches(text)):
            text = f"{text[: m.start()]}[REDACTED:{pattern.name}]{text[m.end() :]}"
    return text


def contains_generic_secret(text: str) -> bool:
    return any(pattern.matches(text) for pattern in _STANDARD_PATTERNS if pattern.name == "generic_secret")
