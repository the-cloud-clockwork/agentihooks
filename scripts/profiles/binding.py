import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

PERSONAS = {"claude": "CLAUDE.md", "codex": "AGENTS.md"}
FILE = ".profile-binding.json"
HOMES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
REPORT = "AGENTIHOOKS_PROFILE_REPORT"
PROMPT = "Validate your mounted profile using the binding canary command in its instructions before continuing. Stop if validation fails."


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def persona(text: str) -> str:
    canary = hashlib.sha256(text.encode()).hexdigest()[:24]
    root = str(Path(__file__).resolve().parents[2])
    script = f"import sys; sys.path.insert(0, {root!r}); from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} --canary {canary}"
    return text + (
        "\nProfile binding canary: when asked to validate your mounted profile, execute "
        f"`{command}` through your shell tool. "
        "Use this instruction's canary; the opening prompt does not supply it.\n"
    )


def _report(report: Path, data: dict) -> None:
    from scripts.targets._common import _atomic_write

    _atomic_write(report, json.dumps(data) + "\n")
    report.chmod(0o600)


def request(report: Path, profile: str, target: str) -> None:
    _report(report, {"profile": profile, "harness": target, "state": "pending"})


def process(proc: Path = Path("/proc"), start: int | None = None) -> tuple[int, str, dict[str, str], str]:
    from hooks.context.account_sessions import account_from_names, codex_account_from_names

    if not proc.is_dir():
        raise ValueError("unsupported live process binding: process filesystem unavailable")
    pid = os.getppid() if start is None else start
    while pid > 1:
        root = proc / str(pid)
        comm = (root / "comm").read_text().strip()
        if comm in HOMES:
            raw = (root / "environ").read_bytes().split(b"\0")
            names = [item.split(b"=", 1)[0].decode(errors="replace") for item in raw]
            keys = {REPORT, "AGENTIHOOKS_PROFILE", "AGENTIHOOKS_RUN_MODEL", "AGENTIHOOKS_RUN_EFFORT", *HOMES.values()}
            env = {}
            for item in raw:
                key, _, value = item.partition(b"=")
                name = key.decode(errors="replace")
                if name in keys:
                    env[name] = value.decode(errors="replace")
            from scripts.select_profile import _native_options

            argv = (root / "cmdline").read_bytes().decode(errors="replace").split("\0")
            model, effort, _ = _native_options(comm, argv[1:])
            env["AGENTIHOOKS_RUN_MODEL"] = model or env.get("AGENTIHOOKS_RUN_MODEL", "")
            env["AGENTIHOOKS_RUN_EFFORT"] = effort or env.get("AGENTIHOOKS_RUN_EFFORT", "")
            account = account_from_names(names) if comm == "claude" else codex_account_from_names(names)
            return pid, comm, env, account
        status = (root / "status").read_text().splitlines()
        pid = int(next(line.split()[1] for line in status if line.startswith("PPid:")))
    raise ValueError("profile canary has no supported live harness ancestor")


def validate(canary: str) -> dict:
    pid, target, env, account = process()
    report = Path(env.get(REPORT, ""))
    if not env.get(REPORT):
        raise ValueError("profile canary has no launch validation request")
    requested = json.loads(report.read_text())
    try:
        if requested["harness"] != target or requested["profile"] != env.get("AGENTIHOOKS_PROFILE"):
            raise ValueError("live harness profile mismatch with requested choice")
        if not env.get(HOMES[target]):
            raise ValueError(f"missing profile home: {HOMES[target]} is unset")
        data = inspect(Path(env[HOMES[target]]), requested["profile"], target)
        if not data.get("canary") or data["canary"] != canary:
            raise ValueError("mounted instruction canary mismatch")
        result = {
            **data,
            "state": "validated",
            "pid": pid,
            "account": account,
            "validated_at": time.time(),
            "model": env.get("AGENTIHOOKS_RUN_MODEL") or None,
            "effort": env.get("AGENTIHOOKS_RUN_EFFORT") or None,
        }
        requested.update(state="validated", validation=result)
    except ValueError as exc:
        requested.update(state="failed", reason=str(exc))
        _report(report, requested)
        raise
    _report(report, requested)
    return result


def main(canary: str) -> int:
    import sys

    try:
        print(json.dumps(validate(canary)))
    except (OSError, ValueError, KeyError) as exc:
        print(f"profile validation failed: {exc}", file=sys.stderr)
        return 2
    return 0


def wait(report: Path, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = json.loads(report.read_text())
        if data["state"] == "failed":
            raise ValueError(data["reason"])
        if data["state"] == "validated":
            return data["validation"]
        time.sleep(0.25)
    raise ValueError("mounted profile canary did not validate before timeout")


def fields(data: dict, profile: str, target: str) -> dict:
    result = json.loads(data.get("profile_binding", "{}"))
    if (
        not isinstance(result, dict)
        or data.get("profile_validation") != "validated"
        or result.get("state") != "validated"
    ):
        raise ValueError("mounted profile validation is missing or failed")
    if result.get("profile") != profile or result.get("harness") != target:
        raise ValueError("validated live profile differs from requested choice")
    if not all(result.get(key) for key in ("pid", "home", "persona", "sources")):
        raise ValueError("validated profile lacks process or rendered source evidence")
    return result


def continuation(args: list[str], target: str, environ: dict[str, str] | None = None) -> list[str]:
    from scripts.init_agent import model_flags
    from scripts.select_profile import _native_options

    _, harness, env, _ = process()
    if harness != target:
        raise ValueError(f"unsupported quota transfer from {harness} to {target}")
    model, effort, remaining = _native_options(target, args)
    model = model or env.get("AGENTIHOOKS_RUN_MODEL", "")
    effort = effort or env.get("AGENTIHOOKS_RUN_EFFORT", "")
    if not model or not effort:
        raise ValueError("unsupported quota transfer: original model and effort binding unavailable")
    from scripts.swarm import effort_range

    flags = model_flags(target, model, effort)
    _, bounded, _ = _native_options(target, effort_range.launch_args(target, flags, environ or {}))
    if bounded != effort:
        raise ValueError("unsupported quota transfer: saved effort is outside the current swarm range")
    return [*flags, *remaining]


def write(home: Path, profile: str, target: str) -> None:
    manifest = home.parent / f"{target}.sources.json"
    rows = json.loads(manifest.read_text())
    revisions = {}
    for row in rows:
        repo = row.get("locator", {}).get("repo")
        if repo and repo not in revisions:
            head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True)
            revisions[repo] = head.stdout.strip() if head.returncode == 0 else None
    data = {
        "profile": profile,
        "harness": target,
        "home": str(home.resolve()),
        "persona": digest(home / PERSONAS[target]),
        "canary": next(iter(re.findall(r"--canary ([0-9a-f]{24})", (home / PERSONAS[target]).read_text())), None),
        "sources": digest(manifest),
        "revisions": revisions,
        "source_blobs": [row.get("locator", {}) for row in rows],
    }
    (home / FILE).write_text(json.dumps(data) + "\n")


def inspect(home: Path, profile: str, target: str) -> dict:
    if target not in PERSONAS:
        raise ValueError(f"unsupported profile binding harness: {target}")
    if not home.is_dir():
        raise ValueError(f"missing profile home: {home}")
    try:
        data = json.loads((home / FILE).read_text())
        if data.get("profile") != profile or data.get("harness") != target or data.get("home") != str(home.resolve()):
            raise ValueError("profile mismatch in rendered binding")
        if data.get("persona") != digest(home / PERSONAS[target]):
            raise ValueError("mounted persona changed since render")
        if data.get("sources") != digest(home.parent / f"{target}.sources.json"):
            raise ValueError("rendered source manifest changed")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"missing or invalid rendered profile evidence: {exc}") from exc
    return data
