"""Configuration for hooks module."""

import os
from pathlib import Path


def _parse_env_file(env_file: Path) -> None:
    """Parse a single .env file and set variables in os.environ.

    Process env takes precedence — only unset keys get populated from the
    file. Otherwise a stale PVC-persisted .env silently masks Helm env
    changes (observed: MEMORY_MIRROR_ROLE cached as 'consumer' in
    /shared/.agentihooks/.env overriding Helm's 'contributor').
    """
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
            os.environ.setdefault(_key, _val)


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

# =============================================================================
# RETRY CIRCUIT BREAKER
# =============================================================================
RETRY_BREAKER_ENABLED = _env_bool("RETRY_BREAKER_ENABLED", "true")
RETRY_BREAKER_MAX = int(os.getenv("RETRY_BREAKER_MAX", "5"))
RETRY_BREAKER_HARD_MAX = int(os.getenv("RETRY_BREAKER_HARD_MAX", "10"))
RETRY_BREAKER_TTL = int(os.getenv("RETRY_BREAKER_TTL", "3600"))

# =============================================================================
# KUBECTL MUTATION GUARD — HARD FLOOR
# =============================================================================
# PreToolUse hook on Bash that blocks live-system state mutation (kubectl edit,
# patch, set, scale, exec writes, cp INTO pod, helm install/upgrade outside
# CI, ssh-edit, scp INTO host, docker exec writes, etc.). Doctrine: code is
# the source of truth; behavior changes go through code → CI → deploy.
# Default-on. Disabling requires the operator to set the env var explicitly
# (never via signal, bypass mode, or any other clearance).
# See: documents/anton/ANTON-CORE-CI-MANIFESTO.md §3.5
#      agentihooks-bundle/profiles/*/.claude/rules/code-is-source.md
KUBECTL_MUTATION_GUARD_ENABLED = _env_bool("KUBECTL_MUTATION_GUARD_ENABLED", "true")

# =============================================================================
# OVERLAY INJECTION
# =============================================================================
# Injects active overlay profile content on every UserPromptSubmit turn.
OVERLAY_INJECTION_ENABLED = _env_bool("OVERLAY_INJECTION_ENABLED", "true")

# =============================================================================
# BRAIN ADAPTER
# =============================================================================
# Pluggable brain content injection into broadcast channels.
# Auto-detect brain: enabled if brain-feed dir has .md files, unless explicitly disabled
_brain_feed_dir = Path(AGENTIHOOKS_HOME) / "brain-feed"
_brain_default = "true" if (_brain_feed_dir.is_dir() and any(_brain_feed_dir.glob("*.md"))) else "false"
BRAIN_ENABLED = _env_bool("BRAIN_ENABLED", _brain_default)
BRAIN_SOURCE_TYPE = os.getenv("BRAIN_SOURCE_TYPE", "file")
BRAIN_SOURCE_PATH = os.getenv("BRAIN_SOURCE_PATH", str(Path(AGENTIHOOKS_HOME) / "brain-feed"))
BRAIN_CHANNEL = os.getenv("BRAIN_CHANNEL", "brain")
BRAIN_REFRESH_INTERVAL = int(os.getenv("BRAIN_REFRESH_INTERVAL", "30"))

# Amygdala — emergency signal propagation
AMYGDALA_ENABLED = _env_bool("AMYGDALA_ENABLED", "false")
AMYGDALA_SIGNAL_PATH = os.getenv("AMYGDALA_SIGNAL_PATH", "")

# Brain Writer — write-back path from agent markers to vault + event bus
BRAIN_WRITER_ENABLED = _env_bool("BRAIN_WRITER_ENABLED", "false")
BRAIN_WRITER_OUTBOX = os.getenv("BRAIN_WRITER_OUTBOX", str(Path(AGENTIHOOKS_HOME) / "brain-outbox"))
BRAIN_WRITER_VAULT_SSH = os.getenv("BRAIN_WRITER_VAULT_SSH", "")
BRAIN_WRITER_VAULT_PATH = os.getenv("BRAIN_WRITER_VAULT_PATH", "/mnt/user/appdata/obsidian/vault")
BRAIN_WRITER_SSH_KEY = os.getenv("BRAIN_WRITER_SSH_KEY", str(Path.home() / ".ssh" / "anton_id_ed25519"))
BRAIN_WRITER_REDIS_URL = os.getenv("BRAIN_WRITER_REDIS_URL", "")
BRAIN_WRITER_MAX_MARKERS = int(os.getenv("BRAIN_WRITER_MAX_MARKERS", "5"))

# Brain HTTP — kernel kb-router client (Phase 7). When BRAIN_URL is set, the
# brain hooks call the kernel instead of reading/writing the filesystem. With
# BRAIN_URL unset, hooks fall back to the legacy filesystem + SSH paths above,
# preserving local/offline operation.
BRAIN_URL = os.getenv("BRAIN_URL", "").rstrip("/")
BRAIN_HTTP_TOKEN = os.getenv("BRAIN_HTTP_TOKEN", "") or os.getenv("KB_ROUTER_TOKEN", "")
BRAIN_HTTP_TIMEOUT = float(os.getenv("BRAIN_HTTP_TIMEOUT", "3"))

# Console quota display
CLAUDE_USAGE_FILE: str = os.getenv("CLAUDE_USAGE_FILE", "")
CLAUDE_USAGE_STALE_SEC: int = int(os.getenv("CLAUDE_USAGE_STALE_SEC", "300"))
CLAUDE_USAGE_POLL_SEC: int = int(os.getenv("CLAUDE_USAGE_POLL_SEC", "60"))

# =============================================================================
# MEMORY MIRROR — cross-machine auto-memory sync (gitfoam push / git pull main)
# =============================================================================
# Each machine pushes to its own gitfoam/<hostname>/main branch; consumers
# merge ONLY origin/main. Promotion is a PR via `agentihooks memory-sync
# propose`. Scope: only ~/.claude/projects/*/memory/** (transcripts excluded).
#
# MEMORY_MIRROR_ROLE (v4 — tiered trust, preferred):
#   off          feature dormant (default)
#   consumer     fetch + consume main only (no snapshot, no gitfoam push)
#   offline      snapshot + gitfoam push only (no fetch/merge; air-gapped)
#   contributor  v3 default: snapshot + push + fetch + merge; manual PR to promote
#   authority    single-leader: contributor pipeline + direct force-with-lease
#                push to origin/main. EXACTLY ONE authority per fleet.
#
# MEMORY_MIRROR_MODE (v3 legacy):
#   off | write | write-local-only
#   Derivation when ROLE is unset: write→contributor, write-local-only→offline,
#   off→off. Legacy MEMORY_MIRROR_ENABLED=true (v1) → contributor.
_memory_mirror_mode_raw = os.getenv("MEMORY_MIRROR_MODE", "").strip().lower()
_memory_mirror_role_raw = os.getenv("MEMORY_MIRROR_ROLE", "").strip().lower()
_memory_mirror_legacy_enabled = _env_bool("MEMORY_MIRROR_ENABLED", "false")

_VALID_MEMORY_MIRROR_ROLES = frozenset({"off", "consumer", "offline", "contributor", "authority"})
if _memory_mirror_role_raw in _VALID_MEMORY_MIRROR_ROLES:
    MEMORY_MIRROR_ROLE: str = _memory_mirror_role_raw
elif _memory_mirror_mode_raw == "write":
    MEMORY_MIRROR_ROLE = "contributor"
elif _memory_mirror_mode_raw == "write-local-only":
    MEMORY_MIRROR_ROLE = "offline"
elif _memory_mirror_mode_raw == "off":
    MEMORY_MIRROR_ROLE = "off"
elif _memory_mirror_legacy_enabled:
    MEMORY_MIRROR_ROLE = "contributor"
else:
    MEMORY_MIRROR_ROLE = "off"

# Legacy MODE kept as a shadow for anything still reading it.
if _memory_mirror_mode_raw in ("off", "write", "write-local-only"):
    MEMORY_MIRROR_MODE: str = _memory_mirror_mode_raw
elif MEMORY_MIRROR_ROLE == "contributor":
    MEMORY_MIRROR_MODE = "write"
elif MEMORY_MIRROR_ROLE == "offline":
    MEMORY_MIRROR_MODE = "write-local-only"
elif MEMORY_MIRROR_ROLE in ("consumer", "authority"):
    # No direct v3 equivalent — treat as write for any legacy callers that
    # only check "is this machine active?".
    MEMORY_MIRROR_MODE = "write"
else:
    MEMORY_MIRROR_MODE = "off"
# Legacy alias kept so existing call sites (and operator scripts) don't break.
MEMORY_MIRROR_ENABLED = MEMORY_MIRROR_ROLE != "off"

MEMORY_MIRROR_DIR: str = os.getenv("MEMORY_MIRROR_DIR", str(Path(AGENTIHOOKS_HOME) / "memory-mirror"))
MEMORY_MIRROR_REMOTE: str = os.getenv("MEMORY_MIRROR_REMOTE", "")
MEMORY_MIRROR_BRANCH_PREFIX: str = os.getenv("MEMORY_MIRROR_BRANCH_PREFIX", "gitfoam")
MEMORY_MIRROR_INTERVAL_SEC: int = int(os.getenv("MEMORY_MIRROR_INTERVAL_SEC", "60"))
MEMORY_MIRROR_CLAUDE_PROJECTS: str = os.getenv(
    "MEMORY_MIRROR_CLAUDE_PROJECTS", str(Path.home() / ".claude" / "projects")
)
MEMORY_MIRROR_SWEEP_IDLE_DAYS: int = int(os.getenv("MEMORY_MIRROR_SWEEP_IDLE_DAYS", "15"))
GITFOAM_BINARY: str = os.getenv("GITFOAM_BINARY", str(Path.home() / ".cargo" / "bin" / "gitfoam"))
GITFOAM_LOCAL_SOURCE: str = os.getenv("GITFOAM_LOCAL_SOURCE", "")

# =============================================================================
# CONTEXT AUDIT — per-tool token consumption tracking
# =============================================================================
CONTEXT_AUDIT_ENABLED = _env_bool("CONTEXT_AUDIT_ENABLED", "true")
CONTEXT_AUDIT_THRESHOLD_PCT: int = int(os.getenv("CONTEXT_AUDIT_THRESHOLD_PCT", "70"))

# =============================================================================
# POST-TOOL-USE PHASE TRACE (debug aid for random-stop diagnosis)
# Logs entry/exit/duration of each sub-handler in on_post_tool_use to hooks.log.
# Off by default — enable with POST_TOOL_TRACE=1 when bisecting handler bugs.
# =============================================================================
POST_TOOL_TRACE = _env_bool("POST_TOOL_TRACE", "false")

# =============================================================================
# AUTONOMY ENFORCER — blocks premature Stop after tool failures (bypass mode only)
# When CLAUDE.md says "never suggest stopping" but the model stalls anyway,
# the Stop hook returns {"decision":"block"} and forces continuation.
# Only fires when bypass mode (`disable controls`) is active.
# =============================================================================
AUTONOMY_ENFORCER_ENABLED = _env_bool("AUTONOMY_ENFORCER_ENABLED", "true")
AUTONOMY_BLOCK_MAX: int = int(os.getenv("AUTONOMY_BLOCK_MAX", "5"))
AUTONOMY_LOOKBACK: int = int(os.getenv("AUTONOMY_LOOKBACK", "8"))

# =============================================================================
# THINKING / EFFORT POLICY
# =============================================================================
EFFORT_POLICY_ENABLED = _env_bool("EFFORT_POLICY_ENABLED", "true")
DEFAULT_EFFORT: str = os.getenv("DEFAULT_EFFORT", "medium")
THINKING_BUDGET_TOKENS: int = int(os.getenv("THINKING_BUDGET_TOKENS", "0"))

# =============================================================================
# PEAK / OFF-PEAK AWARENESS
# =============================================================================
PEAK_HOURS_ENABLED = _env_bool("PEAK_HOURS_ENABLED", "true")
PEAK_HOURS_START: int = int(os.getenv("PEAK_HOURS_START", "5"))
PEAK_HOURS_END: int = int(os.getenv("PEAK_HOURS_END", "11"))
PEAK_HOURS_TZ: str = os.getenv("PEAK_HOURS_TZ", "US/Pacific")

# =============================================================================
# MCP SURFACE AREA WARNING
# =============================================================================
MCP_TOOL_WARN_THRESHOLD: int = int(os.getenv("MCP_TOOL_WARN_THRESHOLD", "40"))
MCP_SCHEMA_AVG_TOKENS: int = int(os.getenv("MCP_SCHEMA_AVG_TOKENS", "150"))

# =============================================================================
# SMART COMPACT SUGGESTIONS
# =============================================================================
COMPACT_SUGGEST_ENABLED = _env_bool("COMPACT_SUGGEST_ENABLED", "true")

# =============================================================================
# CLAUDE.MD SANITY CHECK
# =============================================================================
CLAUDE_MD_SANITY_CHECK = _env_bool("AGENTIHOOKS_CLAUDE_MD_SANITY_CHECK", "true")
CLAUDE_MD_MAXLINES = int(os.getenv("AGENTIHOOKS_CLAUDE_MD_MAXLINES", "200"))

# =============================================================================
# CONTEXT REFRESH — periodic rules re-injection for long sessions
# =============================================================================
CONTEXT_REFRESH_ENABLED = _env_bool("CONTEXT_REFRESH_ENABLED", "true")
CONTEXT_REFRESH_INTERVAL: int = int(os.getenv("CONTEXT_REFRESH_INTERVAL", "20"))
CONTEXT_REFRESH_CLAUDE_MD_INTERVAL: int = int(os.getenv("CONTEXT_REFRESH_CLAUDE_MD_INTERVAL", "40"))
CONTEXT_REFRESH_RULES_DIR: str = os.getenv("CONTEXT_REFRESH_RULES_DIR", str(Path.home() / ".claude" / "rules"))
CONTEXT_REFRESH_INCLUDE_PROJECT = _env_bool("CONTEXT_REFRESH_INCLUDE_PROJECT", "true")
CONTEXT_REFRESH_MAX_CHARS: int = int(os.getenv("CONTEXT_REFRESH_MAX_CHARS", "8000"))

# Context Preprocessor — compression level for refresh injections
_VALID_COMPRESSION_MODES = frozenset({"off", "light", "standard", "aggressive"})
_raw_compression = os.getenv("CONTEXT_REFRESH_COMPRESSION", "standard").lower().strip()
CONTEXT_REFRESH_COMPRESSION: str = _raw_compression if _raw_compression in _VALID_COMPRESSION_MODES else "off"
CONTEXT_REFRESH_ABBREV_FILE: str = os.getenv("CONTEXT_REFRESH_ABBREV_FILE", "")

# Scope of token compression: "refresh" = only context refresh, "all" = all injections
_VALID_COMPRESSION_SCOPES = frozenset({"refresh", "all"})
_raw_scope = os.getenv("CONTEXT_COMPRESSION_SCOPE", "refresh").lower().strip()
CONTEXT_COMPRESSION_SCOPE: str = _raw_scope if _raw_scope in _VALID_COMPRESSION_SCOPES else "refresh"

# =============================================================================
# PROFILE / OVERLAY BROADCAST — notify fleet on profile activation/deactivation
PROFILE_BROADCAST_ENABLED = _env_bool("PROFILE_BROADCAST_ENABLED", "true")
# Auto-overlay: comma-separated overlays to activate at session start
# Can also be set per-agent via AGENTIHOOKS_AUTO_OVERLAY env var
AGENTIHOOKS_AUTO_OVERLAY = os.getenv("AGENTIHOOKS_AUTO_OVERLAY", "")

# BROADCAST SYSTEM — real-time fleet messaging
# =============================================================================
BROADCAST_ENABLED = _env_bool("BROADCAST_ENABLED", "true")
BROADCAST_FILE: str = os.getenv("BROADCAST_FILE", str(Path.home() / ".agentihooks" / "broadcast.json"))
BROADCAST_MAX_MESSAGES: int = int(os.getenv("BROADCAST_MAX_MESSAGES", "50"))
BROADCAST_CRITICAL_ON_PRETOOL = _env_bool("BROADCAST_CRITICAL_ON_PRETOOL", "true")
BROADCAST_PRETOOL_MIN_SEVERITY: str = os.getenv("BROADCAST_PRETOOL_MIN_SEVERITY", "alert")

# Cadence controls — skip re-injecting identical or too-frequent broadcasts per session.
BROADCAST_DEDUP_BY_HASH = _env_bool("BROADCAST_DEDUP_BY_HASH", "true")
# Throttle window between same-message redeliveries. 300s was empirically too
# strict — combined with hash-dedup it suppressed ~96% of brain markers per
# brain-keeper Apr 13 audit. 120s lets a 2h tick land + ad-hoc markers
# without re-injecting the same persistent banner every prompt.
BROADCAST_MIN_INTERVAL_SEC: int = int(os.getenv("BROADCAST_MIN_INTERVAL_SEC", "120"))
# Per-prompt cap. Raised 2→6→8 so brain feed (5 entries) + amygdala (1-2)
# + profile transitions all land in the same prompt without contention.
BROADCAST_MAX_PER_PROMPT: int = int(os.getenv("BROADCAST_MAX_PER_PROMPT", "8"))
BROADCAST_PERSISTENT_THROTTLE = _env_bool("BROADCAST_PERSISTENT_THROTTLE", "true")
BROADCAST_DELIVERY_STATE_FILE: str = os.getenv(
    "BROADCAST_DELIVERY_STATE_FILE",
    str(AGENTIHOOKS_HOME / "broadcast_delivery_state.json"),
)

# =============================================================================
# ENFORCEMENT — operator-curated drumbeat reminders, cadence-driven re-injection
# =============================================================================
ENFORCEMENT_INJECTION_ENABLED = _env_bool("ENFORCEMENT_INJECTION_ENABLED", "true")
ENFORCEMENT_FILE: str = os.getenv("ENFORCEMENT_FILE", str(AGENTIHOOKS_HOME / "enforcements.json"))
ENFORCEMENT_COUNTER_FILE: str = os.getenv(
    "ENFORCEMENT_COUNTER_FILE",
    str(AGENTIHOOKS_HOME / "enforcement_counters.json"),
)

# Brain payload shrinking — cap hot-arcs rows and per-entry body bytes.
BRAIN_HOT_ARCS_TOP_N: int = int(os.getenv("BRAIN_HOT_ARCS_TOP_N", "5"))
BRAIN_PAYLOAD_MAX_BYTES: int = int(os.getenv("BRAIN_PAYLOAD_MAX_BYTES", "1536"))

# =============================================================================
# VOICE OUTPUT — spoken summaries via Anton Voice Service
# =============================================================================
VOICE_ENABLED: bool = _env_bool("VOICE_ENABLED", "false")
VOICE_SERVICE_URL: str = os.getenv("VOICE_SERVICE_URL", "")
VOICE_API_KEY: str = os.getenv("VOICE_API_KEY", "")

# =============================================================================
# CONTROLS BYPASS — operator-toggled session-level CI-gate bypass
# Phrase: "disable controls" / "enable controls". Default-on (feature wired).
# =============================================================================
CONTROLS_BYPASS_ENABLED: bool = _env_bool("CONTROLS_BYPASS_ENABLED", "true")

# =============================================================================
# CI MANIFESTO — doctrine-as-context injection
# =============================================================================
CI_MANIFESTO_ENABLED = _env_bool("CI_MANIFESTO_ENABLED", "true")


# Manifesto resolution (first hit wins):
#   1. $CI_MANIFESTO_PATH explicit env override
#   2. $MANIFESTOS_DIR/<MANIFESTO_NAME>.md (multi-manifesto bundle pattern)
#   3. $AGENTIHOOKS_BUNDLE_ROOT/manifestos/ANTON-CORE-CI-MANIFESTO.md
#   4. ~/dev/tccw-ecosystem/agentihooks-bundle/manifestos/ANTON-CORE-CI-MANIFESTO.md (default)
#   5. legacy fallback ~/dev/tccw-ecosystem/documents/anton/ANTON-CORE-CI-MANIFESTO.md
def _resolve_manifesto_path() -> str:
    explicit = os.getenv("CI_MANIFESTO_PATH")
    if explicit:
        return explicit
    name = os.getenv("MANIFESTO_NAME", "ANTON-CORE-CI-MANIFESTO")
    manifests_dir = os.getenv("MANIFESTOS_DIR")
    if manifests_dir:
        candidate = Path(manifests_dir).expanduser() / f"{name}.md"
        if candidate.exists():
            return str(candidate)
    bundle_root = os.getenv("AGENTIHOOKS_BUNDLE_ROOT")
    if bundle_root:
        candidate = Path(bundle_root).expanduser() / "manifestos" / f"{name}.md"
        if candidate.exists():
            return str(candidate)
    default = Path.home() / "dev" / "tccw-ecosystem" / "agentihooks-bundle" / "manifestos" / f"{name}.md"
    if default.exists():
        return str(default)
    legacy = Path.home() / "dev" / "tccw-ecosystem" / "documents" / "anton" / f"{name}.md"
    return str(legacy)


CI_MANIFESTO_PATH: str = _resolve_manifesto_path()
CI_MANIFESTO_REFRESH_EVERY: int = int(os.getenv("CI_MANIFESTO_REFRESH_EVERY", "8"))

# Auto dev-switch — at SessionStart, if cwd is on main/master, switch to dev.
AUTO_DEV_SWITCH_ENABLED = _env_bool("AUTO_DEV_SWITCH_ENABLED", "true")

# =============================================================================
# OTEL — Custom hook telemetry (Layer 2)
# Layer 1 (Claude Code native) reads standard OTEL_* env vars directly.
# These control agentihooks-specific OTEL emission.
# =============================================================================
OTEL_HOOKS_ENABLED = _env_bool("OTEL_HOOKS_ENABLED", "true")
OTEL_HOOKS_SERVICE_NAME = os.getenv("OTEL_HOOKS_SERVICE_NAME", "agentihooks")

# Langfuse OTEL destination (traces only, OTLP HTTP)
OTEL_LANGFUSE_ENABLED = _env_bool("OTEL_LANGFUSE_ENABLED", "false")
OTEL_LANGFUSE_ENDPOINT = os.getenv("OTEL_LANGFUSE_ENDPOINT", "")
OTEL_LANGFUSE_PUBLIC_KEY = os.getenv("OTEL_LANGFUSE_PUBLIC_KEY", "") or os.getenv("LANGFUSE_PUBLIC_KEY", "")
OTEL_LANGFUSE_SECRET_KEY = os.getenv("OTEL_LANGFUSE_SECRET_KEY", "") or os.getenv("LANGFUSE_SECRET_KEY", "")
