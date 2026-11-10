"""Docs sanity scanner: flags internal addresses, host names, personal emails, token shapes and private keywords.

Project agnostic and standard library only, so any repository can copy or fetch it.
Usage: python docs_sanity.py [--keywords-env NAME] [--keywords-file PATH] [--exclude GLOB] PATH...
Keywords are read one per line or comma separated, so the private list lives in a CI secret or a
file outside the repository and is never committed. Every hit prints as file:line: rule, without
the matched text, and any hit exits 1.
"""

import argparse
import codecs
import fnmatch
import ipaddress
import os
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network((bytes(start), prefix))
    for start, prefix in (
        ((10, 0, 0, 0), 8),
        ((172, 16, 0, 0), 12),
        ((192, 168, 0, 0), 16),
        ((100, 64, 0, 0), 10),
        ((169, 254, 0, 0), 16),
    )
)
NOT_ADDRESS = re.compile(r"[^0-9.]+|\.(?![0-9])")
NOT_HOST = re.compile(r"[^\w.-]+|[.-](?!\w)")
NOT_EMAIL = re.compile(r"[^\w.%+@:/-]+")
EXAMPLE_DOMAINS = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|example|test|invalid|localhost)$", re.IGNORECASE)
NOREPLY = re.compile(r"(?:^|[.+-])no-?reply$", re.IGNORECASE)
HOST_SUFFIXES = ("lan", "local", "internal", "intranet", "corp", "localdomain", "home.arpa", "ts.net")
TOKENS = {
    "github-token": re.compile(r"\b(?:(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
}


@dataclass(frozen=True)
class Hit:
    """One flagged line: where it is and which rule raised it."""

    path: str
    line: int
    rule: str


def private_ipv4(text: str) -> bool:
    """Return whether the text holds an IPv4 address in a private, shared or link local range."""
    for token in NOT_ADDRESS.split(text):
        try:
            address = ipaddress.IPv4Address(token)
        except ValueError:
            continue
        if any(address in network for network in PRIVATE_NETWORKS):
            return True
    return False


def real_email(text: str) -> bool:
    """Return whether the text holds an email address outside reserved example domains and no reply mailboxes."""
    return any(_real_address(token) for token in NOT_EMAIL.split(text))


def _real_address(token: str) -> bool:
    """Return whether one token is a real looking email; URL user names and scp style remotes are not."""
    local, at, rest = token.partition("@")
    domain = NOT_HOST.split(rest, maxsplit=1)[0]
    labels = domain.split(".")
    if not at or not local or ":" in local or "/" in local or local == "git":
        return False
    if len(labels) < 2 or not all(labels) or len(labels[-1]) < 2 or not labels[-1].isalpha():
        return False
    return not (
        EXAMPLE_DOMAINS.search(domain) or NOREPLY.search(local) or domain.lower().endswith("noreply.github.com")
    )


def private_host(text: str) -> bool:
    """Return whether the text names a host under a private or internal suffix; dot files and flags are not hosts."""
    for token in NOT_HOST.split(text):
        host = token.lower()
        suffix = next((s for s in HOST_SUFFIXES if host.endswith("." + s)), None)
        if suffix is None or host.startswith((".", "-")):
            continue
        if ("." + host).endswith((".example." + suffix, ".cluster.local")):
            continue
        return True
    return False


def line_rules(text: str, keywords: Iterable[str]) -> list[str]:
    """Return the name of every rule the line breaks, built in rules first, then keywords."""
    rules = [
        name
        for name, broken in (
            ("private-ipv4", private_ipv4(text)),
            ("email", real_email(text)),
            ("private-host", private_host(text)),
        )
        if broken
    ]
    rules += [
        name
        for name, pattern in TOKENS.items()
        if any("EXAMPLE" not in match.group(0) for match in pattern.finditer(text))
    ]
    lowered = text.lower()
    if any(keyword in lowered for keyword in keywords):
        rules.append("keyword")
    return rules


def decode(data: bytes) -> str | None:
    """Return the text of UTF-16 data by its byte order mark or of UTF-8 data, or None for binary data."""
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return codecs.utf_16_decode(data, "replace")[0]
    if b"\0" in data:
        return None
    return data.decode(errors="replace")


def load_keywords(env_name: str | None, file_path: str | None) -> list[str]:
    """Read the private keyword list from an environment variable and a file, lower cased and deduplicated."""
    raw = os.environ.get(env_name, "") if env_name else ""
    if file_path:
        raw += "\n" + Path(file_path).read_bytes().decode()
    words = (word.strip().lower() for word in re.split(r"[\n,]", raw))
    return sorted({word for word in words if word and not word.startswith("#")})


def walk(paths: Iterable[str], excludes: Iterable[str]) -> Iterator[Path]:
    """Yield every regular file under the given paths, sorted, skipping excluded globs."""
    patterns = tuple(excludes)
    for root in paths:
        base = Path(root)
        files = sorted(p for p in base.rglob("*") if p.is_file()) if base.is_dir() else [base]
        for file in files:
            if not any(fnmatch.fnmatch(file.as_posix(), pattern) for pattern in patterns):
                yield file


def scan_file(path: Path, keywords: Iterable[str]) -> list[Hit]:
    """Return every hit in one file; a binary file yields none."""
    text = decode(path.read_bytes())
    if text is None:
        return []
    words = tuple(keywords)
    return [
        Hit(path.as_posix(), number, rule)
        for number, line in enumerate(text.splitlines(), start=1)
        for rule in line_rules(line, words)
    ]


def scan(paths: Iterable[str], keywords: Iterable[str], excludes: Iterable[str] = ()) -> list[Hit]:
    """Return every hit across the given files and directories."""
    words = tuple(keywords)
    return [hit for file in walk(paths, excludes) for hit in scan_file(file, words)]


def main(argv: list[str] | None = None) -> int:
    """Scan the given paths, print each hit as file:line: rule and return 1 on any hit."""
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--keywords-env")
    parser.add_argument("--keywords-file")
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args(argv)
    missing = [path for path in [*args.paths, args.keywords_file] if path and not Path(path).exists()]
    if missing:
        print(f"docs sanity: no such path: {', '.join(missing)}", file=sys.stderr)
        return 2
    keywords = load_keywords(args.keywords_env, args.keywords_file)
    hits = scan(args.paths, keywords, args.exclude)
    for hit in hits:
        print(f"{hit.path}:{hit.line}: {hit.rule}")
    print(f"docs sanity: {len(hits)} hit(s), {len(keywords)} private keyword(s) loaded", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
