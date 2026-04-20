---
title: "Pillar 2: Guardrails"
nav_order: 2
parent: The Four Pillars
permalink: /docs/pillars/guardrails/
---

# Pillar 2: Guardrails
{: .no_toc }

**What keeps them safe.**

Claude Code agents are powerful. Without boundaries, they can push to production branches, bump versions without a release workflow, leak credentials into tool calls, loop infinitely on broken commands, or quietly bloat their own instruction files. Guardrails close all of these gaps — automatically, at the hook layer, before any damage reaches your codebase.

> Your fleet operates within boundaries you set.

{: .highlight }
> **10+ guardrails ship active by default.** No configuration required. They fire silently when everything is fine, block loudly when something would go wrong, and surface informational banners for supply chain changes.

## What's new (Unreleased)

- **Two-tier secrets** — code-file writes still hard-block; inline Bash args (no file redirect) scan + log + note only. Lets agents pass a token to `curl -H` without breaking the workflow, while keeping durable storage locked.
- **Session-scoped PR / release / hotfix signals** — one signal phrase from the operator unlocks an entire workflow until session end, instead of expiring per-turn. PR has a 3-per-session counter.
- **Subagent signal isolation** — subagents cannot self-arm release or hotfix signals. Only top-level operator sessions can.
- **`gh pr create --base main` required** — PRs to branches other than main are now blocked. Dev work pushes directly, no PR needed.
- **Dependency banner** — every `pip/npm/cargo/...` install emits a visible banner. Never blocks — surfaces supply chain additions for operator audit.
- **Negation-aware signals** — "don't merge to main" no longer arms the release gate. Matchers skip phrases preceded by `don't`, `not`, `never`, `shouldn't`, `won't`, `can't`.
- **Shared `_strip.py`** — strip heredocs (any delimiter), echo/printf/curl/python-c/jq quoted arguments before guards apply regex. Eliminates false-positive blocks on documentation text in command payloads.
- **Guards fail-closed** — unexpected exceptions in branch_guard / prod_lockdown now emit a stderr warning and the outer main exits 1. Silent hook failures are no longer possible.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## The guardrail pipeline

Every agent action passes through a layered defense before executing. The pipeline runs at three Claude Code hook events: `UserPromptSubmit`, `PreToolUse`, and `PostToolUse`.

```mermaid
flowchart TD
    P([User Prompt]) --> UPS

    subgraph UserPromptSubmit
        UPS[Secrets Scanner\nprompt scan]
    end

    UPS --> TOOL([Tool about to execute])

    subgraph PreToolUse["PreToolUse — security gate"]
        S[Secrets Scanner\ntool input scan]
        B[Branch Guard\ngit command filter]
        V[Version Guard\nmanifest protection]
        C[CLAUDE.md Sanity\nline limit check]
        RC[Retry Circuit Breaker\nhard block]
        FR[File Read Dedup\nredundant read block]
    end

    TOOL --> S --> B --> V --> C --> RC --> FR

    S -->|secret found| BLOCK1([BLOCKED — exit 2])
    B -->|merge/force-push/tag| BLOCK2([BLOCKED — exit 2])
    V -->|version field edit| BLOCK3([BLOCKED — exit 2])
    C -->|file exceeds line cap| BLOCK4([BLOCKED — exit 2])
    RC -->|hard max hit| BLOCK5([BLOCKED — exit 2])
    FR -->|unchanged file| BLOCK6([BLOCKED — exit 2])

    FR -->|all clear| EXEC([Tool executes])

    subgraph PostToolUse["PostToolUse — learning + enforcement"]
        RCT[Retry Circuit Breaker\nfailure tracking]
        BA[Bash Output Filter\nverbose truncation]
    end

    EXEC --> RCT
    EXEC --> BA
    RCT -->|threshold hit| WARN([Inject research instructions])
    BA -->|verbose output| TRUNC([Truncated + re-emitted])
```

---

## Guardrail 1: Secrets scanning

**Hook:** `UserPromptSubmit` (warn), `PreToolUse` (block)
**Default:** On — `AGENTIHOOKS_SECRETS_MODE=standard`

The secrets scanner intercepts credentials before they can enter tool calls, log files, or git history. It runs twice per turn: once on the raw user prompt (warn only) and once on every tool's input parameters (block on detection).

### What it detects

| Pattern name | What it catches |
|---|---|
| `aws_access_key` | AKIA/ASIA/AROA/AIPA prefixed AWS key IDs |
| `aws_secret_key` | `aws_secret_access_key = <value>` assignments |
| `github_token` | `ghp_`, `ghs_`, `github_pat_` tokens |
| `private_key` | PEM-encoded RSA, EC, OPENSSH, PGP private keys |
| `bearer_token` | `Authorization: Bearer <token>` headers |
| `db_url_creds` | `postgres://user:pass@host`, `mysql://`, `mongodb://` URLs |
| `generic_secret` | `PASSWORD=`, `API_KEY=`, `SECRET=` assignments with 8+ char values |

In `strict` mode, three additional patterns activate:

| Pattern name | What it catches |
|---|---|
| `slack_token` | `xox[bpors]-...` Slack OAuth tokens |
| `stripe_key` | `sk_live_`, `sk_test_`, `rk_live_`, `rk_test_` keys |
| `jwt_token` | Three-part base64url JWTs (`eyJ...eyJ...`) |

### Suppression

Lines with `# nosecret` are excluded from scanning. Use this for documentation, test fixtures, or known-safe patterns:

```python
example_key = "AKIAIOSFODNN7EXAMPLE"  # nosecret
```

### Configuration

| Variable | Default | Options |
|---|---|---|
| `AGENTIHOOKS_SECRETS_MODE` | `standard` | `off`, `warn`, `standard`, `strict` |

- `off` — scanning disabled entirely
- `warn` — detects and warns, never blocks
- `standard` — detects standard 7 patterns, blocks on `PreToolUse`
- `strict` — standard + Slack/Stripe/JWT, blocks on `PreToolUse`

---

## Guardrail 2: Retry circuit breaker

**Hook:** `PostToolUse` (track + warn), `PreToolUse` (hard block)
**Default:** On — `RETRY_BREAKER_ENABLED=true`, soft at 5, hard at 10

Claude Code agents will retry failing operations. Without a circuit breaker, a stuck agent can hammer the same broken command dozens of times — burning context, wasting tokens, and never escaping the loop.

The retry circuit breaker tracks consecutive failures per operation. When the same operation fails repeatedly, it escalates through two stages:

**Stage 1 — Soft warning (default: 5 failures):** Injects a banner into Claude's context telling it to stop retrying and instead launch parallel `error-researcher` agents for web search. The agent still *can* proceed — but it now knows it should research first.

**Stage 2 — Hard block (default: 10 failures):** Raises a `BlockAction` via `PreToolUse`, preventing the tool from executing at all. The agent must research and change approach before the block lifts.

### How operations are fingerprinted

The breaker tracks per-operation, not per-tool. Operations are fingerprinted by their base command, with subcommand-level grouping for DevOps tools:

| Tool call | Operation key |
|---|---|
| `Bash: kubectl apply -f deploy.yaml` | `bash:kubectl:apply` |
| `Bash: kubectl get pods` | `bash:kubectl:get` |
| `Bash: terraform plan` | `bash:terraform:plan` |
| `Bash: terraform apply` | `bash:terraform:apply` |
| `Bash: docker build .` | `bash:docker:build` |
| `Edit` | `edit` |

This means `kubectl apply` and `kubectl get` have independent counters — a stuck `apply` doesn't block unrelated reads.

Error text is also normalized (hex, timestamps, paths, numbers stripped) before fingerprinting, so slightly different error messages from the same root cause are treated as the same failure.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `RETRY_BREAKER_ENABLED` | `true` | Master switch |
| `RETRY_BREAKER_MAX` | `5` | Consecutive failures before soft warning |
| `RETRY_BREAKER_HARD_MAX` | `10` | Consecutive failures before hard block |
| `RETRY_BREAKER_TTL` | `3600` | Redis key TTL in seconds (1 hour) |

State is persisted in Redis per session and falls back to in-memory when Redis is unavailable.

---

## Guardrail 3: Branch guard

**Hook:** `PreToolUse` (block, Bash commands only)
**Default:** On — always active

Prevents destructive git operations that bypass the normal PR and release workflow. Fires on `git` commands before they execute.

### Blocked operations

| Command pattern | Reason |
|---|---|
| `git merge ... main` / `git merge ... master` | Direct merges bypass PR review |
| `git reset ... main` / `git reset ... master` | Rewrites protected branch history |
| `git push --force` / `git push -f` | Can destroy remote history |
| `git push --force-with-lease` | Force push variant — same risk |
| `git tag` | Tagging is a release operation; must go through CI |

All blocked operations return exit code 2 with a human-readable explanation and, where applicable, the recommended alternative (e.g., `gh workflow run release.yml` for tagging).

### What is allowed

Normal `git push` (without force flags) is allowed — branch protection is a remote concern handled by GitHub. Read-only operations (`git checkout`, `git switch`, `git pull`, `git log`, `git status`, `git diff`) are never touched.

The guard strips heredoc bodies and quoted commit messages before pattern matching to avoid false positives on message content that happens to mention "main" or "master".

---

## Guardrail 4: Version guard

**Hook:** `PreToolUse` (block, `Edit` and `Write` tools)
**Default:** On — always active

Version fields in project manifests should be managed by the CI release workflow, not by an AI agent editing files directly. The version guard blocks any `Edit` or `Write` operation that would modify a version field in a known manifest file.

### Protected files

`pyproject.toml`, `package.json`, `Cargo.toml`, `setup.cfg`, `setup.py`, `version.txt`, `VERSION`

### Detected patterns

```
version = "1.2.3"        # pyproject.toml, Cargo.toml, setup.cfg
"version": "1.2.3"       # package.json
```

If a `Write` or `Edit` targets one of these files and the new content contains a version field pattern, the operation is blocked with a message directing to the release workflow:

```
BLOCKED: Version field modification in pyproject.toml is not allowed.
Version bumping is handled by the release workflow
(gh workflow run release.yml -f bump=patch|minor|major).
Do not edit version fields manually.
```

---

## Guardrail 5: CLAUDE.md sanity

**Hook:** `PreToolUse` (block, `Edit` and `Write` tools)
**Default:** On — `AGENTIHOOKS_CLAUDE_MD_SANITY_CHECK=true`

`CLAUDE.md` is loaded by Claude Code on every turn. Every line in it costs tokens, every session. Agents writing unchecked content into `CLAUDE.md` files can quietly inflate the per-turn base cost while also degrading instruction quality through drift and dilution.

The sanity check intercepts any `Write` or `Edit` targeting a `CLAUDE.md` or `CLAUDE.local.md` file and simulates what the resulting file would look like. If the resulting line count exceeds the cap, the operation is blocked before the file is touched:

```
BLOCKED: Write to /home/user/.claude/CLAUDE.md would produce 347 lines,
exceeding the CLAUDE.md cap of 200 lines.
Trim the content to 200 lines or fewer before writing.
```

For `Edit` operations, the guard reads the current file from disk, applies the proposed change in memory, and counts the resulting lines — catching incremental bloat that would accumulate over many small edits.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `AGENTIHOOKS_CLAUDE_MD_SANITY_CHECK` | `true` | Enable/disable the guardrail |
| `AGENTIHOOKS_CLAUDE_MD_MAXLINES` | `200` | Maximum allowed lines in CLAUDE.md files |

---

## Guardrail 6: MCP surface area

**Hook:** `SessionStart` (warn)
**Default:** On — `MCP_TOOL_WARN_THRESHOLD=40`

Every MCP tool schema loaded into a session costs tokens — approximately 150 tokens per tool, every turn. With 9 MCP servers and 112 tools, that's ~16,800 schema tokens injected into every single context turn before Claude writes a line of code.

At session start, agentihooks counts the total number of MCP tools across all configured servers. If the count exceeds `MCP_TOOL_WARN_THRESHOLD` (default 40), a warning banner fires:

```
MCP SURFACE AREA: 112 tools across 9 servers (~16,800 schema tokens/turn).
Consider disabling unused servers via /mcp to reduce per-turn overhead.
```

The companion CLI command provides the full breakdown:

```bash
agentihooks mcp report
```

```
MCP Surface Area Report
Total: 9 servers, ~112 tools, ~16,800 schema tokens

Server                         Source   Tools   ~Tokens
hooks-utils                      user      32     4,800
github                           user      40     6,000
postgres                         user      15     2,250
...
```

### Configuration

| Variable | Default | Description |
|---|---|---|
| `MCP_TOOL_WARN_THRESHOLD` | `40` | Tool count threshold for session-start warning |
| `MCP_SCHEMA_AVG_TOKENS` | `150` | Estimated tokens per tool schema |

---

## Guardrail 7: Output token limit

**Hook:** `SessionStart` (inject awareness)
**Default:** Passive — activates when `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is set

When `CLAUDE_CODE_MAX_OUTPUT_TOKENS` is set in the environment, agentihooks injects an awareness message into the session context at startup so Claude knows the limit is in effect and can plan accordingly:

```
OUTPUT TOKEN LIMIT: This session is capped at 8192 output tokens per response.
Plan responses to stay within this limit.
```

Without this injection, Claude can unknowingly start generating a response that will be cut off mid-output by the token limit, resulting in truncated tool calls, incomplete edits, or garbled output. The awareness injection prevents the surprise.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | _(unset)_ | Output token cap; awareness injected when set |

---

## Guardrail 8: File read deduplication

**Hook:** `PreToolUse` (block redundant reads), `PostToolUse` (cache on read)
**Default:** On — `FILE_READ_CACHE_ENABLED=true`

Tracks every file read during a session. If Claude tries to read the same file again and the file has not changed on disk since the last read, the operation is blocked:

```
BLOCKED: /path/to/file.py was already read this session and is unchanged on disk.
Use the content already in your context window.
```

This prevents a common and expensive pattern: Claude re-reading the same source file 3–5 times over the course of a long session, each read injecting the same content into the context window.

File identity is tracked by path + `mtime`. A file modified on disk since the last read passes through normally, and the cache entry is updated.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `FILE_READ_CACHE_ENABLED` | `true` | Master switch |
| `FILE_READ_CACHE_BACKEND` | `redis` | `redis` or `memory` |
| `FILE_READ_CACHE_TTL` | `21600` | Redis key TTL (6 hours) |

---

## All guardrails at a glance

| # | Guardrail | Hook event | Default | Block or warn | Config key |
|---|---|---|---|---|---|
| 1 | Secrets scanner | `UserPromptSubmit` / `PreToolUse` | On | Warn / Block | `AGENTIHOOKS_SECRETS_MODE` |
| 2 | Retry circuit breaker | `PostToolUse` / `PreToolUse` | On | Warn → Block | `RETRY_BREAKER_ENABLED` |
| 3 | Branch guard | `PreToolUse` | On | Block | _(always on)_ |
| 4 | Version guard | `PreToolUse` | On | Block | _(always on)_ |
| 5 | CLAUDE.md sanity | `PreToolUse` | On | Block | `AGENTIHOOKS_CLAUDE_MD_SANITY_CHECK` |
| 6 | MCP surface area | `SessionStart` | On | Warn | `MCP_TOOL_WARN_THRESHOLD` |
| 7 | Output token limit | `SessionStart` | Passive | Inject awareness | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` |
| 8 | File read dedup | `PreToolUse` / `PostToolUse` | On | Block | `FILE_READ_CACHE_ENABLED` |

---

## Exit code semantics

Guardrails that block communicate via exit code 2. Claude Code cancels the tool action and displays the hook's stderr output as a warning to the agent.

| Exit code | Meaning |
|---|---|
| `0` | Allow — tool proceeds |
| `2` | Block — tool cancelled, stderr shown as warning |

This is the same mechanism Claude Code uses for all hook blocks — guardrails are first-class citizens of the hook event pipeline.

---

## Hardening your setup

The defaults cover the most common failure modes. For teams or higher-stakes environments, consider these adjustments:

```bash
# ~/.agentihooks/.env

# Stricter secrets scanning (adds Slack, Stripe, JWT patterns)
AGENTIHOOKS_SECRETS_MODE=strict

# Tighter retry tolerance (warn sooner, hard block sooner)
RETRY_BREAKER_MAX=3
RETRY_BREAKER_HARD_MAX=7

# Tighter CLAUDE.md size cap
AGENTIHOOKS_CLAUDE_MD_MAXLINES=150

# Warn earlier on MCP surface area
MCP_TOOL_WARN_THRESHOLD=25

# Set output token ceiling
CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
```

To verify all guardrails are active after installation:

```bash
agentihooks status
```

The status output lists each guardrail with its current state (enabled/disabled), mode, and thresholds. Inside a live session, `/agentihooks` shows the same panel alongside real-time session metrics.
