"""The lab remote a checkout carries: a `lab` remote at AGENTIHOOKS_LAB_REMOTE and a credential helper that hands git
AGENTIHOOKS_LAB_REMOTE_TOKEN as the basic password of the user `lab` when git asks, so no checkout stores the token.
A repository carries it only when the lab URL names the same repository as its origin."""

import re
import subprocess
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

REMOTE = "lab"
URL_ENV = "AGENTIHOOKS_LAB_REMOTE"
TOKEN_ENV = "AGENTIHOOKS_LAB_REMOTE_TOKEN"
HELPER = '!f() { if test "$1" = get; then printf "username=lab\\npassword=%s\\n" "$' + TOKEN_ENV + '"; fi; }; f'

GIT_TIMEOUT_S = 60
RELAYED = re.compile(r"(?:^| )(?:-o ?|--push-option[ =])branch=(\S+)")
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")
SCP = re.compile(r"^[^/:]+:(.*)")

Git = Callable[..., "subprocess.CompletedProcess[str]"]


def _run(path: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)


def at(path: Path) -> Git:
    """Return a runner of git commands in the checkout at path."""
    return partial(_run, path)


def name(url: str) -> str:
    """Return the lowercase repository name a remote URL, scp address or path ends in, without `.git`."""
    return re.split(r"[/:]", url.strip().rstrip("/"))[-1].removesuffix(".git").lower()


def owner(url: str) -> str:
    """Return the lowercase owner a remote URL or scp address names before its repository, or an empty string for a
    local path, a `file://` URL or an address whose repository sits at its root."""
    address = url.strip().lower()
    scheme = SCHEME.match(address)
    if scheme:
        if scheme.group() == "file://":
            return ""
        path = address[scheme.end() :].partition("/")[2]
    elif scp := SCP.match(address):
        path = scp[1]
    else:
        return ""
    segments = [segment for segment in path.split("/") if segment]
    return segments[-2] if len(segments) > 1 else ""


def serves(lab: str, origin: str) -> bool:
    """Return whether the lab URL names a repository and it is the one the origin URL names, owner included when the
    lab URL names an owner."""
    found = name(lab)
    held = owner(lab)
    return bool(found) and found == name(origin) and (not held or held == owner(origin))


def carry(git: Git, environ: Mapping[str, str]) -> bool:
    """Give the repository git runs in the lab remote and its credential helper when the environment names a lab URL
    for the repository its origin names, pointing an existing lab remote at that URL; return whether it applies."""
    url = environ.get(URL_ENV, "")
    if not serves(url, git("config", "remote.origin.url").stdout):
        return False
    verb = "set-url" if git("config", f"remote.{REMOTE}.url").returncode == 0 else "add"
    git("remote", verb, REMOTE, url)
    git("config", f"credential.{url}.helper", HELPER)
    return True


def relayed(args: list[str]) -> list[str]:
    """Return the branches a lab push's `branch=<name>` push options hand the relay to write once its grade is green."""
    return RELAYED.findall(" ".join(args))
