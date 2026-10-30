"""Configuration for hooks module.

Settings rules that hold across the module:
  - The scripts package config pin runs after the env files load, because they can set XDG_CONFIG_HOME.
  - _MULTILINE_REPORTED is shared with the bashrc agentienv check, so the shell and every process it starts
    name a multi line value once.
  - An invalid SECRETS_MODE falls back to standard, never to off.
  - The session id banner is the only identity the session scoped MCP tools have when the MCP server is
    shared by every session.
  - The kubectl mutation guard is disabled only by the operator setting its env var explicitly, never by a
    signal or bypass mode.
  - An explicit BRAIN_ENABLED wins over brain auto detection; AGENTIHOOKS_BASE_CHANNELS has no code default.
  - The broadcast per injection byte caps stay below Claude Code's 10,000 character additionalContext limit
    with envelope headroom.
  - Manifestos load in deterministic filename order.
"""

import os
import re
import sys
from pathlib import Path

from hooks.context import brain_routes
from scripts import gh_config
from scripts.swarm import brain_binding

_VALID_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_FILE_OWNED_KEYS: set[str] = set()


def _parse_env_file(
    env_file: Path,
    *,
    override_file_owned: bool = False,
    pass_keys: set[str] | None = None,
    blocked_keys: set[str] | None = None,
) -> set[str]:
    """Parse a single .env file and set variables in os.environ.

    Precedence, highest first:

      1. the surrounding process env — a stale PVC-persisted .env must never
         mask a Helm env change, which is the whole reason files do not simply
         overwrite;
      2. a later file in the same load pass;
      3. an earlier file in the same load pass.

    ``pass_keys`` carries rule 2: it is the set of keys the current pass has
    already written, and a key in it may be rewritten by a later file. Without
    it every file after the first to define a key was skipped, so the loader
    was first-file-wins while its own docstring promised the opposite.

    ``override_file_owned`` re-applies values for keys this loader set on a
    previous pass, so a long-lived process (the agentihooks MCP server) picks
    up an edited .env instead of reporting whatever was true at import.

    Within one file the first definition wins, matching the scripts.mcp_daemon scanner, so a later wins
    order applies between files only. Only keys recorded in _FILE_OWNED_KEYS, set from a file rather than
    the process, are refreshed on a reload pass, so a value the shell or Helm set stays authoritative. A
    name that is not a valid shell variable is reported by position only, because a mistyped line can put
    a secret in the name.
    """
    multiline: set[str] = set()
    if not env_file.is_file():
        return multiline
    if pass_keys is None:
        pass_keys = set()
    this_file: set[str] = set()
    _open_quote = ""
    for _n, _raw in enumerate(env_file.read_text(encoding="utf-8").splitlines(), 1):
        if _open_quote:
            if _open_quote in _raw:
                _open_quote = ""
            continue
        _line = _raw.strip()
        if not _line or _line.startswith("#"):
            continue
        if _line.startswith("export "):
            _line = _line[7:].lstrip()
        if "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        _key = _key.strip()
        _val = _val.strip()
        if _val and _val[0] in ('"', "'"):
            _q = _val[0]
            _end = _val.find(_q, 1)
            _val = _val[1:_end] if _end != -1 else _val[1:]
            if _end == -1:
                _open_quote = _q
        elif "#" in _val:
            _val = _val[: _val.index("#")].rstrip()
        if blocked_keys and _key in blocked_keys:
            continue
        if not _key:
            continue
        if not _VALID_NAME.fullmatch(_key):
            print(
                f"[agentihooks] {env_file} line {_n}: variable name is not a valid identifier, line skipped",
                file=sys.stderr,
            )
            continue
        if _open_quote:
            multiline.add(_key)
        if _key in this_file:
            continue
        if _key in pass_keys:
            os.environ[_key] = _val
            this_file.add(_key)
        elif _key not in os.environ:
            os.environ[_key] = _val
            _FILE_OWNED_KEYS.add(_key)
            pass_keys.add(_key)
            this_file.add(_key)
        elif override_file_owned and _key in _FILE_OWNED_KEYS:
            os.environ[_key] = _val
            pass_keys.add(_key)
            this_file.add(_key)
    return multiline


_MULTILINE_REPORTED = "AGENTIHOOKS_MULTILINE_REPORTED"


def _report_multiline_values(from_files: set[str]) -> None:
    """Name the environment values that span several lines, once per process tree, and only when stderr is a
    terminal, because hook processes write stderr into block messages.
    """
    names = from_files | {k for k, v in os.environ.items() if "\n" in v and _VALID_NAME.fullmatch(k)}
    reported = os.environ.get(_MULTILINE_REPORTED, "").split()
    names -= set(reported)
    if names and sys.stderr.isatty():
        print(
            "[agentihooks] these values span several lines, and line based tools such as env "
            "print their later lines as if they were names: " + ", ".join(sorted(names)),
            file=sys.stderr,
        )
        os.environ[_MULTILINE_REPORTED] = " ".join([*reported, *sorted(names)])


_BRAIN_KEYS_FROM_KERNEL = (
    "BRAIN_URL",
    "BRAIN_HTTP_TOKEN",
    "KB_ROUTER_TOKEN",
    "BRAIN_ENABLED",
    "BRAIN_CHANNEL",
    "BRAIN_HOT_ARCS_TOP_N",
    "BRAIN_HTTP_TIMEOUT",
    "BRAIN_PAYLOAD_MAX_BYTES",
    "BRAIN_REFRESH_TOOL_CALLS",
    "BRAIN_SOURCE_PATH",
    "BRAIN_SOURCE_TYPE",
    "AMYGDALA_ENABLED",
    "AMYGDALA_SIGNAL_PATH",
    "BRAIN_WRITER_ENABLED",
    "BRAIN_WRITER_MAX_MARKERS",
    "BRAIN_WRITER_OUTBOX",
)

_BRAIN_FILE_OWNED: set[str] = set()


def _agentibrain_home() -> Path:
    """The brain's config directory. Mirrors AGENTIHOOKS_HOME's convention."""
    return Path(os.environ.get("AGENTIBRAIN_HOME", str(Path.home() / ".agentibrain")))


def _brain_env_file() -> Path:
    canonical = _agentibrain_home() / ".env"
    if canonical.is_file():
        return canonical
    legacy = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks"))) / "agentibrain.env"
    try:
        if legacy.is_file():
            header = legacy.read_text(encoding="utf-8", errors="ignore")[:256]
            if "Managed by `agentibrain install`" in header:
                return legacy
    except OSError:
        pass
    return canonical


def _load_brain_env(*, override_file_owned: bool = False) -> None:
    """Discover how to reach the brain from the brain's own .env.

    The brain is an adapter and owns its configuration, credentials included.
    Copying those into agentihooks' chain means the same bearer lives in two
    files and goes stale in one of them on the next rotation. Reading the
    kernel's file directly keeps one copy, and makes a remote brain a matter of
    editing that file rather than re-running an installer.

    Lowest precedence by construction: a key the process or an agentihooks
    .env already set is left alone, so this is a default, not an override.

    Only the connection and client settings in _BRAIN_KEYS_FROM_KERNEL are adopted, never the whole file,
    which also holds database and provider credentials. Keys it set are tracked in _BRAIN_FILE_OWNED apart
    from _FILE_OWNED_KEYS, so a reload never lets the brain file beat an agentihooks .env that defines the
    same key.
    """
    env_file = _brain_env_file()
    if os.environ.get(brain_binding.VARIABLE) or not env_file.is_file():
        return
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for _raw in lines:
        _line = _raw.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        if _line.startswith("export "):
            _line = _line[7:].lstrip()
        _key, _, _val = _line.partition("=")
        _key, _val = _key.strip(), _val.strip()
        if _key not in _BRAIN_KEYS_FROM_KERNEL or not _val:
            continue
        if len(_val) >= 2 and _val[0] in "\"'" and _val[-1] == _val[0]:
            _val = _val[1:-1]
        if _key not in os.environ:
            os.environ[_key] = _val
            _FILE_OWNED_KEYS.add(_key)
            _BRAIN_FILE_OWNED.add(_key)
        elif override_file_owned and _key in _BRAIN_FILE_OWNED:
            os.environ[_key] = _val


def _load_user_env(*, override_file_owned: bool = False) -> None:
    """Load all .env files from ~/.agentihooks/ into os.environ.

    Called once at module import time. AGENTIHOOKS_HOME is resolved from
    the current os.environ (set via shell) to locate env files.

    Load order:
      1. ~/.agentihooks/.env        (main config — always first)
      2. ~/.agentihooks/*.env        (companion files, sorted alphabetically)

    Later files override earlier ones for duplicate keys.

    Key tracking is shared across every file in the pass, so a later file overrides an earlier one and no
    file overrides the process environment.
    """
    _home = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))

    _pass_keys: set[str] = set()

    blocked = set(_BRAIN_KEYS_FROM_KERNEL) | {"SWARM_SECRET"}
    multiline = _parse_env_file(
        _home / ".env",
        override_file_owned=override_file_owned,
        pass_keys=_pass_keys,
        blocked_keys=blocked,
    )

    if _home.is_dir():
        for _extra in sorted(_home.glob("*.env")):
            if _extra.name == ".env":
                continue
            multiline |= _parse_env_file(
                _extra,
                override_file_owned=override_file_owned,
                pass_keys=_pass_keys,
                blocked_keys=blocked,
            )
    _report_multiline_values(multiline)


def _brain_http_configured() -> bool:
    """True when BRAIN_URL names a kernel to talk to.

    The brain defaults below key off this. In an HTTP deployment the feed is
    served by brain-api out of the vault and nothing ever creates a local
    brain-feed directory, so a default derived only from that directory
    resolves false on exactly the installs that are correctly configured.
    """
    return bool(os.environ.get("BRAIN_URL", "").strip())


def _env_files_fingerprint() -> tuple:
    """Modification stamps of every .env file the loader reads, sorted.

    Cheap enough to call on every status request and it is what makes the
    reload safe: with no fingerprint change there is nothing new on disk to
    learn, so the globals are left exactly as they are.
    """
    _home = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))
    stamps = []
    for f in sorted(_home.glob("*.env")) if _home.is_dir() else []:
        try:
            stamps.append((f.name, f.stat().st_mtime))
        except OSError:
            continue
    main = _home / ".env"
    try:
        stamps.append((".env", main.stat().st_mtime))
    except OSError:
        pass
    try:
        stamps.append(("agentibrain/.env", _brain_env_file().stat().st_mtime))
    except OSError:
        pass
    return tuple(sorted(stamps))


def _guard_swarm_brain() -> None:
    if not os.environ.get(brain_binding.VARIABLE):
        return
    os.environ["BRAIN_SOURCE_TYPE"] = "none"
    if not brain_binding.audience(os.environ):
        for key in ("BRAIN_URL", "BRAIN_HTTP_TOKEN", "KB_ROUTER_TOKEN"):
            os.environ.pop(key, None)
        os.environ["BRAIN_ENABLED"] = os.environ["BRAIN_WRITER_ENABLED"] = "false"


def _route_brain() -> None:
    _guard_swarm_brain()
    brain_routes.apply(os.environ)


_load_user_env()
_load_brain_env()
_route_brain()
_ENV_FINGERPRINT = _env_files_fingerprint()
gh_config.pin(os.environ)
gh_config.persist(os.environ)


def reload_brain_env(force: bool = False) -> dict:
    """Re-read the .env files and re-bind the brain/amygdala module globals.

    Hooks are short-lived processes and always see current config. The
    agentihooks MCP server is not — it imports this module once and then
    answers brain_status/brain_refresh from constants frozen at startup, so an
    edited .env leaves it confidently reporting a source it is no longer
    supposed to use.

    **No-ops unless the .env files changed on disk.** A reload that fires
    unconditionally would overwrite whatever a caller set programmatically —
    including a test that patches these globals, and including a deployment
    with no .env at all, where every value would collapse to its default. The
    fingerprint gate means this only ever costs a few stat() calls and only
    ever fires when there is genuinely something new to read.

    Only keys this loader owns are refreshed; anything the shell or Helm set
    stays authoritative. Returns the re-resolved brain values.
    """
    global BRAIN_ENABLED, BRAIN_SOURCE_TYPE, BRAIN_SOURCE_PATH, BRAIN_CHANNEL
    global BRAIN_REFRESH_TOOL_CALLS, BRAIN_URL, BRAIN_HTTP_TOKEN, BRAIN_HTTP_TIMEOUT
    global BRAIN_WRITER_ENABLED
    global AMYGDALA_ENABLED, AMYGDALA_SIGNAL_PATH, _ENV_FINGERPRINT

    current = _env_files_fingerprint()
    if not force and current == _ENV_FINGERPRINT:
        return {
            "brain_enabled": BRAIN_ENABLED,
            "brain_url": BRAIN_URL,
            "brain_source_type": BRAIN_SOURCE_TYPE,
            "brain_source_path": BRAIN_SOURCE_PATH,
            "amygdala_enabled": AMYGDALA_ENABLED,
            "amygdala_signal_path": AMYGDALA_SIGNAL_PATH,
            "reloaded": False,
        }
    _ENV_FINGERPRINT = current

    _load_user_env(override_file_owned=True)
    _load_brain_env(override_file_owned=True)
    _route_brain()

    _feed_dir = Path(AGENTIHOOKS_HOME) / "brain-feed"
    _default = "true" if (_brain_http_configured() or (_feed_dir.is_dir() and any(_feed_dir.glob("*.md")))) else "false"
    BRAIN_ENABLED = _env_bool("BRAIN_ENABLED", _default)
    BRAIN_SOURCE_TYPE = os.getenv("BRAIN_SOURCE_TYPE", "file")
    BRAIN_SOURCE_PATH = os.getenv("BRAIN_SOURCE_PATH", str(_feed_dir))
    BRAIN_CHANNEL = os.getenv("BRAIN_CHANNEL", "brain")
    BRAIN_REFRESH_TOOL_CALLS = int(os.getenv("BRAIN_REFRESH_TOOL_CALLS", "20"))
    BRAIN_URL = os.getenv("BRAIN_URL", "").rstrip("/")
    BRAIN_HTTP_TOKEN = os.getenv("BRAIN_HTTP_TOKEN", "") or os.getenv("KB_ROUTER_TOKEN", "")
    BRAIN_HTTP_TIMEOUT = float(os.getenv("BRAIN_HTTP_TIMEOUT", "3"))
    BRAIN_WRITER_ENABLED = _env_bool("BRAIN_WRITER_ENABLED", "true" if _brain_http_configured() else "false")
    AMYGDALA_ENABLED = _env_bool("AMYGDALA_ENABLED", "false")
    AMYGDALA_SIGNAL_PATH = os.getenv("AMYGDALA_SIGNAL_PATH", "")

    return {
        "brain_enabled": BRAIN_ENABLED,
        "brain_url": BRAIN_URL,
        "brain_source_type": BRAIN_SOURCE_TYPE,
        "brain_source_path": BRAIN_SOURCE_PATH,
        "brain_writer_enabled": BRAIN_WRITER_ENABLED,
        "amygdala_enabled": AMYGDALA_ENABLED,
        "amygdala_signal_path": AMYGDALA_SIGNAL_PATH,
        "reloaded": True,
    }


AGENTIHOOKS_HOME = Path(os.getenv("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))


LOG_FILE = os.getenv("CLAUDE_HOOK_LOG_FILE", str(AGENTIHOOKS_HOME / "logs" / "hooks.log"))

AGENT_LOG_FILE = os.getenv("AGENT_LOG_FILE", str(AGENTIHOOKS_HOME / "logs" / "agent.log"))

LOG_MAX_BYTES = int(os.getenv("AGENTIHOOKS_LOG_MAX_MB", "20")) * 1024 * 1024
LOG_BACKUPS = int(os.getenv("AGENTIHOOKS_LOG_BACKUPS", "3"))


def _env_bool(key: str, default: str = "false") -> bool:
    """Parse env var as boolean. Accepts: true/false, 1/0, yes/no."""
    val = os.getenv(key, default).lower()
    return val in ("true", "1", "yes")


LOG_ENABLED = _env_bool("CLAUDE_HOOK_LOG_ENABLED", "true")

LOG_HOOKS_COMMANDS = _env_bool("LOG_HOOKS_COMMANDS", "false")

LOG_TRANSCRIPT = _env_bool("LOG_TRANSCRIPT", "true")

STREAM_AGENT_LOG = _env_bool("STREAM_AGENT_LOG", "true")

LOG_USE_COLORS = _env_bool("LOG_USE_COLORS", "true")

MEMORY_AUTO_SAVE = _env_bool("MEMORY_AUTO_SAVE", "true")

MASTER_CLAUDE_LOGIN = _env_bool("AGENTIHOOKS_MASTER_CLAUDE_LOGIN", "false")


_VALID_SECRETS_MODES = frozenset({"off", "warn", "standard", "strict"})
_raw = os.getenv("AGENTIHOOKS_SECRETS_MODE", "standard").lower().strip()
SECRETS_MODE: str = _raw if _raw in _VALID_SECRETS_MODES else "standard"

TOKEN_CONTROL_ENABLED = _env_bool("TOKEN_CONTROL_ENABLED", "true")

TOKEN_MONITOR_ENABLED = _env_bool("TOKEN_MONITOR_ENABLED", "true")
TOKEN_WARN_PCT = int(os.getenv("TOKEN_WARN_PCT", "60"))
TOKEN_CRITICAL_PCT = int(os.getenv("TOKEN_CRITICAL_PCT", "80"))
TOKEN_REDIS_TTL = int(os.getenv("TOKEN_REDIS_TTL", "3600"))

BASH_FILTER_ENABLED = _env_bool("BASH_FILTER_ENABLED", "true")
BASH_FILTER_MAX_LINES = int(os.getenv("BASH_FILTER_MAX_LINES", "50"))
BASH_FILTER_MAX_CHARS = int(os.getenv("BASH_FILTER_MAX_CHARS", "5000"))
BASH_FILTER_TEST_MAX_FAILURES = int(os.getenv("BASH_FILTER_TEST_MAX_FAILURES", "10"))
BASH_FILTER_GIT_MAX_COMMITS = int(os.getenv("BASH_FILTER_GIT_MAX_COMMITS", "20"))

FILE_READ_CACHE_ENABLED = _env_bool("FILE_READ_CACHE_ENABLED", "true")

CREDENTIAL_GUARD_ENABLED = _env_bool("CREDENTIAL_GUARD_ENABLED", "true")
FILE_READ_CACHE_BACKEND = os.getenv("FILE_READ_CACHE_BACKEND", "redis")
FILE_READ_CACHE_TTL = int(os.getenv("FILE_READ_CACHE_TTL", "21600"))

MCP_HYGIENE_ENABLED = _env_bool("MCP_HYGIENE_ENABLED", "true")
CODEX_CONTEXT_PIN_ENABLED = _env_bool("CODEX_CONTEXT_PIN_ENABLED", "true")

PROJECT_BRIDGE_ENABLED = _env_bool("PROJECT_BRIDGE_ENABLED", "true")
PROJECT_BRIDGE_MAX_BYTES = int(os.getenv("PROJECT_BRIDGE_MAX_BYTES", "0"))

MCP_SESSION_ID_BANNER_ENABLED = _env_bool("MCP_SESSION_ID_BANNER_ENABLED", "true")

RETRY_BREAKER_ENABLED = _env_bool("RETRY_BREAKER_ENABLED", "true")
RETRY_BREAKER_MAX = int(os.getenv("RETRY_BREAKER_MAX", "5"))
RETRY_BREAKER_HARD_MAX = int(os.getenv("RETRY_BREAKER_HARD_MAX", "10"))
RETRY_BREAKER_TTL = int(os.getenv("RETRY_BREAKER_TTL", "3600"))

KUBECTL_MUTATION_GUARD_ENABLED = _env_bool("KUBECTL_MUTATION_GUARD_ENABLED", "true")

SERENA_EDIT_GUARD_ENABLED = os.getenv("AGENTIHOOKS_SERENA", "true") != "false"

_brain_feed_dir = Path(AGENTIHOOKS_HOME) / "brain-feed"
_brain_default = (
    "true"
    if (_brain_http_configured() or (_brain_feed_dir.is_dir() and any(_brain_feed_dir.glob("*.md"))))
    else "false"
)
BRAIN_ENABLED = _env_bool("BRAIN_ENABLED", _brain_default)
BRAIN_SOURCE_TYPE = os.getenv("BRAIN_SOURCE_TYPE", "file")
BRAIN_SOURCE_PATH = os.getenv("BRAIN_SOURCE_PATH", str(Path(AGENTIHOOKS_HOME) / "brain-feed"))
BRAIN_CHANNEL = os.getenv("BRAIN_CHANNEL", "brain")
BRAIN_REFRESH_TOOL_CALLS = int(os.getenv("BRAIN_REFRESH_TOOL_CALLS", "20"))


def _parse_channel_list(raw: str) -> tuple[str, ...]:
    """Parse comma-separated channel names. Trim whitespace, drop empties, dedupe."""
    seen: list[str] = []
    for part in (raw or "").split(","):
        c = part.strip()
        if c and c not in seen:
            seen.append(c)
    return tuple(seen)


BASE_CHANNELS: tuple[str, ...] = _parse_channel_list(os.getenv("AGENTIHOOKS_BASE_CHANNELS", ""))

AMYGDALA_ENABLED = _env_bool("AMYGDALA_ENABLED", "false")
AMYGDALA_SIGNAL_PATH = os.getenv("AMYGDALA_SIGNAL_PATH", "")

BRAIN_WRITER_ENABLED = _env_bool("BRAIN_WRITER_ENABLED", "true" if _brain_http_configured() else "false")
BRAIN_WRITER_OUTBOX = os.getenv("BRAIN_WRITER_OUTBOX", str(Path(AGENTIHOOKS_HOME) / "brain-outbox"))
BRAIN_WRITER_MAX_MARKERS = int(os.getenv("BRAIN_WRITER_MAX_MARKERS", "5"))

BRAIN_URL = os.getenv("BRAIN_URL", "").rstrip("/")
BRAIN_HTTP_TOKEN = os.getenv("BRAIN_HTTP_TOKEN", "") or os.getenv("KB_ROUTER_TOKEN", "")
BRAIN_HTTP_TIMEOUT = float(os.getenv("BRAIN_HTTP_TIMEOUT", "3"))

CLAUDE_USAGE_FILE: str = os.getenv("CLAUDE_USAGE_FILE", "")
CLAUDE_USAGE_STALE_SEC: int = int(os.getenv("CLAUDE_USAGE_STALE_SEC", "300"))
CLAUDE_USAGE_POLL_SEC: int = int(os.getenv("CLAUDE_USAGE_POLL_SEC", "60"))
QUOTA_USAGE_INJECTION_ENABLED = _env_bool("QUOTA_USAGE_INJECTION_ENABLED", "true")
QUOTA_USAGE_TOOL_CALLS: int = int(os.getenv("QUOTA_USAGE_TOOL_CALLS", "5"))
QUOTA_USAGE_STALE_SEC: int = int(os.getenv("QUOTA_USAGE_STALE_SEC", "300"))

QUOTA_POLICY_ENABLED = _env_bool("QUOTA_POLICY_ENABLED", "true")
QUOTA_HANDOFF_WEEK_PCT: float = float(os.getenv("AGENTIHOOKS_HANDOFF_WEEK_PCT", "98"))
QUOTA_HANDOFF_5H_PCT: float = float(os.getenv("AGENTIHOOKS_HANDOFF_5H_PCT", "99"))
QUOTA_WARN_WEEK_PCT: float = float(os.getenv("AGENTIHOOKS_HANDOFF_WARN_WEEK_PCT", "90"))
QUOTA_WARN_5H_PCT: float = float(os.getenv("AGENTIHOOKS_HANDOFF_WARN_5H_PCT", "95"))
QUOTA_HANDOFF_MIN_LEFT: float = float(os.getenv("AGENTIHOOKS_HANDOFF_MIN_LEFT", "20"))
QUOTA_WAIT_MIN_WEEK_LEFT: float = float(os.getenv("AGENTIHOOKS_WAIT_MIN_WEEK_LEFT", "10"))
QUOTA_RESERVE_ACCOUNTS: frozenset[str] = frozenset(
    slug.strip() for slug in os.getenv("AGENTIHOOKS_RESERVE_ACCOUNTS", "").split(",") if slug.strip()
)

CONTEXT_AUDIT_ENABLED = _env_bool("CONTEXT_AUDIT_ENABLED", "true")
CONTEXT_AUDIT_THRESHOLD_PCT: int = int(os.getenv("CONTEXT_AUDIT_THRESHOLD_PCT", "70"))

POST_TOOL_TRACE = _env_bool("POST_TOOL_TRACE", "false")

EFFORT_POLICY_ENABLED = _env_bool("EFFORT_POLICY_ENABLED", "true")
DEFAULT_EFFORT: str = os.getenv("DEFAULT_EFFORT", "medium")
THINKING_BUDGET_TOKENS: int = int(os.getenv("THINKING_BUDGET_TOKENS", "0"))

MCP_TOOL_WARN_THRESHOLD: int = int(os.getenv("MCP_TOOL_WARN_THRESHOLD", "40"))
MCP_SCHEMA_AVG_TOKENS: int = int(os.getenv("MCP_SCHEMA_AVG_TOKENS", "150"))

COMPACT_SUGGEST_ENABLED = _env_bool("COMPACT_SUGGEST_ENABLED", "true")
COMPACT_LIMIT: int = int(os.getenv("AGENTIHOOKS_COMPACT_LIMIT", "600"))
HANDOFF_MARGIN: int = int(os.getenv("AGENTIHOOKS_HANDOFF_MARGIN", "50"))

CLAUDE_MD_SANITY_CHECK = _env_bool("AGENTIHOOKS_CLAUDE_MD_SANITY_CHECK", "true")
CLAUDE_MD_MAXLINES = int(os.getenv("AGENTIHOOKS_CLAUDE_MD_MAXLINES", "200"))


_VALID_COMPRESSION_MODES = frozenset({"off", "light", "standard", "aggressive"})
_raw_compression = os.getenv("CONTEXT_REFRESH_COMPRESSION", "standard").lower().strip()
CONTEXT_REFRESH_COMPRESSION: str = _raw_compression if _raw_compression in _VALID_COMPRESSION_MODES else "off"
CONTEXT_REFRESH_ABBREV_FILE: str = os.getenv("CONTEXT_REFRESH_ABBREV_FILE", "")

_VALID_COMPRESSION_SCOPES = frozenset({"refresh", "all"})
_raw_scope = os.getenv("CONTEXT_COMPRESSION_SCOPE", "refresh").lower().strip()
CONTEXT_COMPRESSION_SCOPE: str = _raw_scope if _raw_scope in _VALID_COMPRESSION_SCOPES else "refresh"

PROFILE_BROADCAST_ENABLED = _env_bool("PROFILE_BROADCAST_ENABLED", "true")

BROADCAST_ENABLED = _env_bool("BROADCAST_ENABLED", "true")
BROADCAST_FILE: str = os.getenv("BROADCAST_FILE", str(Path.home() / ".agentihooks" / "broadcast.json"))
BROADCAST_MAX_MESSAGES: int = int(os.getenv("BROADCAST_MAX_MESSAGES", "50"))
BROADCAST_CRITICAL_ON_PRETOOL = _env_bool("BROADCAST_CRITICAL_ON_PRETOOL", "false")
BROADCAST_PRETOOL_MIN_SEVERITY: str = os.getenv("BROADCAST_PRETOOL_MIN_SEVERITY", "critical")

BROADCAST_DEDUP_BY_HASH = _env_bool("BROADCAST_DEDUP_BY_HASH", "true")
BROADCAST_MIN_INTERVAL_SEC: int = int(os.getenv("BROADCAST_MIN_INTERVAL_SEC", "120"))
BROADCAST_MAX_PER_PROMPT: int = int(os.getenv("BROADCAST_MAX_PER_PROMPT", "8"))
BROADCAST_MAX_BYTES_PRETOOL: int = int(os.getenv("BROADCAST_MAX_BYTES_PRETOOL", "0"))
BROADCAST_MAX_BYTES_PROMPT: int = int(os.getenv("BROADCAST_MAX_BYTES_PROMPT", "0"))
HOOK_INJECTION_MAX_CHARS: int = int(os.getenv("HOOK_INJECTION_MAX_CHARS", "9000"))
BROADCAST_PERSISTENT_THROTTLE = _env_bool("BROADCAST_PERSISTENT_THROTTLE", "true")
BROADCAST_DELIVERY_STATE_FILE: str = os.getenv(
    "BROADCAST_DELIVERY_STATE_FILE",
    str(AGENTIHOOKS_HOME / "broadcast_delivery_state.json"),
)

ENFORCEMENT_INJECTION_ENABLED = _env_bool("ENFORCEMENT_INJECTION_ENABLED", "true")
ENFORCEMENT_FILE: str = os.getenv("ENFORCEMENT_FILE", str(AGENTIHOOKS_HOME / "enforcements.json"))
ENFORCEMENT_COUNTER_FILE: str = os.getenv(
    "ENFORCEMENT_COUNTER_FILE",
    str(AGENTIHOOKS_HOME / "enforcement_counters.json"),
)
ENFORCEMENT_DELIVERY_STATE_FILE: str = os.getenv(
    "ENFORCEMENT_DELIVERY_STATE_FILE",
    str(AGENTIHOOKS_HOME / "enforcement_delivery_state.json"),
)
ENFORCEMENT_MATCH_COUNTER_FILE: str = os.getenv(
    "ENFORCEMENT_MATCH_COUNTER_FILE",
    str(AGENTIHOOKS_HOME / "enforcement_match_counters.json"),
)

CONDITIONS_ENABLED = _env_bool("CONDITIONS_ENABLED", "true")
CONDITIONS_TIMEOUT_SEC: float = float(os.getenv("CONDITIONS_TIMEOUT_SEC", "10"))
CONDITIONS_MAX_PARALLEL: int = max(1, int(os.getenv("CONDITIONS_MAX_PARALLEL", "8")))
CONDITIONS_TRUSTED_OWNERS: str = os.getenv("CONDITIONS_TRUSTED_OWNERS", "")

BRAIN_HOT_ARCS_TOP_N: int = int(os.getenv("BRAIN_HOT_ARCS_TOP_N", "10"))
BRAIN_PAYLOAD_MAX_BYTES: int = int(os.getenv("BRAIN_PAYLOAD_MAX_BYTES", "1536"))

VOICE_ENABLED: bool = _env_bool("VOICE_ENABLED", "false")
VOICE_SERVICE_URL: str = os.getenv("VOICE_SERVICE_URL", "")
VOICE_API_KEY: str = os.getenv("VOICE_API_KEY", "")

CONTROLS_BYPASS_ENABLED: bool = _env_bool("CONTROLS_BYPASS_ENABLED", "true")

CI_MANIFESTO_ENABLED = _env_bool("CI_MANIFESTO_ENABLED", "true")


def manifesto_name(name: str) -> str:
    return Path(name.strip()).stem.casefold()


def _manifesto_skip_names() -> set[str]:
    raw = os.getenv("AGENTIHOOKS_SKIP_MANIFESTO", "")
    return {manifesto_name(name) for name in raw.split(",") if name.strip()}


_FRONT_MATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)", re.S | re.M)


def _manifesto_front(text: str) -> tuple[dict, str]:
    import yaml

    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text
    return (data, text[match.end() :].lstrip("\n")) if isinstance(data, dict) else ({}, text)


def manifesto_body(path: str | Path) -> str:
    return _manifesto_front(Path(path).read_text(encoding="utf-8"))[1]


def manifesto_roles(path: str | Path) -> list[str] | None:
    roles = _manifesto_front(Path(path).read_text(encoding="utf-8"))[0].get("roles")
    if roles is None:
        return None
    return [str(role).casefold() for role in (roles if isinstance(roles, list) else [roles])]


def _receives(path: Path, role: str | None, choice: dict[str, bool]) -> bool:
    picked = choice.get(manifesto_name(path.name))
    if picked is not None:
        return picked
    if role is None:
        return True
    roles = manifesto_roles(path)
    return roles is None or role.casefold() in roles


def _manifesto_files(directory: Path) -> list[Path]:
    return [path for path in directory.glob("*.md") if path.name.casefold() != "readme.md"]


def _resolve_manifesto_paths(
    bundle_root: str | Path | None = None,
    role: str | None = None,
    choice: dict[str, bool] | None = None,
    user_root: str | Path | None = None,
    shared_root: str | Path | None = None,
) -> list[str]:
    explicit = os.getenv("CI_MANIFESTO_PATH")
    if explicit:
        path = Path(explicit).expanduser()
        return [str(path)] if path.is_file() else []
    manifests_dir = os.getenv("MANIFESTOS_DIR")
    root = bundle_root or os.getenv("AGENTIHOOKS_BUNDLE_ROOT")
    directory = (
        Path(manifests_dir).expanduser()
        if manifests_dir
        else Path(root).expanduser() / "manifestos"
        if root
        else Path.home() / "dev" / "tcc-ecosystem" / "agentihooks-bundle" / "manifestos"
    )
    user_directory = Path(user_root).expanduser() / "manifestos" if user_root and not manifests_dir else None
    shared_directory = Path(shared_root).expanduser() / "manifestos" if shared_root and not manifests_dir else None
    skip = _manifesto_skip_names()
    layers = [layer for layer in (directory, shared_directory, user_directory) if layer and layer.is_dir()]
    if layers:
        merged: list[Path] = []
        for layer in layers:
            files = _manifesto_files(layer)
            overridden = {path.stem.casefold() for path in files}
            merged = [path for path in merged if path.stem.casefold() not in overridden] + files
        return [
            str(path)
            for path in sorted(merged, key=lambda item: item.name.casefold())
            if path.stem.casefold() not in skip and _receives(path, role, choice or {})
        ]
    legacy = Path.home() / "dev" / "tcc-ecosystem" / "documents" / "anton" / "ANTON-CORE-CI-MANIFESTO.md"
    return [str(legacy)] if legacy.is_file() and legacy.stem.casefold() not in skip else []


def _resolve_manifesto_path() -> str:
    paths = _resolve_manifesto_paths()
    return paths[0] if paths else ""


CI_MANIFESTO_PATHS: list[str] = _resolve_manifesto_paths()
CI_MANIFESTO_PATH: str = CI_MANIFESTO_PATHS[0] if CI_MANIFESTO_PATHS else ""
CI_MANIFESTO_MAX_BYTES: int = int(os.getenv("CI_MANIFESTO_MAX_BYTES", "0"))
CI_MANIFESTO_RUNTIME_INJECT: bool = _env_bool("CI_MANIFESTO_RUNTIME_INJECT", "false")
CI_MANIFESTO_REFRESH_EVERY: int = int(os.getenv("CI_MANIFESTO_REFRESH_EVERY", "8"))

AGENTIHOOKS_FORCE_DEV_BRANCH = _env_bool("AGENTIHOOKS_FORCE_DEV_BRANCH", "false")

OTEL_HOOKS_ENABLED = _env_bool("OTEL_HOOKS_ENABLED", "true")
OTEL_HOOKS_SERVICE_NAME = os.getenv("OTEL_HOOKS_SERVICE_NAME", "agentihooks")


def langfuse_enabled(environ) -> bool:
    """Claude Code strips every OTEL_* variable from hook processes, so the unprefixed name is the one hooks see."""
    names = ("AGENTIHOOKS_LANGFUSE_ENABLED", "OTEL_LANGFUSE_ENABLED")
    return any(environ.get(name, "").lower() in ("true", "1", "yes") for name in names)


OTEL_LANGFUSE_ENABLED = langfuse_enabled(os.environ)


def langfuse_route(environ) -> tuple[str, str]:
    """(endpoint, Host header). The default LAN ingress routes by Host; the public name sits behind Cloudflare Access."""
    endpoint = environ.get("OTEL_LANGFUSE_ENDPOINT", "")
    host = environ.get("OTEL_LANGFUSE_HOST_HEADER", "") or ("" if endpoint else "langfuse.homeofanton.com")
    return endpoint or "http://10.10.30.200/api/public/otel", host


def hook_collector(environ) -> tuple[str, str]:
    """(endpoint, protocol) of the hook telemetry collector; Claude Code strips every OTEL_* variable from hook processes."""
    endpoint = environ.get("AGENTIHOOKS_OTLP_ENDPOINT", "") or environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    protocol = environ.get("AGENTIHOOKS_OTLP_PROTOCOL", "") or environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "")
    return endpoint, protocol


OTEL_LANGFUSE_ENDPOINT, OTEL_LANGFUSE_HOST_HEADER = langfuse_route(os.environ)
OTEL_LANGFUSE_PUBLIC_KEY = os.getenv("OTEL_LANGFUSE_PUBLIC_KEY", "") or os.getenv("LANGFUSE_PUBLIC_KEY", "")
OTEL_LANGFUSE_SECRET_KEY = os.getenv("OTEL_LANGFUSE_SECRET_KEY", "") or os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_FIELD_MAX_CHARS = int(os.getenv("AGENTIHOOKS_LANGFUSE_FIELD_MAX_CHARS", "32000"))
