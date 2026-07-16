"""AWS configuration parser for hooks module.

Parse AWS CLI config files to extract profile names and account IDs.

Usage:
    from hooks.integrations.aws import AWSConfigParser

    parser = AWSConfigParser.get_parser()

    # Get all accounts
    accounts = parser.get_all_accounts()
    for acc in accounts:
        print(f"{acc.profile}: {acc.account_id}")

    # Get specific account
    account_id = parser.get_account_id("<account_profile>")
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from hooks.common import log

# =============================================================================
# CONFIGURATION
# =============================================================================

# Default config file locations (in order of priority)
# The /home/appuser entries are container-only fallbacks (ECS/Fargate task
# role image runs as `appuser`); they simply don't exist on a bare-metal or
# macOS host and are skipped by the existence check in get_parser() below.
DEFAULT_CONFIG_PATHS = [
    Path.home() / ".aws" / "config",
    Path.home() / ".aws" / "config.fargate",
    Path("/home/appuser/.aws/config"),
    Path("/home/appuser/.aws/config.fargate"),
]

# Regex patterns
PROFILE_PATTERN = re.compile(r"^\[profile\s+([^\]]+)\]")
ROLE_ARN_PATTERN = re.compile(r"role_arn\s*=\s*arn:aws:iam::(\d{12}):")
ACCOUNT_ID_PATTERN = re.compile(r"\d{12}")


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class AWSAccount:
    """Information about an AWS account from config."""

    profile: str
    account_id: str
    role_arn: Optional[str] = None

    @property
    def display(self) -> str:
        """Return display format: account_id|profile."""
        return f"{self.account_id}|{self.profile}"


# =============================================================================
# AWS CONFIG PARSER
# =============================================================================


class AWSConfigParser:
    """Parse AWS CLI configuration files to extract profile and account information."""

    _instance: Optional["AWSConfigParser"] = None

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize AWS config parser.

        Args:
            config_path: Optional path to AWS config file. If not provided,
                        searches default locations.
        """
        self._config_path: Optional[Path] = None
        self._accounts: Dict[str, AWSAccount] = {}
        self._loaded = False

        if config_path:
            path = Path(config_path)
            if path.exists():
                self._config_path = path
            else:
                log(f"AWS config file not found: {config_path}")
        else:
            self._config_path = self._find_config()

    @classmethod
    def get_parser(cls, config_path: Optional[str] = None) -> "AWSConfigParser":
        """
        Get singleton parser instance.

        Args:
            config_path: Optional path to AWS config file.

        Returns:
            AWSConfigParser instance.
        """
        if cls._instance is None or config_path:
            cls._instance = cls(config_path)
        return cls._instance

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the cached parser instance."""
        cls._instance = None

    def _find_config(self) -> Optional[Path]:
        """Find AWS config file from default locations."""
        # Check environment variable first
        env_config = os.environ.get("AWS_CONFIG_FILE")
        if env_config:
            path = Path(env_config)
            if path.exists():
                return path

        # Check default locations
        for path in DEFAULT_CONFIG_PATHS:
            if path.exists():
                return path

        return None

    def _parse_config(self) -> None:
        """Parse the AWS config file and extract profiles."""
        if self._loaded or not self._config_path:
            return

        try:
            content = self._config_path.read_text()
            current_profile: Optional[str] = None
            current_role_arn: Optional[str] = None

            for line in content.splitlines():
                line = line.strip()

                # Check for profile header
                profile_match = PROFILE_PATTERN.match(line)
                if profile_match:
                    # Save previous profile if it had an account ID
                    if current_profile and current_role_arn:
                        account_match = ACCOUNT_ID_PATTERN.search(current_role_arn)
                        if account_match:
                            self._accounts[current_profile] = AWSAccount(
                                profile=current_profile,
                                account_id=account_match.group(),
                                role_arn=current_role_arn,
                            )

                    # Start new profile
                    current_profile = profile_match.group(1)
                    current_role_arn = None
                    continue

                # Check for role_arn
                if current_profile and line.startswith("role_arn"):
                    role_match = ROLE_ARN_PATTERN.match(line)
                    if role_match:
                        current_role_arn = line.split("=", 1)[1].strip()

            # Don't forget last profile
            if current_profile and current_role_arn:
                account_match = ACCOUNT_ID_PATTERN.search(current_role_arn)
                if account_match:
                    self._accounts[current_profile] = AWSAccount(
                        profile=current_profile,
                        account_id=account_match.group(),
                        role_arn=current_role_arn,
                    )

            self._loaded = True
            log(f"Parsed AWS config: {len(self._accounts)} profiles found")

        except Exception as e:
            log(f"Error parsing AWS config: {e}")

    def get_profiles(self) -> List[str]:
        """
        Get list of all profile names.

        Returns:
            List of profile names.
        """
        self._parse_config()
        return list(self._accounts.keys())

    def get_account_id(self, profile: str) -> Optional[str]:
        """
        Get account ID for a specific profile.

        Args:
            profile: Profile name.

        Returns:
            12-digit account ID or None if not found.
        """
        self._parse_config()
        account = self._accounts.get(profile)
        return account.account_id if account else None

    def get_account(self, profile: str) -> Optional[AWSAccount]:
        """
        Get full account information for a profile.

        Args:
            profile: Profile name.

        Returns:
            AWSAccount instance or None if not found.
        """
        self._parse_config()
        return self._accounts.get(profile)

    def get_all_accounts(self) -> List[AWSAccount]:
        """
        Get all accounts from config.

        Returns:
            List of AWSAccount instances.
        """
        self._parse_config()
        return list(self._accounts.values())

    def find_by_account_id(self, account_id: str) -> Optional[AWSAccount]:
        """
        Find account by account ID.

        Args:
            account_id: 12-digit AWS account ID.

        Returns:
            AWSAccount instance or None if not found.
        """
        self._parse_config()
        for account in self._accounts.values():
            if account.account_id == account_id:
                return account
        return None

    def find_by_pattern(self, pattern: str) -> List[AWSAccount]:
        """
        Find accounts matching a pattern in profile name.

        Args:
            pattern: Regex pattern to match against profile names.

        Returns:
            List of matching AWSAccount instances.
        """
        self._parse_config()
        regex = re.compile(pattern, re.IGNORECASE)
        return [acc for acc in self._accounts.values() if regex.search(acc.profile)]

    def to_raw_format(self) -> str:
        """
        Output accounts in raw format (account_id|profile per line).

        This matches the format used by reader.sh.

        Returns:
            Multi-line string with account_id|profile format.
        """
        self._parse_config()
        lines = [acc.display for acc in self._accounts.values()]
        return "\n".join(lines)

    @property
    def config_path(self) -> Optional[str]:
        """Get the path to the loaded config file."""
        return str(self._config_path) if self._config_path else None

    @property
    def count(self) -> int:
        """Get the number of profiles loaded."""
        self._parse_config()
        return len(self._accounts)


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================


def get_aws_profiles(config_path: Optional[str] = None) -> List[str]:
    """
    Get list of all AWS profile names.

    Args:
        config_path: Optional path to AWS config file.

    Returns:
        List of profile names.
    """
    return AWSConfigParser.get_parser(config_path).get_profiles()


def get_aws_account_id(profile: str, config_path: Optional[str] = None) -> Optional[str]:
    """
    Get account ID for a specific profile.

    Args:
        profile: Profile name.
        config_path: Optional path to AWS config file.

    Returns:
        12-digit account ID or None if not found.
    """
    return AWSConfigParser.get_parser(config_path).get_account_id(profile)


def get_all_aws_accounts(config_path: Optional[str] = None) -> List[AWSAccount]:
    """
    Get all AWS accounts from config.

    Args:
        config_path: Optional path to AWS config file.

    Returns:
        List of AWSAccount instances.
    """
    return AWSConfigParser.get_parser(config_path).get_all_accounts()


def find_aws_account(
    account_id: Optional[str] = None,
    profile_pattern: Optional[str] = None,
    config_path: Optional[str] = None,
) -> Optional[AWSAccount]:
    """
    Find AWS account by ID or profile pattern.

    Args:
        account_id: 12-digit AWS account ID to find.
        profile_pattern: Regex pattern to match profile names.
        config_path: Optional path to AWS config file.

    Returns:
        First matching AWSAccount or None.
    """
    parser = AWSConfigParser.get_parser(config_path)

    if account_id:
        return parser.find_by_account_id(account_id)

    if profile_pattern:
        matches = parser.find_by_pattern(profile_pattern)
        return matches[0] if matches else None

    return None
