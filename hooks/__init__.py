"""
Hooks package for Claude Code hook management.

This package provides centralized hook handling for all Claude Code events.

Structure:
    hooks/
    ├── common.py           # Core logging/utils
    ├── config.py           # Configuration
    ├── hook_manager.py     # Event dispatcher
    ├── integrations/       # External service clients
    │   └── mailer.py       # Email client
    └── observability/      # Logging/metrics
        ├── transcript.py   # Transcript logging
        └── metrics.py      # Metrics collection (planned)

Can be invoked as: python -m hooks.hook_manager
"""

from hooks.common import log, log_command, log_transcript, output_json, run_script
from hooks.config import LOG_ENABLED, LOG_FILE, LOG_HOOKS_COMMANDS, LOG_TRANSCRIPT, LOG_USE_COLORS
from hooks.hook_manager import main
from hooks.observability.transcript import log_new_entries

__all__ = [
    # Core
    "main",
    # Logging
    "log",
    "log_command",
    "log_transcript",
    "log_new_entries",
    "output_json",
    "run_script",
    # Config
    "LOG_FILE",
    "LOG_ENABLED",
    "LOG_HOOKS_COMMANDS",
    "LOG_TRANSCRIPT",
    "LOG_USE_COLORS",
]
