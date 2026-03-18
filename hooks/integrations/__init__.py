"""External service integrations.

This package contains clients for external services:
- aws: AWS config parsing
- mailer: SMTP email client
- sqs: AWS SQS messaging with state enrichment
- storage: AWS S3 storage uploads
- webhook: HTTP webhook client
- lambda_invoke: AWS Lambda invocation
- dynamodb: AWS DynamoDB storage
- postgres: PostgreSQL database storage

Integration Configuration:
    All integrations use IntegrationBase for environment variable validation.
    Use `check_all_integrations()` to verify all required env vars are set.

    Example:
        from hooks.integrations import check_all_integrations
        results = check_all_integrations()  # Returns dict of ConfigStatus
"""

from hooks.integrations.aws import (
    AWSAccount,
    AWSConfigParser,
    find_aws_account,
    get_all_aws_accounts,
    get_aws_account_id,
    get_aws_profiles,
)
from hooks.integrations.base import (
    ConfigStatus,
    EnvVarStatus,
    IntegrationBase,
    IntegrationRegistry,
)
from hooks.integrations.dynamodb import (
    DynamoDBClient,
    DynamoDBIntegration,
    DynamoDBResult,
)
from hooks.integrations.dynamodb import (
    put_item as dynamodb_put_item,
)
from hooks.integrations.lambda_invoke import (
    LambdaClient,
    LambdaIntegration,
    LambdaResult,
)
from hooks.integrations.lambda_invoke import (
    invoke as lambda_invoke,
)
from hooks.integrations.mailer import (
    EmailClient,
    EmailConfig,
    EmailIntegration,
    EmailResult,
    load_email_config,
    load_html_template,
    markdown_to_html,
    parse_recipients,
    scan_for_config_files,
    send_email,
    send_from_config,
    send_markdown_file,
    wrap_html_body,
)
from hooks.integrations.postgres import (
    PostgresClient,
    PostgresIntegration,
    PostgresResult,
)
from hooks.integrations.postgres import (
    execute as postgres_execute,
)
from hooks.integrations.postgres import (
    insert as postgres_insert,
)
from hooks.integrations.sqs import (
    SQSClient,
    SQSIntegration,
    SQSResult,
    load_state,
    send_message,
)
from hooks.integrations.storage import (
    S3StorageClient,
    StorageIntegration,
    UploadResult,
    upload_path,
)
from hooks.integrations.webhook import (
    HTTPClient,
    HTTPIntegration,
    HTTPResult,
)
from hooks.integrations.webhook import (
    send as http_send,
)

# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================


def check_all_integrations(print_output: bool = False) -> dict:
    """Check configuration status of all registered integrations.

    Args:
        print_output: If True, print status to stdout

    Returns:
        Dict mapping integration names to their ConfigStatus

    Example:
        from hooks.integrations import check_all_integrations

        # Programmatic check
        results = check_all_integrations()
        for name, status in results.items():
            if not status.is_configured:
                print(f"{name}: Missing {status.missing_required}")

        # With output
        check_all_integrations(print_output=True)
    """
    return IntegrationRegistry.check_all(print_output=print_output)


__all__ = [
    # Integration Base
    "IntegrationBase",
    "IntegrationRegistry",
    "ConfigStatus",
    "EnvVarStatus",
    "check_all_integrations",
    # AWS
    "AWSConfigParser",
    "AWSAccount",
    "get_aws_profiles",
    "get_aws_account_id",
    "get_all_aws_accounts",
    "find_aws_account",
    # Email
    "EmailClient",
    "EmailResult",
    "EmailConfig",
    "EmailIntegration",
    "send_email",
    "send_markdown_file",
    "send_from_config",
    "load_email_config",
    "load_html_template",
    "scan_for_config_files",
    "markdown_to_html",
    "wrap_html_body",
    "parse_recipients",
    # SQS
    "SQSClient",
    "SQSResult",
    "SQSIntegration",
    "send_message",
    "load_state",
    # S3 Storage
    "S3StorageClient",
    "StorageIntegration",
    "UploadResult",
    "upload_path",
    # HTTP Webhook
    "HTTPClient",
    "HTTPResult",
    "HTTPIntegration",
    "http_send",
    # Lambda
    "LambdaClient",
    "LambdaResult",
    "LambdaIntegration",
    "lambda_invoke",
    # DynamoDB
    "DynamoDBClient",
    "DynamoDBResult",
    "DynamoDBIntegration",
    "dynamodb_put_item",
    # PostgreSQL
    "PostgresClient",
    "PostgresResult",
    "PostgresIntegration",
    "postgres_insert",
    "postgres_execute",
]
