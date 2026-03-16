"""Configuration for hooks module."""

import os
from pathlib import Path


def _parse_env_file(env_file: Path) -> None:
    """Parse a single .env file and set variables in os.environ."""
    if not env_file.is_file():
        return
    for _raw in env_file.read_text(encoding="utf-8").splitlines():
        _line = _raw.strip()
        if not _line or _line.startswith("#"):
            continue
        # Strip optional "export " prefix
        if _line.startswith("export "):
            _line = _line[7:].lstrip()
        if "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        _key = _key.strip()
        _val = _val.strip()
        # Handle quoted values: KEY="value" or KEY='value'
        if _val and _val[0] in ('"', "'"):
            _q = _val[0]
            _end = _val.find(_q, 1)
            _val = _val[1:_end] if _end != -1 else _val[1:]
        elif "#" in _val:
            # Strip inline comment: KEY=value # comment
            _val = _val[: _val.index("#")].rstrip()
        if _key:
            os.environ[_key] = _val


def _load_user_env() -> None:
    """Load all .env files from ~/.agentihooks/ into os.environ.

    Called once at module import time. AGENTIHOOKS_HOME is resolved from
    the current os.environ (set via shell) to locate env files.

    Load order:
      1. ~/.agentihooks/.env        (main config — always first)
      2. ~/.agentihooks/*.env        (companion files, sorted alphabetically)

    Later files override earlier ones for duplicate keys.
    """
    _home = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))

    # 1. Main .env first
    _parse_env_file(_home / ".env")

    # 2. Additional *.env files (sorted, skip the main .env to avoid double-load)
    if _home.is_dir():
        for _extra in sorted(_home.glob("*.env")):
            if _extra.name == ".env":
                continue
            _parse_env_file(_extra)


_load_user_env()

# =============================================================================
# RUNTIME DATA ROOT
# =============================================================================

# Root directory for all agentihooks runtime data (logs, memory, state).
# Defaults to ~/.agentihooks. Override via env var for shared K8s mounts:
#   export AGENTIHOOKS_HOME=/mnt/efs/shared
AGENTIHOOKS_HOME = Path(os.getenv("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks")))

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

LOG_FILE = os.getenv("CLAUDE_HOOK_LOG_FILE", str(AGENTIHOOKS_HOME / "logs" / "hooks.log"))

# Path to agent transcript log (centralized stream of conversation)
# This is a copy of the Claude Code transcript, streamed in real-time
AGENT_LOG_FILE = os.getenv("AGENT_LOG_FILE", str(AGENTIHOOKS_HOME / "logs" / "agent.log"))


def _env_bool(key: str, default: str = "false") -> bool:
    """Parse env var as boolean. Accepts: true/false, 1/0, yes/no."""
    val = os.getenv(key, default).lower()
    return val in ("true", "1", "yes")


# Enable/disable hook logging
LOG_ENABLED = _env_bool("CLAUDE_HOOK_LOG_ENABLED", "true")

# Enable/disable logging of hook commands output
LOG_HOOKS_COMMANDS = _env_bool("LOG_HOOKS_COMMANDS", "false")

# Enable/disable automatic transcript logging (logs conversation to hooks.log)
LOG_TRANSCRIPT = _env_bool("LOG_TRANSCRIPT", "true")

# Enable/disable agent log streaming via hooks (copies transcript to AGENT_LOG_FILE)
# Default: false - filesystem-based streaming (sync_transcripts_to_shared.sh) is preferred
# as it provides real-time updates without depending on hook events
STREAM_AGENT_LOG = _env_bool("STREAM_AGENT_LOG", "true")

# Enable/disable ANSI colors in logs (disable for CloudWatch, enable for local dev)
LOG_USE_COLORS = _env_bool("LOG_USE_COLORS", "true")

# Enable/disable automatic memory save on session Stop
# Captures session digest and stores it via MemoryStore
MEMORY_AUTO_SAVE = _env_bool("MEMORY_AUTO_SAVE", "true")

# =============================================================================
# SECRETS SCANNING MODE
# =============================================================================

# Controls how secrets scanning behaves: off | warn | standard | strict
# Invalid values fail-safe to "standard" (never to "off").
_VALID_SECRETS_MODES = frozenset({"off", "warn", "standard", "strict"})
_raw = os.getenv("AGENTIHOOKS_SECRETS_MODE", "standard").lower().strip()
SECRETS_MODE: str = _raw if _raw in _VALID_SECRETS_MODES else "standard"

# =============================================================================
# TOKEN CONTROL CONFIGURATION
# =============================================================================
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
FILE_READ_CACHE_BACKEND = os.getenv("FILE_READ_CACHE_BACKEND", "redis")
FILE_READ_CACHE_TTL = int(os.getenv("FILE_READ_CACHE_TTL", "21600"))

MCP_HYGIENE_ENABLED = _env_bool("MCP_HYGIENE_ENABLED", "true")

# Console quota display
CLAUDE_USAGE_FILE: str = os.getenv("CLAUDE_USAGE_FILE", "")
CLAUDE_USAGE_STALE_SEC: int = int(os.getenv("CLAUDE_USAGE_STALE_SEC", "300"))
CLAUDE_USAGE_POLL_SEC: int = int(os.getenv("CLAUDE_USAGE_POLL_SEC", "60"))
