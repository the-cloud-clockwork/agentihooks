#!/usr/bin/env python3
"""Base class for all integrations with environment variable validation.

Provides a common foundation for integrations that:
1. Validates required environment variables
2. Logs configuration status to both agent log and Python logging
3. Prevents silent failures by making missing config visible

Usage:
    class MyIntegration(IntegrationBase):
        INTEGRATION_NAME = "my-integration"
        REQUIRED_ENV_VARS = {"MY_API_KEY": "API key for service"}
        OPTIONAL_ENV_VARS = {"MY_TIMEOUT": "Request timeout in seconds (default: 30)"}

    integration = MyIntegration()
    if not integration.is_configured:
        integration.log_status()
"""

import json
import logging
import os
import sys
from abc import ABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_script_dir = Path(__file__).resolve().parent
_app_dir = _script_dir.parent.parent
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from hooks.common import log as agent_log

logger = logging.getLogger("hooks.integrations")


@dataclass
class EnvVarStatus:
    """Status of an environment variable."""

    name: str
    description: str
    is_set: bool
    is_required: bool
    value_hint: Optional[str] = None


@dataclass
class ConfigStatus:
    """Overall configuration status for an integration."""

    integration_name: str
    is_configured: bool
    missing_required: List[str] = field(default_factory=list)
    missing_optional: List[str] = field(default_factory=list)
    env_vars: List[EnvVarStatus] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "integration": self.integration_name,
            "configured": self.is_configured,
            "missing_required": self.missing_required,
            "missing_optional": self.missing_optional,
            "env_vars": [
                {
                    "name": v.name,
                    "description": v.description,
                    "is_set": v.is_set,
                    "required": v.is_required,
                }
                for v in self.env_vars
            ],
            "timestamp": self.timestamp,
        }


class IntegrationBase(ABC):
    """Base class for all integrations with environment variable validation.

    Subclasses should define:
        INTEGRATION_NAME: str - Human-readable name of the integration
        REQUIRED_ENV_VARS: Dict[str, str] - Required env vars and descriptions
        OPTIONAL_ENV_VARS: Dict[str, str] - Optional env vars and descriptions
    """

    INTEGRATION_NAME: str = "base"
    REQUIRED_ENV_VARS: Dict[str, str] = {}
    OPTIONAL_ENV_VARS: Dict[str, str] = {}

    def __init__(self, log_on_init: bool = False):
        """Initialize integration and optionally log status.

        Args:
            log_on_init: If True, log configuration status on initialization
        """
        self._config_status: Optional[ConfigStatus] = None

        if log_on_init:
            self.log_status()

    @property
    def is_configured(self) -> bool:
        """Check if all required environment variables are set."""
        return len(self.get_missing_required()) == 0

    def get_missing_required(self) -> List[str]:
        """Get list of missing required environment variables."""
        missing = []
        for var_name in self.REQUIRED_ENV_VARS:
            value = os.getenv(var_name, "")
            if not value:
                missing.append(var_name)
        return missing

    def get_missing_optional(self) -> List[str]:
        """Get list of missing optional environment variables."""
        missing = []
        for var_name in self.OPTIONAL_ENV_VARS:
            value = os.getenv(var_name, "")
            if not value:
                missing.append(var_name)
        return missing

    def get_config_status(self) -> ConfigStatus:
        """Get comprehensive configuration status."""
        if self._config_status is not None:
            return self._config_status

        env_vars = []

        for var_name, description in self.REQUIRED_ENV_VARS.items():
            value = os.getenv(var_name, "")
            is_set = bool(value)

            value_hint = None
            if is_set and len(value) > 4:
                value_hint = f"{value[:2]}...{value[-2:]}"
            elif is_set:
                value_hint = "***"

            env_vars.append(
                EnvVarStatus(
                    name=var_name,
                    description=description,
                    is_set=is_set,
                    is_required=True,
                    value_hint=value_hint,
                )
            )

        for var_name, description in self.OPTIONAL_ENV_VARS.items():
            value = os.getenv(var_name, "")
            is_set = bool(value)

            value_hint = None
            if is_set and len(value) > 4:
                value_hint = f"{value[:2]}...{value[-2:]}"
            elif is_set:
                value_hint = "***"

            env_vars.append(
                EnvVarStatus(
                    name=var_name,
                    description=description,
                    is_set=is_set,
                    is_required=False,
                    value_hint=value_hint,
                )
            )

        self._config_status = ConfigStatus(
            integration_name=self.INTEGRATION_NAME,
            is_configured=self.is_configured,
            missing_required=self.get_missing_required(),
            missing_optional=self.get_missing_optional(),
            env_vars=env_vars,
        )

        return self._config_status

    def log_status(self, level: str = "info") -> None:
        """Log configuration status to both agent log and Python logger.

        Args:
            level: Log level ('info', 'warning', 'error')
        """
        status = self.get_config_status()

        if status.is_configured:
            message = f"[{self.INTEGRATION_NAME}] Configuration OK"
            payload = {
                "integration": self.INTEGRATION_NAME,
                "status": "configured",
                "env_vars": {v.name: v.is_set for v in status.env_vars},
            }
        else:
            message = f"[{self.INTEGRATION_NAME}] MISSING REQUIRED CONFIG"
            payload = {
                "integration": self.INTEGRATION_NAME,
                "status": "misconfigured",
                "missing_required": status.missing_required,
                "missing_optional": status.missing_optional,
                "required_vars": {name: desc for name, desc in self.REQUIRED_ENV_VARS.items()},
            }

        agent_log(message, payload)

        log_func = getattr(logger, level, logger.info)
        if status.is_configured:
            log_func(f"{self.INTEGRATION_NAME}: configured")
        else:
            log_func(f"{self.INTEGRATION_NAME}: MISSING ENV VARS: {', '.join(status.missing_required)}")

    def check(self) -> ConfigStatus:
        """Validate configuration and log status. Returns ConfigStatus."""
        status = self.get_config_status()
        self.log_status(level="warning" if not status.is_configured else "info")
        return status

    def print_status(self, as_json: bool = False) -> None:
        """Print configuration status to stdout.

        Args:
            as_json: If True, output JSON format; otherwise human-readable
        """
        status = self.get_config_status()

        if as_json:
            print(json.dumps(status.to_dict(), indent=2))
            return

        print(f"\n{'=' * 60}")
        print(f"  {self.INTEGRATION_NAME.upper()} CONFIGURATION STATUS")
        print(f"{'=' * 60}")

        status_icon = "✓" if status.is_configured else "✗"
        status_text = "CONFIGURED" if status.is_configured else "NOT CONFIGURED"
        print(f"\n  Status: {status_icon} {status_text}\n")

        if status.missing_required:
            print("  MISSING REQUIRED:")
            for var in status.missing_required:
                desc = self.REQUIRED_ENV_VARS.get(var, "")
                print(f"    ✗ {var}")
                if desc:
                    print(f"      → {desc}")
            print()

        if status.missing_optional:
            print("  MISSING OPTIONAL:")
            for var in status.missing_optional:
                desc = self.OPTIONAL_ENV_VARS.get(var, "")
                print(f"    - {var}")
                if desc:
                    print(f"      → {desc}")
            print()

        configured = [v for v in status.env_vars if v.is_set]
        if configured:
            print("  CONFIGURED:")
            for var in configured:
                req = "(required)" if var.is_required else "(optional)"
                print(f"    ✓ {var.name} {req}")
            print()

        print(f"{'=' * 60}\n")


class IntegrationRegistry:
    """Registry of all available integrations for status checking."""

    _integrations: Dict[str, type] = {}

    @classmethod
    def register(cls, integration_class: type) -> type:
        """Register an integration class. Use as decorator."""
        name = getattr(integration_class, "INTEGRATION_NAME", integration_class.__name__)
        cls._integrations[name] = integration_class
        return integration_class

    @classmethod
    def get(cls, name: str) -> Optional[type]:
        """Get integration class by name."""
        return cls._integrations.get(name)

    @classmethod
    def all(cls) -> Dict[str, type]:
        """Get all registered integrations."""
        return cls._integrations.copy()

    @classmethod
    def check_all(cls, print_output: bool = True) -> Dict[str, ConfigStatus]:
        """Check configuration status of all registered integrations.

        Args:
            print_output: If True, print status to stdout

        Returns:
            Dict mapping integration names to their ConfigStatus
        """
        results = {}

        if print_output:
            print(f"\n{'=' * 60}")
            print("  INTEGRATION CONFIGURATION CHECK")
            print(f"{'=' * 60}\n")

        for name, integration_class in cls._integrations.items():
            try:
                integration = integration_class()
                status = integration.get_config_status()
                results[name] = status

                if print_output:
                    icon = "✓" if status.is_configured else "✗"
                    status_text = "OK" if status.is_configured else "MISSING CONFIG"
                    print(f"  {icon} {name}: {status_text}")
                    if not status.is_configured:
                        for var in status.missing_required:
                            print(f"      → Missing: {var}")

            except Exception as e:
                if print_output:
                    print(f"  ! {name}: ERROR - {e}")

        if print_output:
            print(f"\n{'=' * 60}\n")

        return results


def main():
    """CLI for checking integration configurations."""
    if len(sys.argv) < 2:
        print("Usage: python -m hooks.integrations.base <command>")
        print("")
        print("Commands:")
        print("  check-all              Check all registered integrations")
        print("  check-all --json       Output as JSON")
        print("")
        sys.exit(1)

    command = sys.argv[1]

    if command == "check-all":
        as_json = "--json" in sys.argv

        if as_json:
            try:
                from hooks.integrations import mailer, sqs
            except ImportError:
                pass

            results = IntegrationRegistry.check_all(print_output=False)
            output = {name: status.to_dict() for name, status in results.items()}
            print(json.dumps(output, indent=2))
        else:
            try:
                from hooks.integrations import mailer, sqs  # noqa: F401
            except ImportError:
                pass

            IntegrationRegistry.check_all(print_output=True)

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
