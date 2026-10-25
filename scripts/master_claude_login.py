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
ORDER_ENV = "AGENTIHOOKS_MASTER_LOGIN_ORDER"
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
OFF = "the master claude ai login is off on this hive: set AGENTIHOOKS_MASTER_CLAUDE_LOGIN=true"
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
ALL_HELD = (
    "every stored master claude ai login is held by a live master ({held}): "
    "add one with agentihooks claude login --master"
)
FULL = "all {cap} master login slots hold a login: remove one with agentihooks claude login --remove SLOT"
EMPTY = "master login slot {number} holds no login"
RECORD_KEYS = ("grant", "account", "epoch", "generation")


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


def unusable() -> str:
    if hooks_config.MASTER_CLAUDE_LOGIN is not True:
        return OFF
    return ""


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


def creds(number: int | None = None) -> str:
    number = number or slot()
    return CREDS if number == 1 else f"{CREDS}-{number}"


def root(number: int | None = None) -> Path:
    base = Path(hooks_config.AGENTIHOOKS_HOME)
    number = number or slot()
    return base / ("master-login" if number == 1 else f"master-login-{number}")


def home(number: int | None = None) -> Path:
    return root(number) / "claude"


def numbers() -> range:
    return range(1, MAX_SLOTS + 1)


def login_account(number: int, copy: Mapping[str, str] | None = None) -> str:
    account = _read(home(number) / CONFIG).get(ACCOUNT) or json.loads((copy or {}).get("account") or "{}")
    email = account.get("emailAddress") if isinstance(account, dict) else None
    return aicode_tokens.slug(email) if email else ""


def holders(folder: Path) -> dict[str, int]:
    held = (_read(folder / OWNER).get("holders") or {}).items()
    return {owner: pid for owner, pid in held if _alive(pid)}


def logins() -> dict[int, str]:
    return {number: login_account(number) for number in numbers() if _private(home(number))}


def free(name: str) -> list[str]:
    return [found for number, found in logins().items() if found and set(holders(root(number))) <= {name}]


def empty() -> int:
    number = next((number for number in numbers() if not _private(home(number))), None)
    if number is None:
        raise LoginRefused(FULL.format(cap=MAX_SLOTS))
    return number


def remove(number: int, bao: openbao_kv.OpenBao | None) -> str:
    if not 1 <= number <= MAX_SLOTS:
        raise LoginRefused(SLOT_RANGE.format(number=number, cap=MAX_SLOTS))
    folder, found = root(number), login_account(number)
    if not _private(folder / "claude"):
        raise LoginRefused(EMPTY.format(number=number))
    with _locked(folder):
        held = holders(folder)
        if held:
            raise LoginRefused(HELD.format(owner=", ".join(sorted(held))))
        if bao is not None:
            openbao_kv.merge(bao, creds(number), {}, RECORD_KEYS)
        shutil.rmtree(folder / "claude")
    return found


@contextmanager
def _locked(folder: Path) -> Iterator[Path]:
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
    with _locked(root()) as folder:
        target = _rebuild(folder, rendered)
    if report:
        binding.rehome(Path(report), target)
    return target


def _rebuild(folder: Path, rendered: Path) -> Path:
    target, staging = folder / "claude", folder / "staging"
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


def _master(found: str, environ: Mapping[str, str], store: SettingsStore, number: int | None = None) -> None:
    number = number or slot()
    harness = "claude" if number == 1 else f"claude-{number}"
    actor, now = environ["AGENTIHOOKS_AGENT_NAME"], time.time()
    if master_account.claim(store, harness, found, environ, actor, now) != found:
        store.set(f"master-account-{harness}", found, actor, now)


def account(
    target: Path,
    environ: Mapping[str, str],
    store_of: Callable[[Mapping[str, str]], SettingsStore] | None = None,
    number: int | None = None,
) -> str:
    email = (_read(target / CONFIG).get(ACCOUNT) or {}).get("emailAddress")
    if not (email and _private(target)):
        raise LoginRefused(NO_LOGIN)
    found = aicode_tokens.slug(email)
    if not any(name.startswith(TOKEN_PREFIX) for name in environ):
        raise LoginRefused(NO_QUOTA)
    if f"{TOKEN_PREFIX}{found}" in environ:
        _master(found, environ, (store_of or routing_store)(environ), number)
    return found


def child(environ: Mapping[str, str], target: Path, marked: str = "") -> dict[str, str]:
    kept = {name: value for name, value in environ.items() if not name.startswith(DROPPED)}
    marker = {f"{INTERACTIVE_PREFIX}{marked}": "1"} if marked else {}
    return {**kept, **marker, **dict.fromkeys(SELECTORS, str(target))}


def _alive(pid: object) -> bool:
    return isinstance(pid, int) and Path(f"/proc/{pid}").exists()


def claim(name: str, pid: int, alive: Callable[[object], bool] = _alive, floor: int = 0, alone: bool = False) -> int:
    with _locked(root()) as folder:
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


def publish(bao: openbao_kv.OpenBao, target: Path, epoch: int, number: int | None = None) -> bool:
    grant = _read(target / CREDENTIALS).get(GRANT) or {}
    if not grant.get("refreshToken"):
        return False
    generation = int(grant.get("expiresAt", 0))
    stored, _ = openbao_kv.read(bao, creds(number))
    if (int(stored.get("epoch", 0)), int(stored.get("generation", 0))) >= (epoch, generation):
        return False
    entries = {
        "grant": json.dumps(grant),
        "account": json.dumps(_read(target / CONFIG).get(ACCOUNT) or {}),
        "epoch": str(epoch),
        "generation": str(generation),
    }
    openbao_kv.merge(bao, creds(number), entries)
    return True


def restore(stored: Mapping[str, str], target: Path, force: bool = False) -> bool:
    if not stored.get("grant"):
        return False
    grant = json.loads(stored["grant"])
    local = _read(target / CREDENTIALS).get(GRANT) or {}
    if grant == local:
        return False
    if local and not force and int(local.get("expiresAt", 0)) >= int(stored.get("generation", 0)):
        return False
    _write(target / CREDENTIALS, {GRANT: grant})
    _write(target / CONFIG, {**_read(target / CONFIG), ACCOUNT: json.loads(stored["account"])})
    return True


def recover(target: Path, environ: Mapping[str, str]) -> bool:
    number = next((number for number in numbers() if home(number) == target), None)
    return number is not None and restore(stored(openbao_kv.from_env(environ), number), target, force=True)


def checked(target: Path, environ: Mapping[str, str], found: str) -> bool:
    from scripts.routing import native_login

    try:
        native_login.check(child(environ, target), found)
    except native_login.LoginError:
        if not recover(target, environ):
            return False
        try:
            native_login.check(child(environ, target), found)
        except native_login.LoginError:
            return False
    return True


def stored(bao: openbao_kv.OpenBao | None, number: int | None = None) -> dict[str, str]:
    if bao is None:
        return {}
    try:
        return openbao_kv.read(bao, creds(number))[0]
    except openbao_kv.OpenBaoError as exc:
        print(RECOVERY_FAILED.format(error=exc), file=sys.stderr)
        return {}


def keep(bao: openbao_kv.OpenBao | None, target: Path, epoch: int, number: int | None = None) -> bool:
    if bao is None:
        return False
    try:
        return publish(bao, target, epoch, number)
    except openbao_kv.OpenBaoError as exc:
        print(PUBLISH_FAILED.format(error=exc), file=sys.stderr)
        return False


def checkpoint(environ: Mapping[str, str]) -> bool:
    number = next((found for found in numbers() if environ.get("CLAUDE_CONFIG_DIR") == str(home(found))), None)
    if number is None:
        return False
    epoch = int(_read(root(number) / OWNER).get("epoch", 0))
    return keep(openbao_kv.from_env(environ), home(number), epoch, number)


def _offered(bao: openbao_kv.OpenBao | None) -> dict[int, dict[str, str]]:
    copies = {number: stored(bao, number) for number in numbers()}
    return {number: copy for number, copy in copies.items() if _private(home(number)) or copy.get("grant")}


def _preferred() -> dict[str, int]:
    ranks: dict[str, int] = {}
    for name in os.environ.get(ORDER_ENV, "").split(","):
        try:
            ranks.setdefault(aicode_tokens.slug(name.strip()), len(ranks))
        except aicode_tokens.SlugError:
            continue
    return ranks


def _take(name: str, bao: openbao_kv.OpenBao | None) -> tuple[int, int, dict[str, str]]:
    from scripts import master_logins

    offered = _offered(bao)
    if not offered:
        with _locked(root()) as folder:
            if not holders(folder):
                _adopt()
        offered = {slot(): {}} if _private(home()) else {}
    if not offered:
        raise LoginRefused(NO_LOGIN)
    named = {number: login_account(number, copy) for number, copy in offered.items()}
    now, held = time.time(), []
    order = list(offered)
    if len(offered) > 1:
        ranks = _preferred()
        order.sort(
            key=lambda number: (
                ranks.get(named[number], len(ranks)),
                -master_logins.left(named[number], now),
                number != slot(),
                number,
            )
        )
    for number in order:
        with _locked(root(number)) as folder:
            try:
                epoch = _claim(folder, name, os.getpid(), _alive, int(offered[number].get("epoch", 0)), True)[0]
            except LoginRefused:
                held.append(f"{named[number] or number} by {', '.join(sorted(holders(folder)))}")
                continue
        return number, epoch, offered[number]
    raise LoginRefused(ALL_HELD.format(held="; ".join(held)))


def prepare(environ: Mapping[str, str], cwd: Path) -> tuple[Path, str]:
    rendered = Path(environ["CLAUDE_CONFIG_DIR"])
    refused = refusal(rendered, cwd)
    if refused:
        raise LoginRefused(refused)
    bao = openbao_kv.from_env(environ)
    number, epoch, copy = _take(environ["AGENTIHOOKS_AGENT_NAME"], bao)
    with _locked(root(number)) as folder:
        target = _rebuild(folder, rendered)
    if environ.get(binding.REPORT):
        binding.rehome(Path(environ[binding.REPORT]), target, shared=False)
    restore(copy, target)
    found = account(target, environ, number=number)
    keep(bao, target, epoch, number)
    return target, found


def browser_home(environ: Mapping[str, str]) -> Path:
    for number in numbers():
        with _locked(root(number)) as folder:
            if _private(home(number)) or holders(folder):
                continue
            _claim(folder, environ["AGENTIHOOKS_AGENT_NAME"], os.getpid(), _alive, 0, True)
            return _rebuild(folder, Path(environ["CLAUDE_CONFIG_DIR"]))
    raise LoginRefused(FULL.format(cap=MAX_SLOTS))


def release(target: Path, environ: Mapping[str, str]) -> None:
    name = environ["AGENTIHOOKS_AGENT_NAME"]
    with _locked(target.parent) as folder:
        held = _read(folder / OWNER)
        held["holders"] = {
            owner: pid for owner, pid in (held.get("holders") or {}).items() if (owner, pid) != (name, os.getpid())
        }
        if held.get("alone") == name and name not in held["holders"]:
            held.pop("alone")
        _write(folder / OWNER, held)


def claimed_home(environ: Mapping[str, str]) -> Path | None:
    name = environ["AGENTIHOOKS_AGENT_NAME"]
    return next(
        (home(number) for number in numbers() if holders(root(number)).get(name) == os.getpid()),
        None,
    )


def _alert(environ: Mapping[str, str], outcome: str, cwd: Path) -> None:
    from scripts import master_login_chain as chain

    alerted = _token_folder(environ) / "claude"
    alerted.parent.mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)
    refused = chain.alert(environ, alerted, outcome, str(cwd))
    if refused:
        print(f"[agenti] operator login email refused: {refused}", file=sys.stderr)


def prepare_launch(environ: Mapping[str, str], cwd: Path) -> tuple[Path, str] | None:
    from scripts import master_login_chain as chain

    target, reported = None, None

    def current():
        nonlocal target
        try:
            if target is None:
                target, found = prepare(environ, cwd)
            else:
                number = next(number for number in numbers() if home(number) == target)
                found = account(target, environ, number=number)
                if environ.get(binding.REPORT):
                    binding.rehome(Path(environ[binding.REPORT]), target)
                epoch = int(_read(target.parent / OWNER)["epoch"])
                keep(openbao_kv.from_env(environ), target, epoch, number)
        except LoginRefused as exc:
            if str(exc) == NO_LOGIN or str(exc).startswith("every stored master claude ai login is held"):
                target = target or claimed_home(environ)
                return None
            raise
        if not checked(target, environ, found):
            return None
        chain.notification(target, "current", "success")
        return target, found

    def browser():
        nonlocal target, reported
        try:
            target = target or browser_home(environ)
        except LoginRefused:
            reported = "failure"
            return reported
        chain.notification(target, "current", "unavailable")
        reported = chain.authorize(environ, target)
        return reported

    def token():
        if target is not None:
            release(target, environ)
        if reported in ("captcha", "failure"):
            _alert(environ, reported, cwd)
        return None

    return chain.run(current, browser, token)


def _token_folder(environ: Mapping[str, str]) -> Path:
    return Path(hooks_config.AGENTIHOOKS_HOME) / "master-token" / aicode_tokens.slug(environ["AGENTIHOOKS_AGENT_NAME"])


def token_home(environ: Mapping[str, str]) -> Path:
    folder = _token_folder(environ)
    with _locked(folder):
        target = _rebuild(folder, Path(environ["CLAUDE_CONFIG_DIR"]))
        (target / CREDENTIALS).unlink(missing_ok=True)
        path = target / "settings.json"
        settings = _read(path)
        settings.pop("forceLoginMethod", None)
        path.unlink(missing_ok=True)
        _write(path, settings)
    if environ.get(binding.REPORT):
        binding.rehome(Path(environ[binding.REPORT]), target)
    return target


def _token_args(args: list[str]) -> list[str]:
    result = []
    index = 0
    flags = ("--remote-control", "--remote-control-session-name-prefix")
    while index < len(args):
        argument = args[index]
        index += 1
        if argument.split("=", 1)[0] in flags:
            if "=" not in argument and index < len(args) and not args[index].startswith("--"):
                index += 1
        else:
            result.append(argument)
    return result


def _token_lock(environ: Mapping[str, str]):
    import fcntl

    from scripts.claude_quota_balancer import _cache_path

    path = _cache_path(environ).with_name("claude-route.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a+", encoding="utf-8")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def launch_token(binary: str, args: list[str], environ: Mapping[str, str], report: str) -> None:
    from hooks.context.account_sessions import sessions_by_account
    from scripts import master_login_chain as chain
    from scripts.claude_quota_balancer import API_ACCOUNT, launch_environment, select_credential
    from scripts.hive.account_slots import execute
    from scripts.install import _claude_command, _write_route_report

    with _token_lock(environ):
        decision = select_credential(environ, claude_bin=binary, sessions=sessions_by_account(), exclude=(API_ACCOUNT,))
        target = token_home(environ)
        child_env = {**launch_environment(decision, environ), **dict.fromkeys(SELECTORS, str(target))}
        chain.notification(target, "token", "routed")
        _write_route_report(
            report, status="routed", account=decision.account, placement="fallback", remote_control="off"
        )
        print(f"[agenti] account={decision.account} route=token-fallback remote-control=off", flush=True)
        execute(
            "claude",
            decision.account,
            child_env,
            lambda env: os.execvpe(binary, _claude_command(binary, _token_args(args)), env),
        )


def launch_claude(binary: str, args: list[str], environ: Mapping[str, str], report: str) -> None:
    from scripts.claude_quota_balancer import RoutingError
    from scripts.hive.account_slots import execute
    from scripts.install import _claude_command, _write_route_report

    try:
        prepared = prepare_launch(environ, Path.cwd())
    except LoginRefused as exc:
        raise RoutingError(str(exc)) from exc
    if prepared is None:
        return launch_token(binary, args, environ, report)
    target, found = prepared
    _write_route_report(report, status="routed", account=found, placement="forced")
    print(f"[agenti] account={found} route={ROUTE}", flush=True)
    execute(
        "claude",
        found,
        child(environ, target, found),
        lambda env: os.execvpe(binary, _claude_command(binary, [*SETTINGS, *args]), env),
    )
