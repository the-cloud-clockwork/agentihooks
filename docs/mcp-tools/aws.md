---
title: AWS
nav_order: 4
---

# AWS Tools
{: .no_toc }

The AWS category provides utilities for reading and navigating AWS CLI configuration files. These tools help agents discover available AWS profiles and account IDs without requiring direct AWS API calls.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Tools

| Tool | Description |
|------|-------------|
| `aws_get_profiles()` | List all AWS profile names from config |
| `aws_get_account_id()` | Get account ID and role ARN for a specific profile |
| `aws_find_account()` | Find accounts by ID or profile name pattern |

---

## Tool reference

### `aws_get_profiles`

```python
aws_get_profiles(config_path: str = "") -> str
```

Reads the AWS config file and returns all named profiles. Uses `AWS_CONFIG_FILE` env var or `~/.aws/config` by default. Override with `config_path`.

**Returns:** JSON with `count`, `config_path`, `profiles` (list of profile names)

---

### `aws_get_account_id`

```python
aws_get_account_id(profile: str, config_path: str = "") -> str
```

Parses the role ARN for the named profile to extract the account ID.

**Returns:** JSON with `profile`, `account_id`, `role_arn`

---

### `aws_find_account`

```python
aws_find_account(
    pattern: str = "",
    account_id: str = "",
    config_path: str = ""
) -> str
```

Search by account ID (exact match) or profile name (regex pattern). Provide one or the other.

**Returns:** JSON with `found` (bool), `accounts` (list)

---

## Notes

These tools parse the static AWS config file — they do not make live AWS API calls or assume any particular credential provider. They are useful when an agent needs to enumerate available environments before taking action (e.g., choosing which account to deploy to).

For live AWS operations (DynamoDB, Lambda, S3), see [Database](database.md), [Compute](compute.md), and [Storage](storage.md).

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AWS_CONFIG_FILE` | No | `~/.aws/config` | Path to AWS config file |
