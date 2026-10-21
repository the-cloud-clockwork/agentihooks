"""Native Claude is the only refresher of the master login; this module never asks for an OAuth grant."""

import fcntl
import json
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from hooks import config as hooks_config
from hooks.context.account_sessions import API_MARKER, INTERACTIVE_PREFIX, TOKEN_PREFIX
from scripts import aicode_tokens, openbao_kv
from scripts.profiles import binding
from scripts.routing import master_account
from scripts.routing.settings import SettingsStore, open_store

ROUTE = "master-login"
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
CREDS = "agentihooks/master-claude-login"
SLOT_ENV = "AGENTIHOOKS_MASTER_LOGIN_SLOT"
SLOT_FILE = "master-login.slot"
MAX_SLOTS = int(os.environ.get("AGENTIHOOKS_MASTER_LOGIN_SLOTS", "5"))
SLOT_RANGE = "master login slot {number} is outside 1 to {cap}"
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
NO_LOGIN = "no master claude ai login: run agentihooks claude login --master"
NO_QUOTA = "the master claude ai login needs workers on quota tokens; none is set"
HELD = "the master login is held by {owner}, still running"
API_HELPER = "{path} sets apiKeyHelper"
SETS = "{path} sets {names}"
FORCED = "{path} sets forceLoginMethod {value}"
SEVERAL = "several legacy master claude ai logins, keep one: {paths}"
LEGACY_HELD = "the legacy master login {path} is held by {owner}, still running"
LEGACY = "master-claude"
PUBLISH_FAILED = "master login recovery copy not stored: {error}"
RECOVERY_FAILED = "master login recovery copy not read: {error}"


class LoginRefused(RuntimeError):
    pass


def enabled(slug: str, seat: str, lane: str, harness: str) -> bool:
    from scripts.swarm_v2 import front_door

    return (
        hooks_config.MASTER_CLAUDE_LOGIN is True
        and lane == LANE
        and harness == "claude"
        and front_door.holds(slug, seat)
    )


def route(login: bool) -> list[str]:
    return ["--route", ROUTE] if login else []


def signed_in() -> str:
    if hooks_config.MASTER_CLAUDE_LOGIN is not True:
        return ""
    email = (_read(home() / CONFIG).get(ACCOUNT) or {}).get("emailAddress")
    return aicode_tokens.slug(email) if email and _private(home()) else ""


def slot() -> int:
    raw = os.environ.get(SLOT_ENV)
    if not raw and _slot_file().is_file():
        raw = _slot_file().read_text().strip()
    try:
        number = int(raw or 1)
    except ValueError:
        return 1
    return number if 1 <= number <= MAX_SLOTS else 1


def _slot_file() -> Path:
    return Path(hooks_config.AGENTIHOOKS_HOME) / SLOT_FILE


def use(number: int) -> None:
    if not 1 <= number <= MAX_SLOTS:
        raise LoginRefused(SLOT_RANGE.format(number=number, cap=MAX_SLOTS))
    path = _slot_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{number}\n")


def creds() -> str:
    number = slot()
    return CREDS if number == 1 else f"{CREDS}-{number}"


def root() -> Path:
    base = Path(hooks_config.AGENTIHOOKS_HOME)
    number = slot()
    return base / ("master-login" if number == 1 else f"master-login-{number}")


def home() -> Path:
    return root() / "claude"


@contextmanager
def _locked() -> Iterator[Path]:
    folder = root()
    folder.mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)
    with (folder / LOCK).open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield folder


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


def build(rendered: Path, report: str | None = None) -> Path:
    with _locked() as folder:
        target = _rebuild(folder, rendered)
    if report:
        binding.rehome(Path(report), target)
    return target


def _rebuild(folder: Path, rendered: Path) -> Path:
    target, staging = home(), folder / "staging"
    kept = _private(target)
    grant = kept.read_bytes() if kept else None
    account = _read(target / CONFIG).get(ACCOUNT)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(
        rendered, staging, symlinks=True, ignore=lambda found, _: [CREDENTIALS] if Path(found) == rendered else []
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
            shutil.copyfile(rendered.parent / name, folder / name)
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    return target


def _adopt() -> None:
    target = home()
    if _private(target):
        return
    legacy = sorted(path for path in (Path(hooks_config.AGENTIHOOKS_HOME) / LEGACY).glob("*/claude") if _private(path))
    if len(legacy) > 1:
        raise LoginRefused(SEVERAL.format(paths=", ".join(map(str, legacy))))
    if legacy:
        path = legacy[0]
        held = _read(path.parent / OWNER)
        if _alive(held.get("pid")):
            raise LoginRefused(LEGACY_HELD.format(path=path, owner=held.get("owner")))
        shutil.rmtree(target, ignore_errors=True)
        path.rename(target)
        shutil.rmtree(path.parent)


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


def routing_store(environ: Mapping[str, str]) -> SettingsStore:
    from scripts.routing import place

    return open_store(place._client(environ), environ)


def _master(found: str, environ: Mapping[str, str], store: SettingsStore) -> None:
    number = slot()
    harness = "claude" if number == 1 else f"claude-{number}"
    actor, now = environ["AGENTIHOOKS_AGENT_NAME"], time.time()
    if master_account.claim(store, harness, found, environ, actor, now) != found:
        store.set(f"master-account-{harness}", found, actor, now)


def account(
    target: Path,
    environ: Mapping[str, str],
    store_of: Callable[[Mapping[str, str]], SettingsStore] | None = None,
) -> str:
    email = (_read(target / CONFIG).get(ACCOUNT) or {}).get("emailAddress")
    if not (email and _private(target)):
        raise LoginRefused(NO_LOGIN)
    found = aicode_tokens.slug(email)
    if not any(name.startswith(TOKEN_PREFIX) for name in environ):
        raise LoginRefused(NO_QUOTA)
    if f"{TOKEN_PREFIX}{found}" in environ:
        _master(found, environ, (store_of or routing_store)(environ))
    return found


def child(environ: Mapping[str, str], target: Path, marked: str = "") -> dict[str, str]:
    kept = {name: value for name, value in environ.items() if not name.startswith(DROPPED)}
    marker = {f"{INTERACTIVE_PREFIX}{marked}": "1"} if marked else {}
    return {**kept, **marker, **dict.fromkeys(SELECTORS, str(target))}


def _alive(pid: object) -> bool:
    return isinstance(pid, int) and Path(f"/proc/{pid}").exists()


def claim(name: str, pid: int, alive: Callable[[object], bool] = _alive, floor: int = 0, alone: bool = False) -> int:
    with _locked() as folder:
        return _claim(folder, name, pid, alive, floor, alone)[0]


def _claim(
    folder: Path, name: str, pid: int, alive: Callable[[object], bool], floor: int, alone: bool = False
) -> tuple[int, bool]:
    held = _read(folder / OWNER)
    holders = {owner: found for owner, found in (held.get("holders") or {}).items() if alive(found)}
    others = sorted(owner for owner, found in holders.items() if found != pid)
    blocker = next((owner for owner in (name, held.get("alone"), *(others if alone else ())) if owner in others), None)
    if blocker:
        raise LoginRefused(HELD.format(owner=blocker))
    epoch = max(int(held.get("epoch", 0)), floor) + 1
    _write(folder / OWNER, {"holders": {**holders, name: pid}, "epoch": epoch, **({"alone": name} if alone else {})})
    return epoch, bool(others)


def publish(bao: openbao_kv.OpenBao, target: Path, epoch: int) -> bool:
    grant = _read(target / CREDENTIALS).get(GRANT) or {}
    if not grant.get("refreshToken"):
        return False
    generation = int(grant.get("expiresAt", 0))
    stored, _ = openbao_kv.read(bao, creds())
    if (int(stored.get("epoch", 0)), int(stored.get("generation", 0))) >= (epoch, generation):
        return False
    entries = {
        "grant": json.dumps(grant),
        "account": json.dumps(_read(target / CONFIG).get(ACCOUNT) or {}),
        "epoch": str(epoch),
        "generation": str(generation),
    }
    openbao_kv.merge(bao, creds(), entries)
    return True


def restore(stored: Mapping[str, str], target: Path) -> bool:
    if (target / CREDENTIALS).exists() or not stored.get("grant"):
        return False
    _write(target / CREDENTIALS, {GRANT: json.loads(stored["grant"])})
    _write(target / CONFIG, {**_read(target / CONFIG), ACCOUNT: json.loads(stored["account"])})
    return True


def stored(bao: openbao_kv.OpenBao | None) -> dict[str, str]:
    if bao is None:
        return {}
    try:
        return openbao_kv.read(bao, creds())[0]
    except openbao_kv.OpenBaoError as exc:
        print(RECOVERY_FAILED.format(error=exc), file=sys.stderr)
        return {}


def keep(bao: openbao_kv.OpenBao | None, target: Path, epoch: int) -> bool:
    if bao is None:
        return False
    try:
        return publish(bao, target, epoch)
    except openbao_kv.OpenBaoError as exc:
        print(PUBLISH_FAILED.format(error=exc), file=sys.stderr)
        return False


def checkpoint(environ: Mapping[str, str]) -> bool:
    if environ.get("CLAUDE_CONFIG_DIR") != str(home()):
        return False
    epoch = int(_read(root() / OWNER).get("epoch", 0))
    return keep(openbao_kv.from_env(environ), home(), epoch)


def prepare(environ: Mapping[str, str], cwd: Path) -> tuple[Path, str]:
    rendered = Path(environ["CLAUDE_CONFIG_DIR"])
    refused = refusal(rendered, cwd)
    if refused:
        raise LoginRefused(refused)
    bao = openbao_kv.from_env(environ)
    copy = stored(bao)
    with _locked() as folder:
        epoch, shared = _claim(
            folder, environ["AGENTIHOOKS_AGENT_NAME"], os.getpid(), _alive, int(copy.get("epoch", 0))
        )
        if not (shared or copy.get("grant")):
            _adopt()
            if not _private(home()):
                raise LoginRefused(NO_LOGIN)
        target = home() if shared else _rebuild(folder, rendered)
    if environ.get(binding.REPORT):
        binding.rehome(Path(environ[binding.REPORT]), target, shared=shared)
    restore(copy, target)
    found = account(target, environ)
    keep(bao, target, epoch)
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
