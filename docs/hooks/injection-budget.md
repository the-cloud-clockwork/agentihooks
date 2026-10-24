---
title: Hook Injection Budget
nav_order: 6
parent: Hook System
permalink: /docs/hooks/injection-budget/
---

# Hook Injection Budget
{: .no_toc }

AgentiHooks fits Claude Code hook context into a shared event budget so that
operational instructions arrive before optional recalled context. The default is
9,000 UTF-16 code units, below Claude Code's 10,000-character hook limit.

## Table of contents
{: .no_toc .text-delta }

1. TOC
{:toc}

## Why the budget exists

Claude Code limits each hook `additionalContext` field and plain stdout to
10,000 characters. Above that limit it saves the text to a file and shows the
model only a 2,000-character preview. There is no Claude setting to raise this
limit. Previously, a large tool memory banner could occupy the preview and
hide the enforcements that followed it.

AgentiHooks collects typed context blocks before emitting them. `SessionStart`
and `UserPromptSubmit` flush their collected context as plain stdout;
`PreToolUse` combines its buffered context and handler blocks before emitting
`hookSpecificOutput.additionalContext`. Each of these Claude events shares one
budget across its producers. The limit applies to injected text, rather than
the JSON envelope or the model's total context window.

## Priority order

Blocks are sorted by the following kind, preserving arrival order within a
kind, even when everything fits:

| Order | Kind | Content |
|-------|------|---------|
| 1 | Inbox | Claimed inbox messages |
| 2 | Alert | Critical and nuclear broadcasts, operator lines, quota and recycle directives |
| 3 | Enforcement | Runtime doctrine and enforcement reminders |
| 4 | Condition | Context added by conditions |
| 5 | Refocus | Swarm task and phase context |
| 6 | Notice | Other hook notices |
| 7 | Brain | Brain feed and other broadcasts |
| 8 | Tool memory | Relevant past tool errors |
| Last | Unknown | Blocks with an unrecognized kind |

This order gives enforcements priority over conditions, refocus and memory;
it does not guarantee that every enforcement is inside the first 2,000
characters. Inbox messages and alerts precede them.

### When content does not fit

The fitter reserves 200 units for a final one-line notice. It keeps higher
priority content first, shortens a block at a line boundary where possible,
and drops fragments too small to be useful. A single long line can be cut
within the line. The notice names the affected categories, for example:

```text
[context budget: left out tool memory; shortened brain feed and broadcasts to stay under 9000 characters]
```

The notice describes this event's output. It does not delete the source
memory, broadcast, condition or enforcement, and it does not promise a later
retry of omitted content.

Inbox blocks are always retained whole because rendering them marks the
messages delivered. **An oversized inbox can therefore exceed the configured
budget.** Enforcements and alerts have priority, but do not have this exemption;
sufficiently large higher priority content can shorten or displace them too.
Keep instructions and inbox messages concise instead of relying on the preview.

### Tool memory

Tool memory uses plain text without the box drawing frame. By default it
selects at most five entries, each limited to 160 characters, and enters the
budget last. The stored history still defaults to 100 entries. Reducing the
injected count does not reduce the stored history.

## Configuration

The default needs no opt-in and works independently of
[context compression](context-preprocessor.md). Add settings to the existing
AgentiHooks environment configuration when a smaller output is needed:

```dotenv
HOOK_INJECTION_MAX_CHARS=8000
AGENTICORE_TOOL_MEMORY_SHOW=3
```

| Variable | Default | Effect |
|----------|---------|--------|
| `HOOK_INJECTION_MAX_CHARS` | `9000` | Shared Claude event budget in UTF-16 code units. Positive values enable priority sorting and fitting. Zero or a negative value disables both. |
| `AGENTICORE_TOOL_MEMORY_SHOW` | `5` | Maximum selected tool memory entries before event fitting. Each entry has a fixed 160-character limit. |
| `AGENTICORE_TOOL_MEMORY_MAX` | `100` | Maximum stored tool memory entries; separate from the injection budget. |

Use a positive budget below 10,000. Increasing it above the host limit or
setting it to zero restores the risk of file spilling and a truncated preview.
Non-BMP characters, such as some symbols, use two UTF-16 units each; a Python
character count alone can undercount them.

Configuration is loaded by the hook process. These examples also apply values
to a single invocation without changing the stored settings:

```bash
HOOK_INJECTION_MAX_CHARS=8000 AGENTICORE_TOOL_MEMORY_SHOW=3 \
  python3 -m hooks < session-start.json > session-start-context.txt
```

Here `session-start.json` is a captured `SessionStart` payload from the session
being diagnosed. Hook execution has normal lifecycle effects, including inbox
delivery tracking; replay it only in an isolated diagnostic session.

## Check the emitted size

Measure the final injected string after fitting. For plain `SessionStart`
stdout saved as `session-start-context.txt`:

```bash
python3 - <<'PY'
from pathlib import Path
text = Path('session-start-context.txt').read_text()
print(len(text.encode('utf-16-le')) // 2)
PY
```

For a saved Claude `PreToolUse` JSON response in `pre-tool-response.json`:

```bash
python3 - <<'PY'
import json
from pathlib import Path
response = json.loads(Path('pre-tool-response.json').read_text())
text = response.get('hookSpecificOutput', {}).get('additionalContext', '')
print(len(text.encode('utf-16-le')) // 2)
PY
```

A measurement of zero can mean the event emitted no context. Check that the
capture contains the expected event output before treating it as a size proof.
Measure a session start and a tool call separately; one does not establish the
other's size. Plain stdout includes a trailing print newline, so its captured
size may be one unit above the fitted text.

## Troubleshooting

| Symptom | Check and action |
|---------|------------------|
| A context budget notice names missing memory or broadcasts | Lower priority text exceeded the remaining room. Read the source through its normal tool when needed; reduce repeated or oversized content. |
| An enforcement is shortened or missing | Inspect the notice and the inbox, alerts and enforcement volume ahead of it. Shorten the source instructions; priority cannot make unbounded doctrine fit. |
| Claude shows a saved file and only a preview | Measure the final text, confirm a positive budget below 10,000 and inspect oversized inbox messages. A separate hook command's stdout is outside AgentiHooks' producer buffer. |
| Compression is off but a notice still appears | Fitting is independent of compression. The default budget remains active. |

## Other harnesses

The Claude budget and priority sorting do not apply to Codex or Copilot;
those targets retain block arrival order and their own delivery limits.
[Codex](../reference/CODEX-COMPAT.md) installs
`additionalContextLimit: 0` per hook handler to disable its spilling behavior.
Copilot's `postToolUse` context has a separate 10 KB limit, enforced in bytes
with a visible truncation marker. Neither setting raises Claude Code's limit.

See [hook events](events.md) for payloads and
[configuration](../reference/configuration.md#hook-injection-budget) for the
configuration index.
