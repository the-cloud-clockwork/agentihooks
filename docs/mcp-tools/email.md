---
title: Email
nav_order: 5
parent: MCP Tools
---

# Email Tools
{: .no_toc }

The Email category provides SMTP email sending with support for plain text, HTML, and markdown content. Markdown content is automatically converted to styled HTML before sending.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Tools

| Tool | Description |
|------|-------------|
| `email_send()` | Send an email with flexible content options |

---

## Tool reference

### `email_send`

```python
email_send(
    to: str,
    subject: str,
    body: str = "",
    html: str = "",
    markdown: str = "",
    title: str = "",
    template: str = ""
) -> str
```

Sends an email. Content priority: `html` > `markdown` > `body`. Supply exactly one.

- **`body`** — plain text content
- **`html`** — raw HTML content
- **`markdown`** — markdown string auto-converted to HTML
- **`title`** — used as a heading in the converted HTML
- **`template`** — HTML template with `{{placeholder}}` substitution

`to` accepts a single address or comma-separated list.

**Returns:** JSON with `success` (bool), `recipients_count`, `error`

---

## SMTP modes

### Relay mode (no auth)

Used when your SMTP server accepts unauthenticated connections (e.g., an internal mail relay).

Set only: `SMTP_SERVER`, `SMTP_PORT`, `SENDER_EMAIL`

Leave `SMTP_USER` and `SMTP_PASS` unset.

### Authenticated mode

Used when the SMTP server requires login (e.g., Gmail, SES SMTP, corporate relay with auth).

Set: `SMTP_SERVER`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SENDER_EMAIL`

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SMTP_SERVER` | Yes | — | SMTP server hostname |
| `SMTP_PORT` | No | `25` | SMTP port |
| `SMTP_SERVER_IP` | No | — | Optional fallback IP for the SMTP server |
| `SMTP_USER` | Auth mode | — | SMTP username |
| `SMTP_PASS` | Auth mode | — | SMTP password |
| `SENDER_EMAIL` | Yes | — | From address |
