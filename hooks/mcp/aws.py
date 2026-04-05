"""AWS MCP tools."""

import json

from hooks.common import log


def register(mcp):
    @mcp.tool()
    def aws_get_profiles(config_path: str = "") -> str:
        """Get list of all AWS profile names from config.

        Args:
            config_path: Optional path to AWS config file (uses default locations if not provided)

        Returns:
            JSON with list of profile names
        """
        try:
            from hooks.integrations.aws import AWSConfigParser, get_aws_profiles

            profiles = get_aws_profiles(config_path if config_path else None)
            parser = AWSConfigParser.get_parser(config_path if config_path else None)

            return json.dumps(
                {
                    "success": True,
                    "count": len(profiles),
                    "config_path": parser.config_path,
                    "profiles": profiles,
                }
            )

        except Exception as e:
            log("MCP aws_get_profiles failed", {"error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def aws_get_account_id(profile: str, config_path: str = "") -> str:
        """Get account ID for a specific AWS profile.

        Args:
            profile: AWS profile name
            config_path: Optional path to AWS config file

        Returns:
            JSON with profile, account_id, and role_arn
        """
        try:
            from hooks.integrations.aws import AWSConfigParser

            parser = AWSConfigParser.get_parser(config_path if config_path else None)
            account = parser.get_account(profile)

            if account:
                return json.dumps(
                    {
                        "success": True,
                        "profile": account.profile,
                        "account_id": account.account_id,
                        "role_arn": account.role_arn,
                    }
                )
            else:
                return json.dumps(
                    {
                        "success": True,
                        "found": False,
                        "profile": profile,
                    }
                )

        except Exception as e:
            log("MCP aws_get_account_id failed", {"profile": profile, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})

    @mcp.tool()
    def aws_find_account(
        pattern: str = "",
        account_id: str = "",
        config_path: str = "",
    ) -> str:
        """Find AWS account by ID or profile name pattern.

        Args:
            pattern: Regex pattern to match profile names (e.g., "platform.*prod")
            account_id: 12-digit AWS account ID to find
            config_path: Optional path to AWS config file

        Returns:
            JSON with matching accounts
        """
        try:
            from hooks.integrations.aws import AWSConfigParser

            parser = AWSConfigParser.get_parser(config_path if config_path else None)

            if account_id:
                account = parser.find_by_account_id(account_id)
                if account:
                    return json.dumps(
                        {
                            "success": True,
                            "found": True,
                            "accounts": [
                                {
                                    "profile": account.profile,
                                    "account_id": account.account_id,
                                    "role_arn": account.role_arn,
                                }
                            ],
                        }
                    )
                else:
                    return json.dumps(
                        {
                            "success": True,
                            "found": False,
                            "search": {"account_id": account_id},
                        }
                    )

            elif pattern:
                matches = parser.find_by_pattern(pattern)
                return json.dumps(
                    {
                        "success": True,
                        "found": len(matches) > 0,
                        "count": len(matches),
                        "search": {"pattern": pattern},
                        "accounts": [
                            {
                                "profile": acc.profile,
                                "account_id": acc.account_id,
                                "role_arn": acc.role_arn,
                            }
                            for acc in matches
                        ],
                    }
                )

            else:
                return json.dumps(
                    {
                        "success": False,
                        "error": "Either pattern or account_id must be provided",
                    }
                )

        except Exception as e:
            log("MCP aws_find_account failed", {"pattern": pattern, "account_id": account_id, "error": str(e)})
            return json.dumps({"success": False, "error": str(e)})
