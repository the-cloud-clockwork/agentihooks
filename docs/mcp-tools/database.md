---
title: Database
nav_order: 8
parent: MCP Tools
---

# Database Tools
{: .no_toc }

The Database category covers DynamoDB and PostgreSQL integrations. All tools support optional **state enrichment** to automatically inject session context into stored records.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Tools

| Tool | Description |
|------|-------------|
| `dynamodb_put_item()` | Write an item to DynamoDB |
| `postgres_execute()` | Execute a parameterized SQL query |

---

## Tool reference

### `dynamodb_put_item`

```python
dynamodb_put_item(
    payload: str,
    table_name: str = "",
    partition_key: str = "",
    sort_key: str = "",
    enrich: bool = False
) -> str
```

Writes a JSON item to DynamoDB. Falls back to `DYNAMODB_TABLE_NAME`, `DYNAMODB_PARTITION_KEY`, and `DYNAMODB_SORT_KEY` env vars when parameters are omitted. When `enrich=True`, merges `conversation_map.json` fields into the item.

**Returns:** JSON with `success` (bool), `table_name`, `partition_key`, `partition_key_value`, `sort_key`, `sort_key_value`, `error`

---

### `postgres_execute`

```python
postgres_execute(query: str, params: str = "[]") -> str
```

Executes a parameterized SQL query using `%s` placeholders. `params` must be a JSON array of values.

```python
postgres_execute(
    query="UPDATE runs SET status = %s WHERE id = %s",
    params='["completed", 42]'
)
```

**Returns:** JSON with `success` (bool), `rows_affected`, `error`

---

## State enrichment

When `enrich=True`, `conversation_map.json` fields are merged into the item/row before it is written. This is useful for automatically tagging records with session metadata (agent name, correlation ID, etc.).

---

## Environment variables

### DynamoDB

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DYNAMODB_TABLE_NAME` | Yes* | — | Table name (*required unless passed per-call) |
| `DYNAMODB_PARTITION_KEY` | No | `session_id` | Partition key attribute name |
| `DYNAMODB_SORT_KEY` | No | — | Sort key attribute name (omit if table has no sort key) |
| `DYNAMODB_ENDPOINT_URL` | No | — | Custom DynamoDB endpoint (for local testing with DynamoDB Local) |
| `IS_EVALUATION` | No | `false` | Evaluation mode flag |

### PostgreSQL

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `POSTGRES_HOST` | Yes | — | Database host |
| `POSTGRES_NAME` | Yes | — | Database name |
| `POSTGRES_USERNAME` | Yes | — | Username |
| `POSTGRES_PASSWORD` | Yes | — | Password |
| `POSTGRES_PORT` | No | `5432` | Port |
| `POSTGRES_TABLE` | No | — | Default table name |
| `IS_EVALUATION` | No | `false` | Evaluation mode flag |
