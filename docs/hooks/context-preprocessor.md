---
title: Context Preprocessor
nav_order: 3
parent: Hook System
permalink: /docs/hooks/context-preprocessor/
---

# Context Preprocessor
{: .no_toc }

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

---

## Overview

The Context Preprocessor compresses injected content — broadcast banners and verbose tool output — before it lands in the model's context window.

> **History:** the preprocessor was originally built to fit periodic rules/CLAUDE.md re-injection inside an 8,000-character budget. That periodic re-injection was **removed 2026-07-20** (the harness already loads rules and CLAUDE.md at position zero). The preprocessor survived because the same token-compression logic still pays off on the injections that remain. Its config keeps the historical `CONTEXT_REFRESH_*` names for back-compat.

### The problem

Injected banners (fleet broadcasts, brain drumbeat) and captured tool output add up across a long session. Every character re-emitted is a token spent. Left raw, a chatty broadcast stream or a wall of `git log` output burns budget that could hold real work.

### The insight

LLMs predict over subword tokens, not characters. The BPE tokenizer splits "authentication" into tokens like `["auth", "ent", "ication"]` (3 tokens). If you write "auth" instead, it is 1 token and the model activates the same semantic representation — the surrounding tokens ("credentials", "secrets", "env vars") provide enough signal for the attention heads to reconstruct full meaning.

This property means we can compress injected content by 30-55% while preserving LLM comprehension, as long as we protect tokens that carry critical operational semantics (negation, action verbs, identifiers).

### Scope

`CONTEXT_COMPRESSION_SCOPE` has two values:

- `all` — compression is applied to every `inject_context()` / `inject_banner()` call (session-start banners, secrets warnings, tool memory, circuit breaker messages, context threshold warnings) and bash output filter `additionalContext` results.
- `refresh` (default) — historically the value that let the removed context-refresh path compress its own rules/CLAUDE.md payload. **With that path removed 2026-07-20, `refresh` no longer triggers compression anywhere** — it is effectively `off`. Set `CONTEXT_COMPRESSION_SCOPE=all` to get any compression on the injections that remain.

> The default is left at `refresh` for config back-compat; flipping the fleet default to `all` is a separate decision. Until then, opt in explicitly per profile.

---

## Compression Levels

| Level | Name | Transforms | Token Savings | Per 100-Turn Session |
|-------|------|------------|--------------|---------------------|
| 0 | `off` | None (passthrough) | 0% | 0 tokens |
| 1 | `light` | Strip markdown formatting | ~5-10% | ~200-500 tokens |
| 2 | `standard` | Level 1 + remove filler words + apply abbreviation dictionary | ~10-20% | ~2,000-4,000 tokens |
| 3 | `aggressive` | Level 2 + internal vowel removal on long common words | ~20-35% | ~4,000-8,000 tokens |

{: .highlight }
**Level default is `standard`, but nothing is compressed until `CONTEXT_COMPRESSION_SCOPE=all`.** Set it in `~/.agentihooks/.env` to apply compression to all hook injections and tool output.

Session savings scale with session length and how many injections fire. With `scope=all`, savings compound across every `inject_context`, `inject_banner`, and `additionalContext` call in the hook system.

---

## The Compression Pipeline

Each level is additive — level N applies everything from levels below it. The safety protection mask runs first, before any transform.

### Level 1: Markdown Formatting Removal

Strips structural markdown that carries zero semantic weight for the LLM:

| Transform | Before | After |
|-----------|--------|-------|
| Headers | `## Delegation Map` | `[Delegation Map]` |
| Tables | `\| Key \| Value \|` (multi-row) | `Key: Value` (flat per row) |
| Mermaid blocks | `` ```mermaid ... ``` `` | `[diagram removed]` |
| Bold/italic | `**important**` | `important` |
| Horizontal rules | `---` | *(removed)* |

### Level 2: Filler Words and Abbreviations

**Filler word removal** — removes low-information function words:

| Before | After |
|--------|-------|
| `The system is configured to use Redis` | `system configured use Redis` |
| `All of the deployment operations` | `All deploy operations` |
| `This is a hard rule that applies` | `hard rule applies` |

Target words: `a`, `an`, `the`, `is`, `are`, `was`, `were`, `be`, `been`, `being`, `in`, `on`, `at`, `to`, `of`, `for`, `that` (conjunction), `which`, `with` (when not part of a command).

**Abbreviation substitution** — replaces common DevOps terms using a dictionary:

| Full term | Abbreviation |
|-----------|-------------|
| `authentication` | `auth` |
| `kubernetes` | `k8s` |
| `configuration` | `cfg` |
| `environment` | `env` |
| `production` | `prod` |
| `deployment` | `deploy` |
| `infrastructure` | `infra` |
| `repository` | `repo` |
| `namespace` | `ns` |
| `application` | `app` |
| `database` | `db` |

Full dictionary in `hooks/context/data/abbreviations.json` (~50 entries).

### Level 3: Internal Vowel Removal

Removes vowels that are flanked by consonants on both sides, in words of 7+ characters:

| Before | After |
|--------|-------|
| `instruction` | `instrction` |
| `protection` | `prtctn` |
| `collaborative` | `collbrtve` |
| `mandatory` | `mndtry` |

Leading vowels are preserved (they anchor word shape for the tokenizer). Short words and exclusion-set words (like `error`, `issue`, `order`) are never disemvoweled.

---

## Safety Rules — Protected Tokens

{: .important }
The preprocessor NEVER modifies tokens in protected categories. This is enforced by a span-based protection mask that is computed before any transform runs.

### Protected categories

**1. Code blocks** — fenced (` ``` `) and inline (`` ` ``):

```
`kubectl delete pod` → preserved exactly
```

This is the most important protection. Commands, paths, env var names, and identifiers are almost always inside code spans in well-authored rule files.

**2. Negation words** — matched as whole words:

`never`, `don't`, `not`, `no`, `without`, `cannot`, `can't`, `won't`, `shouldn't`, `must not`, `do not`

Compressing a negation risks flipping the meaning of a rule.

**3. Assertion words** — operational imperatives:

`always`, `must`, `required`, `mandatory`, `only`, `exactly`, `strictly`

**4. Action verbs** — high-stakes operations:

`push`, `delete`, `commit`, `deploy`, `block`, `destroy`, `drop`, `truncate`, `kill`, `terminate`, `rollback`, `revert`, `reset`, `force`, `override`, `disable`, `remove`, `purge`, `wipe`

**5. ALL_CAPS identifiers** — env var names:

`CONTEXT_COMPRESSION_SCOPE`, `KUBECTL_NAMESPACE`, `AWS_REGION`, etc. Pattern: `[A-Z][A-Z0-9_]{2,}`

**6. Numbers and thresholds**:

`8000`, `20`, `3600`, `80%`, `512MiB` — any numeric literal including byte sizes and percentages.

**7. File paths and CLI commands**:

`~/.agentihooks/.env`, `/home/user/.claude/rules/`, `kubectl delete`, `helm upgrade`, `git push --force`

---

## Algorithm: Protection Mask

The protection mask is a list of `(start, end)` character-offset spans computed from the raw text. Each transform function uses `_apply_masked()` which:

1. Finds all regex matches for the transform
2. Checks each match span against the protection mask
3. Skips any match that overlaps a protected span
4. Applies non-overlapping matches only

The mask is **rebuilt after each transform** because text modifications shift character offsets. This is O(n*m) per transform (n=matches, m=protected spans) but acceptable given rule files are a few KB.

```
text = "Never run `kubectl delete` in production"

Protection mask:
  [0, 5)    = "Never"           (negation)
  [10, 26)  = "`kubectl delete`" (code span)
  [30, 40)  = "production"      (after abbrev: becomes "prod", but in L1 it's not yet abbreviated)

Level 2 filler removal:
  "run" → not protected, but it's a verb not in filler list → kept
  "in"  → filler word, not in protected span → removed

Result: "Never run `kubectl delete` prod"
```

---

## The Abbreviation Dictionary

Location: `hooks/context/data/abbreviations.json`

Structure:

```json
{
  "_version": 1,
  "entries": {
    "authentication": "auth",
    "kubernetes": "k8s",
    "configuration": "cfg"
  }
}
```

Entries are applied **longest-match first** to avoid partial collisions (e.g., "authentication" before "auth").

### Extending the dictionary

Set `CONTEXT_REFRESH_ABBREV_FILE` to the path of a custom JSON file with the same structure. Your entries are shallow-merged on top of the built-in dictionary (your entries win on collision).

```bash
# In ~/.agentihooks/.env
CONTEXT_REFRESH_ABBREV_FILE=/home/user/.agentihooks/custom-abbrevs.json
```

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_REFRESH_COMPRESSION` | `standard` | Compression level: `off`, `light`, `standard`, `aggressive` |
| `CONTEXT_COMPRESSION_SCOPE` | `refresh` | `all` compresses all injections + tool output. `refresh` (default) is now a no-op — see below. |
| `CONTEXT_REFRESH_ABBREV_FILE` | *(empty)* | Path to user-supplied abbreviation dictionary (JSON). Merged on top of built-in. |

### Scope

Set `CONTEXT_COMPRESSION_SCOPE=all` to compress **everything** that flows through the hook system:

- All `inject_context()` / `inject_banner()` calls (session start banners, secrets warnings, tool memory, circuit breaker messages, context threshold warnings)
- Bash output filter `additionalContext` results (kubectl describe, docker logs, git diffs)

The other value, `refresh` (the default), historically scoped compression to the context-refresh rules/CLAUDE.md payload. **That path was removed 2026-07-20, so `refresh` now compresses nothing — it is effectively `off`.** The only two call sites that gate compression (`hooks/common.py`, `hooks/hook_manager.py`) check `scope == "all"`. To get any compression, set `all`.

```bash
# In ~/.agentihooks/.env
CONTEXT_REFRESH_COMPRESSION=standard
CONTEXT_COMPRESSION_SCOPE=all
```

**Not compressed** (by design): user prompts, stderr block messages, log file output.

---

## Full Compression Example

Source: a typical clearance rule file (811 chars original).

### Level 0 (off) — 811 chars

```
# Operator Clearance — Full by Default

## Default: Full Clearance

You have full clearance at all times unless the operator explicitly restricts it.

Full clearance means:
- Push to any branch including main
- Force push when needed
- Destructive operations (rm, kubectl delete, docker rm) — just do it
- Production operations — just do it
- All git operations — just do it

## The One Absolute: Secrets

No clearance level — not even full — permits handling credentials, API keys,
tokens, or passwords in plaintext. Reference via env vars only.

## Restricting and Restoring

- "restrict clearance" / "careful mode" → ask before destructive/production ops
- "full clearance" / "back to normal" → default behavior restored
- Restriction is per-task, reverts automatically after task completion
```

### Level 1 (light) — ~620 chars

```
[Operator Clearance — Full by Default]

[Default: Full Clearance]

You have full clearance at all times unless the operator explicitly restricts it.

Full clearance means:
- Push to any branch including main
- Force push when needed
- Destructive operations (rm, kubectl delete, docker rm) — just do it
- Production operations — just do it
- All git operations — just do it

[The One Absolute: Secrets]

No clearance level — not even full — permits handling credentials, API keys,
tokens, or passwords in plaintext. Reference via env vars only.

[Restricting and Restoring]

- "restrict clearance" / "careful mode" → ask before destructive/production ops
- "full clearance" / "back to normal" → default behavior restored
- Restriction per-task, reverts automatically after task completion
```

### Level 2 (standard) — ~480 chars

```
[Operator Clearance — Full by Default]

[Default: Full Clearance]

You have full clearance unless operator explicitly restricts it.

Full clearance means:
- Push any branch including main
- Force push when needed
- Destructive ops (rm, kubectl delete, docker rm) — just do it
- prod ops — just do it
- All git ops — just do it

[One Absolute: Secrets]

No clearance level permits handling credentials, API keys, tokens, or passwords plaintext. Reference via env vars only.

[Restricting and Restoring]

- "restrict clearance" / "careful mode" → ask before destructive/prod ops
- "full clearance" / "back to normal" → default behavior restored
- Restriction per-task, reverts automatically after task completion
```

### Level 3 (aggressive) — ~410 chars

```
[Opertr Clearance — Full by Default]

[Default: Full Clearance]

You have full clearance unless opertr explctly rstrcts it.

Full clearance means:
- Push any branch inclding main
- Force push when needed
- Destructive ops (rm, kubectl delete, docker rm) — just do it
- prod ops — just do it
- All git ops — just do it

[One Absolte: Secrets]

No clearance level permits handlng credntls, API keys, tokens, or passwords plaintext. Reference via env vars only.

[Rstrcting and Restrng]

- "restrict clearance" / "careful mode" → ask before destructive/prod ops
- "full clearance" / "back to normal" → default behavr restored
- Restriction per-task, reverts autmtcly after task completn
```

---

## Limitations

- **All-command rules**: If a rule file is entirely code blocks and identifiers, the protection mask covers the whole document and no compression occurs. This is correct behavior.
- **No semantic validation**: The preprocessor cannot detect if compression changes the operational meaning of a rule in edge cases. It relies on the protection categories to prevent this.
- **Dictionary maintenance**: The abbreviation dictionary is manually curated. New DevOps terms need to be added as they emerge.
- **Level 3 readability**: Aggressive vowel removal produces text that is harder for humans to read in logs. It remains fully comprehensible to the LLM.

---

## Future Scope

The Context Preprocessor is designed to grow into a standalone service:

- **User message preprocessing**: compress verbose user inputs before they consume context budget
- **Tool output compression**: apply abbreviation and formatting reduction to large tool outputs (complementing `bash_output_filter.py`)
- **Adaptive compression**: dynamically increase compression level as context usage approaches the window limit (integrating with context audit data)
- **Custom compression profiles**: per-project or per-domain compression dictionaries
- **Compression analytics**: track compression ratios and token savings across sessions
