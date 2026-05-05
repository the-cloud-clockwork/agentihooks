---
title: Compute
nav_order: 9
parent: MCP Tools
---

# Compute Tools
{: .no_toc }

The Compute category provides AWS Lambda invocation with support for synchronous and asynchronous execution modes.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Tools

| Tool | Description |
|------|-------------|
| `lambda_invoke_function()` | Invoke an AWS Lambda function |

---

## Tool reference

### `lambda_invoke_function`

```python
lambda_invoke_function(
    payload: str,
    function_name: str = "",
    async_invoke: bool = False,
    enrich: bool = False
) -> str
```

Invokes a Lambda function with a JSON payload. Falls back to `LAMBDA_FUNCTION_NAME` when `function_name` is omitted.

When `enrich=True`, session state from `conversation_map.json` is merged into the payload before invocation.

**Returns:** JSON with `success` (bool), `status_code`, `function_name`, `invocation_type`, `response_payload`, `error`

---

## Invocation modes

### Synchronous (`async_invoke=False`)

Uses `InvocationType: RequestResponse`. The tool waits for the Lambda to return and includes the response payload in the result.

Best for: short-lived functions, functions that return data the agent needs.

### Asynchronous (`async_invoke=True`)

Uses `InvocationType: Event`. The tool returns immediately after Lambda accepts the invocation — no response payload is returned.

Best for: long-running functions, fire-and-forget workflows, triggering background processing.

The `LAMBDA_INVOCATION_TYPE` environment variable sets the default mode; the `async_invoke` parameter overrides it per-call.

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LAMBDA_FUNCTION_NAME` | Yes* | — | Lambda function ARN or name (*required unless passed per-call) |
| `LAMBDA_INVOCATION_TYPE` | No | `RequestResponse` | Default invocation type (`RequestResponse` or `Event`) |
| `IS_EVALUATION` | No | `false` | Evaluation mode flag (skips actual invocation) |
