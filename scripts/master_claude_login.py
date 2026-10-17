"""Native Claude is the only refresher of the master login; this module never asks for an OAuth grant."""

import fcntl
import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from hooks import config as hooks_config
from hooks.context.account_sessions import API_MARKER, INTERACTIVE_PREFIX, TOKEN_PREFIX
from scripts import aicode_tokens, openbao_kv
from scripts.profiles import binding

ROUTE = "master-login"
FIELD = "master_claude_login"
KEY = "master-claude-login"
LANE = "master"
SETTINGS = ["--settings", '{"forceLoginMethod":"claudeai"}']
LOGIN = ["auth", "login"]
CREDENTIALS = ".credentials.json"
CONFIG = ".claude.json"
ACCOUNT = "oauthAccount"
GRANT = "claudeAiOauth"
MANIFESTS = ("claude.sources.json", "claude.mounts.json")
OWNER = "owner.json"
LOCK = "owner.lock"
CREDS = "agentihooks/master-claude"
MANAGED = Path("/etc/claude-code/managed-settings.json")
SELECTORS = ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR")
REINTRODUCED = (
    "CLAUDE_CODE_OAUTH_",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_VERTEX_",
    "ANTHROPIC_FOUNDRY_",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_",
    TOKEN_PREFIX,
    *SELECTORS,
)
DROPPED = (
    *REINTRODUCED,
    API_MARKER,
    INTERACTIVE_PREFIX,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "DISABLE_GROWTHBOOK",
)
PRIVATE_DIR, PRIVATE_FILE = 0o700, 0o600
NO_LOGIN = "no master claude ai login for swarm {slug}: run agentihooks claude login --master {slug}"
SAME_ACCOUNT = "the master login account {account} is also a worker quota token"
NO_QUOTA = "the master claude ai login needs workers on quota tokens; none is set"
HELD = "the master login of swarm {slug} is held by {owner}, still running"
API_HELPER = "{path} sets apiKeyHelper"
SETS = "{path} sets {names}"
FORCED = "{path} sets forceLoginMethod {value}"
PUBLISH_FAILED = "master login recovery copy not stored: {error}"
RECOVERY_FAILED = "master login recovery copy not read: {error}"


class LoginRefused(RuntimeError):
    pass


def enabled(config, lane: str, harness: str) -> bool:
    return lane == LANE and harness == "claude" and getattr(config, FIELD, None) is True


def route(login: bool) -> list[str]:
    return ["--route", ROUTE] if login else []


def root(slug: str) -> Path:
    return Path(hooks_config.AGENTIHOOKS_HOME) / "master-claude" / slug


def home(slug: str) -> Path:
    return root(slug) / "claude"


def _read(path: Path) -> dict:
    try:
        found = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc))
    path.chmod(PRIVATE_FILE)


def _private(target: Path) -> Path | None:
    path = target / CREDENTIALS
    return path if path.is_file() and not path.is_symlink() else None


def build(slug: str, rendered: Path, report: str | None = None) -> Path:
    target, staging = home(slug), root(slug) / "staging"
    root(slug).mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)
    kept = _private(target)
    grant = kept.read_bytes() if kept else None
    account = _read(target / CONFIG).get(ACCOUNT)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(
        rendered, staging, symlinks=True, ignore=lambda folder, _: [CREDENTIALS] if Path(folder) == rendered else []
    )
    staging.chmod(PRIVATE_DIR)
    if grant is not None:
        (staging / CREDENTIALS).write_bytes(grant)
        (staging / CREDENTIALS).chmod(PRIVATE_FILE)
    config = {key: value for key, value in _read(staging / CONFIG).items() if key != ACCOUNT}
    _write(staging / CONFIG, {**config, **({ACCOUNT: account} if account else {})})
    evidence = _read(staging / binding.FILE)
    (staging / binding.FILE).write_text(json.dumps({**evidence, "home": str(target.resolve())}) + "\n")
    for name in MANIFESTS:
        if (rendered.parent / name).is_file():
            shutil.copyfile(rendered.parent / name, root(slug) / name)
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    if report:
        binding.rehome(Path(report), target)
    return target


def refusal(target: Path, cwd: Path, managed: Path | None = None) -> str:
    for path in (
        managed or MANAGED,
        target / "settings.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    ):
        settings = _read(path)
        if "apiKeyHelper" in settings:
            return API_HELPER.format(path=path)
        names = sorted(name for name in settings.get("env") or {} if name.startswith(REINTRODUCED))
        if names:
            return SETS.format(path=path, names=", ".join(names))
        if settings.get("forceLoginMethod", "claudeai") != "claudeai":
            return FORCED.format(path=path, value=settings["forceLoginMethod"])
    return ""


def account(slug: str, target: Path, environ: Mapping[str, str]) -> str:
    email = (_read(target / CONFIG).get(ACCOUNT) or {}).get("emailAddress")
    if not (email and _private(target)):
        raise LoginRefused(NO_LOGIN.format(slug=slug))
    found = aicode_tokens.slug(email)
    if f"{TOKEN_PREFIX}{found}" in environ:
        raise LoginRefused(SAME_ACCOUNT.format(account=found))
    if not any(name.startswith(TOKEN_PREFIX) for name in environ):
        raise LoginRefused(NO_QUOTA)
    return found


def child(environ: Mapping[str, str], target: Path, marked: str = "") -> dict[str, str]:
    kept = {name: value for name, value in environ.items() if not name.startswith(DROPPED)}
    marker = {f"{INTERACTIVE_PREFIX}{marked}": "1"} if marked else {}
    return {**kept, **marker, **dict.fromkeys(SELECTORS, str(target))}


def _alive(pid: object) -> bool:
    return isinstance(pid, int) and Path(f"/proc/{pid}").exists()


def claim(slug: str, name: str, pid: int, alive: Callable[[object], bool] = _alive, floor: int = 0) -> int:
    folder = root(slug)
    folder.mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)
    with (folder / LOCK).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        held = _read(folder / OWNER)
        if held.get("pid", pid) != pid and alive(held["pid"]):
            raise LoginRefused(HELD.format(slug=slug, owner=held.get("owner")))
        epoch = max(int(held.get("epoch", 0)), floor) + 1
        _write(folder / OWNER, {"owner": name, "pid": pid, "epoch": epoch})
    return epoch


def publish(bao: openbao_kv.OpenBao, slug: str, target: Path, epoch: int) -> bool:
    grant = _read(target / CREDENTIALS).get(GRANT) or {}
    if not grant.get("refreshToken"):
        return False
    generation = int(grant.get("expiresAt", 0))
    stored, _ = openbao_kv.read(bao, f"{CREDS}/{slug}")
    if (int(stored.get("epoch", 0)), int(stored.get("generation", 0))) >= (epoch, generation):
        return False
    entries = {
        "grant": json.dumps(grant),
        "account": json.dumps(_read(target / CONFIG).get(ACCOUNT) or {}),
        "epoch": str(epoch),
        "generation": str(generation),
    }
    openbao_kv.merge(bao, f"{CREDS}/{slug}", entries)
    return True


def restore(stored: Mapping[str, str], target: Path) -> bool:
    if (target / CREDENTIALS).exists() or not stored.get("grant"):
        return False
    _write(target / CREDENTIALS, {GRANT: json.loads(stored["grant"])})
    _write(target / CONFIG, {**_read(target / CONFIG), ACCOUNT: json.loads(stored["account"])})
    return True


def stored(bao: openbao_kv.OpenBao | None, slug: str) -> dict[str, str]:
    if bao is None:
        return {}
    try:
        return openbao_kv.read(bao, f"{CREDS}/{slug}")[0]
    except openbao_kv.OpenBaoError as exc:
        print(RECOVERY_FAILED.format(error=exc), file=sys.stderr)
        return {}


def keep(bao: openbao_kv.OpenBao | None, slug: str, target: Path, epoch: int) -> bool:
    if bao is None:
        return False
    try:
        return publish(bao, slug, target, epoch)
    except openbao_kv.OpenBaoError as exc:
        print(PUBLISH_FAILED.format(error=exc), file=sys.stderr)
        return False


def checkpoint(environ: Mapping[str, str]) -> bool:
    slug = environ.get("AGENTIHOOKS_SWARM")
    if not slug or environ.get("CLAUDE_CONFIG_DIR") != str(home(slug)):
        return False
    epoch = int(_read(root(slug) / OWNER).get("epoch", 0))
    return keep(openbao_kv.from_env(environ), slug, home(slug), epoch)


def prepare(environ: Mapping[str, str], cwd: Path) -> tuple[Path, str]:
    slug, rendered = environ["AGENTIHOOKS_SWARM"], Path(environ["CLAUDE_CONFIG_DIR"])
    refused = refusal(rendered, cwd)
    if refused:
        raise LoginRefused(refused)
    bao = openbao_kv.from_env(environ)
    copy = stored(bao, slug)
    epoch = claim(slug, environ["AGENTIHOOKS_AGENT_NAME"], os.getpid(), floor=int(copy.get("epoch", 0)))
    target = build(slug, rendered, environ.get(binding.REPORT))
    restore(copy, target)
    found = account(slug, target, environ)
    keep(bao, slug, target, epoch)
    return target, found


def launch_claude(binary: str, args: list[str], environ: Mapping[str, str], report: str) -> None:
    from scripts.claude_quota_balancer import RoutingError
    from scripts.install import _claude_command, _write_route_report

    try:
        target, found = prepare(environ, Path.cwd())
    except LoginRefused as exc:
        raise RoutingError(str(exc)) from exc
    _write_route_report(report, status="routed", account=found, placement="forced")
    print(f"[agenti] account={found} route={ROUTE}", flush=True)
    os.execvpe(binary, _claude_command(binary, [*SETTINGS, *args]), child(environ, target, found))
