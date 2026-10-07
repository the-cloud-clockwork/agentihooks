import argparse
import ipaddress
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "swarm-v2-baseline/1"
GIT_TIMEOUT = 30
COMMAND_TIMEOUT = 30
HTTP_TIMEOUT = 5
REASON_LIMIT = 200
KUBECTL_READS = frozenset({"get", "describe", "version"})
KUBECTL_VALUE_FLAGS = frozenset({"--context", "-n", "--namespace", "--kubeconfig", "--cluster", "--user"})
VERSION_COMMANDS = frozenset({("agentihooks", "--version"), ("git", "--version")})
REGENERATE = (
    "python -m scripts.swarm_v2.baseline --sources docs/swarm-v2/baseline-sources.json"
    " --previous docs/swarm-v2/baseline.json --json docs/swarm-v2/baseline.json --markdown docs/swarm-v2/baseline.md"
)

_REDACTIONS = (
    (re.compile(r"(Bearer\s+)\S+"), r"\1<redacted>"),
    (re.compile(r"((?:token|password|secret|api_?key)\s*[:=]\s*)[^\s&]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"\b(?:gh[oprsu]_|github_pat_|glpat-|sk-|xox[abp]-)[A-Za-z0-9_-]{8,}"), "<redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted>"),
    (re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+"), "<redacted-url>"),
    (re.compile(r"\b[\w.-]+@(?!sha\d+:)[\w-]+(?:\.[\w-]+)*:[^\s\"'<>]+"), "<redacted-url>"),
    (re.compile(r"\b(?=[\w.-]*[A-Za-z])[\w-]+(?:\.[\w-]+)*:\d{2,5}\b"), "<redacted-host>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<redacted-ip>"),
)
_IPV6 = re.compile(r"(?<![\w:])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*(?![\w:])")


@dataclass(frozen=True)
class Probe:
    name: str
    kind: str
    argv: tuple[str, ...] = ()
    url: str = ""
    url_env: str = ""
    path: str = ""
    field: str = ""
    unique: bool = False
    revision: bool = False


@dataclass(frozen=True)
class Repository:
    name: str
    url: str
    probes: tuple[Probe, ...]
    unknown_live: tuple[str, ...]
    checkout: str = ""
    interfaces: tuple[str, ...] = ()


@dataclass(frozen=True)
class Sources:
    branch: str
    repositories: tuple[Repository, ...]


def _ipv6(match: re.Match) -> str:
    try:
        ipaddress.IPv6Address(match.group())
    except ValueError:
        return match.group()
    return "<redacted-ip>"


def sanitize(text: str) -> str:
    text = _IPV6.sub(_ipv6, text)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _repo_url(url: str, base: Path) -> str:
    if "://" in url or url.startswith("git@"):
        return url
    return str(base / Path(url).expanduser()) if url else ""


def _kubectl_verb(args: tuple[str, ...]) -> str:
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in KUBECTL_VALUE_FLAGS:
            skip = True
        elif not arg.startswith("-"):
            return arg
        elif "=" not in arg:
            return ""
    return ""


def read_only(argv: tuple[str, ...]) -> bool:
    if argv in VERSION_COMMANDS:
        return True
    return argv[:1] == ("kubectl",) and _kubectl_verb(argv[1:]) in KUBECTL_READS


def _probe(raw: dict) -> Probe:
    probe = Probe(**{**raw, "argv": tuple(raw.get("argv", ()))})
    if probe.kind == "command" and not read_only(probe.argv):
        raise ValueError(f"probe {probe.name} is not a read-only command")
    return probe


def load_sources(path: Path | str) -> Sources:
    path = Path(path)
    data = json.loads(path.read_text())
    repositories = tuple(
        Repository(
            name=repo["name"],
            url=_repo_url(repo["url"], path.parent),
            probes=tuple(_probe(p) for p in repo.get("probes", ())),
            unknown_live=tuple(repo.get("unknown_live", ())),
            checkout=_repo_url(repo.get("checkout", ""), path.parent),
            interfaces=tuple(repo.get("interfaces", ())),
        )
        for repo in data["repositories"]
    )
    return Sources(branch=data["branch"], repositories=repositories)


def _unresolved(branch: str, reason: str) -> dict:
    return {"branch": branch, "status": "unresolved", "commit": None, "reason": reason}


def resolve_head(url: str, branch: str) -> dict:
    ref = f"refs/heads/{branch}"
    try:
        proc = subprocess.run(
            ["git", "ls-remote", url, ref], capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        return _unresolved(branch, "timed out")
    except FileNotFoundError:
        return _unresolved(branch, "git not found")
    if proc.returncode:
        return _unresolved(branch, f"exit {proc.returncode}: {sanitize(proc.stderr.strip())[:REASON_LIMIT]}")
    for line in proc.stdout.splitlines():
        commit, _, name = line.partition("\t")
        if name == ref:
            return {"branch": branch, "status": "resolved", "commit": commit}
    return _unresolved(branch, f"{ref} not found")


def _unverified(probe: Probe, reason: str) -> dict:
    return {"name": probe.name, "status": "unverified", "value": None, "reason": reason}


def _verified(probe: Probe, value: str) -> dict:
    if probe.unique:
        value = " ".join(sorted(set(value.split())))
    if not value:
        return _unverified(probe, "empty answer")
    return {"name": probe.name, "status": "verified", "value": sanitize(value)}


def _run_command(probe: Probe) -> dict:
    try:
        proc = subprocess.run(list(probe.argv), capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False)
    except FileNotFoundError:
        return _unverified(probe, "command not found")
    except subprocess.TimeoutExpired:
        return _unverified(probe, "timed out")
    if proc.returncode:
        return _unverified(probe, f"exit {proc.returncode}: {sanitize(proc.stderr.strip())[:REASON_LIMIT]}")
    return _verified(probe, proc.stdout.strip())


def _read_http(probe: Probe) -> dict:
    base = os.environ.get(probe.url_env, "") if probe.url_env else probe.url
    if not base:
        return _unverified(probe, f"{probe.url_env} is not set")
    try:
        with urllib.request.urlopen(base + probe.path, timeout=HTTP_TIMEOUT) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return _unverified(probe, f"http {exc.code}")
    except OSError as exc:
        return _unverified(probe, f"unreachable ({type(exc).__name__})")
    except ValueError:
        return _unverified(probe, "answer is not JSON")
    value = body.get(probe.field) if isinstance(body, dict) else None
    if value is None:
        return _unverified(probe, f"endpoint answered without {probe.field}")
    return _verified(probe, str(value))


_PROBES: dict[str, Callable[[Probe], dict]] = {"command": _run_command, "http": _read_http}


def observe(probe: Probe, source: dict) -> dict:
    reader = _PROBES.get(probe.kind)
    result = reader(probe) if reader else _unverified(probe, f"unsupported probe kind {probe.kind}")
    if probe.revision and result["status"] == "verified" and source["commit"]:
        result["matches_source"] = set(result["value"].split()) == {source["commit"]}
    return result


def _git_ok(checkout: str, *args: str) -> bool | None:
    try:
        proc = subprocess.run(
            ["git", "-C", checkout, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.returncode == 0


def _interface(checkout: str, commit: str, path: str) -> dict:
    found = _git_ok(checkout, "cat-file", "-e", f"{commit}:{path}")
    if found is None:
        return {"path": path, "status": "unverified", "reason": "git did not answer"}
    return {"path": path, "status": "present" if found else "missing"}


def check_interfaces(repo: Repository, commit: str | None) -> list[dict]:
    if not repo.interfaces:
        return []
    reason = ""
    if not commit:
        reason = "source head unresolved"
    elif not repo.checkout:
        reason = "no local checkout configured"
    else:
        found = _git_ok(repo.checkout, "cat-file", "-e", f"{commit}^{{commit}}")
        reason = {None: "git did not answer", False: "source head not in local checkout"}.get(found, "")
    if reason:
        return [{"path": path, "status": "unverified", "reason": reason} for path in repo.interfaces]
    return [_interface(repo.checkout, commit, path) for path in repo.interfaces]


def _observe_repo(repo: Repository, branch: str) -> dict:
    source = resolve_head(repo.url, branch)
    deployed = [observe(probe, source) for probe in repo.probes]
    unknown = list(repo.unknown_live) + [p["name"] for p in deployed if p["status"] == "unverified"]
    interfaces = check_interfaces(repo, source["commit"])
    return {
        "repo": repo.name,
        "source": source,
        "interfaces": interfaces,
        "deployed": deployed,
        "unknown_live": unknown,
    }


def _drift(previous: dict | None, repositories: list[dict], observed_at: str) -> list[dict]:
    if not previous:
        return []
    before = {r["repo"]: r["source"].get("commit") for r in previous.get("repositories", ())}
    entries = []
    for repo in repositories:
        old, new = before.get(repo["repo"]), repo["source"]["commit"]
        if old and new and old != new:
            entries.append({"repo": repo["repo"], "from": old, "to": new, "observed_at": observed_at})
    return entries


def _unverified_count(repositories: list[dict]) -> int:
    sources = sum(1 for r in repositories if r["source"]["status"] != "resolved")
    probes = sum(1 for r in repositories for p in r["deployed"] if p["status"] != "verified")
    interfaces = sum(1 for r in repositories for i in r["interfaces"] if i["status"] == "unverified")
    return sources + probes + interfaces


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(sources: Sources, previous: dict | None = None, now: Callable[[], str] | None = None) -> dict:
    observed_at = (now or _utc_now)()
    repositories = [_observe_repo(repo, sources.branch) for repo in sources.repositories]
    new_drift = _drift(previous, repositories, observed_at)
    history = list(previous.get("drift", ())) if previous else []
    return {
        "schema": SCHEMA,
        "observed_at": observed_at,
        "repositories": repositories,
        "drift": history + new_drift,
        "measurements": {
            "baseline_unverified_items": _unverified_count(repositories),
            "baseline_drift_items": len(new_drift),
        },
    }


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _source_cell(source: dict) -> str:
    if source["status"] == "resolved":
        return f"{source['branch']} `{source['commit']}`"
    return _cell(f"{source['branch']} unresolved ({source['reason']})")


def _deployed_cell(deployed: list[dict]) -> str:
    parts = []
    for probe in deployed:
        if probe["status"] == "verified":
            match = {True: ", matches source", False: ", differs from source"}.get(probe.get("matches_source"), "")
            parts.append(f"{probe['name']}: verified `{probe['value']}`{match}")
        else:
            parts.append(f"{probe['name']}: unverified ({probe['reason']})")
    return _cell("; ".join(parts)) if parts else "none probed"


def render_markdown(report: dict) -> str:
    lines = [
        "# Swarm v2 implementation baseline",
        "",
        f"Package SV2-FND-01. Observed at {report['observed_at']}.",
        "Source is the branch head. Deployed values come from read-only probes.",
        "An unverified value is unknown: it never means empty, absent or zero.",
        "baseline_drift_items counts drift found by this run; the Drift section keeps every earlier entry.",
        f"Regenerate with `{REGENERATE}`.",
        "",
        "| Repository | Source | Deployed | Unknown live values |",
        "|---|---|---|---|",
    ]
    for repo in report["repositories"]:
        unknown = _cell(", ".join(repo["unknown_live"])) or "none"
        lines.append(
            f"| {repo['repo']} | {_source_cell(repo['source'])} | {_deployed_cell(repo['deployed'])} | {unknown} |"
        )
    lines += ["", "## Source-proven interfaces", ""]
    for repo in report["repositories"]:
        for item in repo["interfaces"]:
            reason = f" ({item['reason']})" if "reason" in item else ""
            lines.append(f"- {repo['repo']} `{item['path']}`: {item['status']}{reason}")
    if lines[-1] == "":
        lines.append("None configured.")
    lines += ["", "## Drift", ""]
    lines += [f"- {d['observed_at']} {d['repo']}: `{d['from']}` to `{d['to']}`" for d in report["drift"]] or [
        "None recorded."
    ]
    lines += ["", "## Measurements", ""]
    lines += [f"- {key}: {value}" for key, value in report["measurements"].items()]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record Swarm v2 source and deployment baselines read-only.")
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args(argv)
    previous = json.loads(args.previous.read_text()) if args.previous else None
    current = json.loads(args.json.read_text()) if args.json.exists() else None
    if current and (previous is None or previous["observed_at"] < current["observed_at"]):
        parser.error(f"{args.json} holds a newer baseline than --previous; pass it as --previous")
    report = collect(load_sources(args.sources), previous=previous)
    _write(args.json, json.dumps(report, indent=2) + "\n")
    _write(args.markdown, render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
