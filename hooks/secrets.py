"""Secrets detection and redaction for Claude Code hooks.

Scans text for credential patterns and redacts them to prevent
secrets from appearing in logs or being processed by Claude.

Supports tiered pattern sets controlled by ``AGENTIHOOKS_SECRETS_MODE``:
  off      → no scanning
  warn     → standard patterns (7), scan only (caller decides action)
  standard → standard patterns (7), scan + block
  strict   → standard + extended patterns (Slack/Stripe/JWT), scan + block
"""

import re
from typing import Callable, NamedTuple


class _Pattern(NamedTuple):
    name: str
    regex: re.Pattern
    accept: Callable[[re.Match], bool] | None = None

    def matches(self, text: str) -> list[re.Match]:
        return [m for m in self.regex.finditer(text) if self.accept is None or self.accept(m)]


_NOSECRET_RE = re.compile(r"#\s*nosecret\b", re.IGNORECASE)

# A bare value that is a member access, call or subscript (`settings.x`,
# `get_secret()`, `Optional[str]`) is code reading a secret, not a literal one.
_CODE_EXPRESSION_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$]|\(|\[)")


def _is_literal_secret(match: re.Match) -> bool:
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
        re.compile(r"-----BEGIN\s+(RSA|EC|OPENSSH|PGP)\s+PRIVATE KEY-----"),
    ),
    _Pattern(
        "bearer_token",
        re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9._\-+/]{20,}", re.IGNORECASE),
    ),
    _Pattern(
        "db_url_creds",
        re.compile(r"(postgres|mysql|mongodb)://[^:]+:[^@]{4,}@", re.IGNORECASE),
    ),
    _Pattern(
        "generic_secret",
        # Match KEY = VALUE but skip env var references ($VAR), placeholders (<...> or {...}),
        # comparisons (==, ===), and quoted values under 8 chars such as Terraform time units
        re.compile(
            r"(?:PASSWORD|SECRET|API_KEY|PRIVATE_KEY|ACCESS_TOKEN)\s*(?:=(?!=)|:)\s*"
            r"""(?:["'](?P<quoted>[^\s"'$<{]{8,})|(?P<bare>[^\s"'$<{]{8,}))""",
            re.IGNORECASE,
        ),
        _is_literal_secret,
    ),
]

_STRICT_PATTERNS: list[_Pattern] = [
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
]


def _get_patterns(mode: str) -> list[_Pattern]:
    """Return the pattern list for the given secrets mode."""
    if mode == "off":
        return []
    if mode == "strict":
        return _STANDARD_PATTERNS + _STRICT_PATTERNS
    # "warn" and "standard" both use the standard set
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

    # Strip suppressed lines before pattern matching
    filtered = "".join(line for line in text.splitlines(keepends=True) if not _NOSECRET_RE.search(line))
    hits: list[str] = []
    for pattern in patterns:
        if pattern.matches(filtered):
            hits.append(pattern.name)
    return hits


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
