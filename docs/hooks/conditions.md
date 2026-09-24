---
title: Conditions
nav_order: 5
parent: Hook System
permalink: /docs/hooks/conditions/
---

# Conditions
{: .no_toc }

A condition is a script in a bundle or profile that runs on every tool call it
matches. It can add context for the agent, rewrite the tool input before the tool
runs, replace the tool output the agent sees, or deny the call. The filename is the
whole configuration: `pre-bash.git-guard.sh` runs before every Bash call that runs
`git`.

1. TOC
{:toc}

---

## Quick example

```bash
mkdir -p ~/my-bundle/.claude/conditions
cat > ~/my-bundle/.claude/conditions/pre-bash.kubectl-readonly.sh <<'EOF'
#!/usr/bin/env bash
cmd=$(jq -r '.tool_input.command')
if grep -Eq 'kubectl (edit|patch|apply|delete)' <<<"$cmd"; then
  echo "kubectl writes go through GitOps" >&2
  exit 2
fi
EOF
agentihooks conditions list --tool Bash --command "kubectl delete pod x"
```

No install step: the hook reads condition directories in place on every call.

## Where conditions live

| Layer (lowest to highest priority) | Directory | Source label |
|---|---|---|
| Bundle (global) | `<bundle>/.claude/conditions/` | `bundle` |
| Each profile in the active chain | `<profile>/.claude/conditions/` | `profile:<name>` |
| Runtime (machine-wide, used when no bundle is linked) | `~/.agentihooks/conditions/` | `runtime` |
| Directory (the repository the session runs in) | `<git-root>/.agentihooks/conditions/` | `directory` |

The profile chain is the one installed for the running harness
(`agentihooks init --profile anton,brain` gives two profile layers). Profiles
resolve the same way as enforcements: built-in, then `<bundle>/profiles/<name>`,
then linked profiles.

A later layer overrides an earlier one when the `<step>-<matcher>-<name>` part of
the filename is the same, so a profile can replace a global condition by shipping
a file with the same name.

The directory layer is the repository's own `.agentihooks/` folder, next to its
`enforcements.json`. It runs only when the repository is trusted:

- it has no `origin` remote (local-only work), or
- the `origin` owner matches the linked bundle's `origin` owner, or
- the owner is listed in `CONDITIONS_TRUSTED_OWNERS` (comma list; `*` trusts all).

A repository cloned from anyone else keeps its conditions on disk but they never
run; `agentihooks conditions list` shows the layer as skipped with the owner.

## Creating conditions from a session

Tell the agent what you want, in your own words, with a phrase such as *set a
condition*, *add a condition*, *create a new condition*, *update the condition*,
*remove the condition*:

> set a condition: after every `kubectl apply`, remind me the change must go through GitOps

The agent writes the script and calls `condition_set` on the `hooks-utils` MCP
server. The condition is live from the next matching tool call; nothing to open,
save or install.

| Tool | Gated | Does |
|---|---|---|
| `condition_set` | yes | writes `<step>-<matcher>-<name>[.async].<ext>` with the script body, executable |
| `condition_clear` | yes | deletes a condition file (`scope` picks a layer when the name exists in several) |
| `condition_list` | no | layers, trust, active conditions in order, files that do not parse |
| `condition_show` | no | the script of one active condition |

Where `condition_set` writes (`scope`):

| `scope` | Target |
|---|---|
| `global` (default) | `<bundle>/.claude/conditions/`; `~/.agentihooks/conditions/` when no bundle is linked |
| `profile` | `<profile>/.claude/conditions/` of the chain's first profile, or `profile="<name>"` |
| `directory` | `<git-root>/.agentihooks/conditions/` of the session's repository |

Files written into the bundle or a repository are ordinary working-tree changes:
commit them the usual way to keep them.

**The gate.** Agents never create, change or remove conditions on their own.
The `UserPromptSubmit` hook arms a per-session gate only when the `prompt` field —
the text you typed — contains one of the phrases above, not negated (*don't add a
condition* does not arm it). Tool output, files, broadcasts and injected context
never arm it. The gate closes at the end of the turn (`Stop`) and after an hour at
most. While it is closed:

- `condition_set` / `condition_clear` are denied in PreToolUse and refused by the
  MCP server itself;
- `Write`, `Edit`, `MultiEdit` and `NotebookEdit` on any path under
  `.claude/conditions/` or `.agentihooks/conditions/` are denied;
- `Bash` commands that touch those folders are denied unless every program in
  them only reads (`ls`, `cat`, `grep`, `find` without `-delete`/`-exec`, `git
  add/commit/status/diff/log/push`, …) and nothing is redirected into a file.

The Bash check is a pattern match and cannot see every indirect write; the MCP
gate and the file-tool gate are exact.

## Filename grammar

```
<step>-<matcher>-<name>[.async].<ext>
```

| Part | Rule |
|---|---|
| `step` | `pre` (PreToolUse) or `post` (PostToolUse) |
| `matcher` | everything between the first and the last `-`; see [Matcher grammar](#matcher-grammar) |
| `name` | the last `-` segment; use `_`, never `-` (`ls_formatter`) |
| `.async` | optional; run detached, output ignored |
| `ext` | required; `.sh`/`.bash` run with `bash`, `.py` with the agentihooks Python; any other extension runs directly when the file is executable (shebang) |

Ignored silently: names starting with `.` or `_`, names ending in `~`, and `*.md`
(so a `README.md` can sit next to the scripts). Any other file that does not parse
is listed by `agentihooks conditions list` with the reason.

Because the matcher is "everything between", MCP tool names that contain hyphens
work as-is:

```
pre-mcp__gateway-tools__github-create_pull_request-audit.py
```

## Matcher grammar

One grammar serves conditions and the [enforcement `matcher`](#enforcement-matcher).
Matching is case-insensitive and runs on the tool name after target normalization,
so `bash` also matches the Codex and Copilot shell tools.

| Token | Matches |
|---|---|
| `any` | every tool |
| `bash`, `read`, `edit`, `write`, `webfetch`, `agent`, … | that tool |
| `mcp` | every MCP tool |
| `mcp__<server>` | every tool of one MCP server |
| `mcp__<server>__<tool>` | one MCP tool |
| `bash.<cli>` | a Bash call in which any simple command runs `<cli>` |
| `a+b` | either alternative (`edit+write`, `bash.git+bash.gh`) |

`bash.<cli>` looks at the program of every simple command in the command line.
Quoted text, heredoc bodies and redirection targets are not programs. Leading
`VAR=value` assignments and the wrappers `sudo`, `env`, `command`, `time`, `nohup`,
`exec` and `timeout <n>` are skipped, and the path is reduced to its basename:

| Command | Programs |
|---|---|
| `cd repo && git push` | `cd`, `git` |
| `grep "a; rm x" f \| wc -l` | `grep`, `wc` |
| `FOO=1 sudo -u bob git status` | `git` |
| `timeout 30 /usr/bin/python3 -m x` | `python3` |

`mcp__<server>` matches the whole server here, unlike a Claude Code settings
matcher, where the same string matches no tool.

## Script contract

**stdin** is the hook payload as JSON, after target normalization:
`tool_name`, `tool_input`, `tool_response` (post only), `tool_use_id`,
`session_id`, `cwd`, `transcript_path`, `permission_mode`, `hook_event_name`.

**Environment** adds scalars for shell scripts:

| Variable | Value |
|---|---|
| `AH_STEP` | `pre` or `post` |
| `AH_EVENT` | `PreToolUse` or `PostToolUse` |
| `AH_TOOL_NAME` | normalized tool name |
| `AH_TOOL_USE_ID` | the call's id, for keying concurrent runs |
| `AH_SESSION_ID`, `AH_CWD`, `AH_TRANSCRIPT_PATH`, `AH_PERMISSION_MODE` | from the payload |
| `AH_TARGET` | `claude`, `codex` or `copilot` |
| `AH_CONDITION` | the condition's filename |
| `AH_CONDITION_SOURCE` | `bundle` or `profile:<name>` |

The working directory is the session's `cwd`. The conversation is available through
`transcript_path`; for example, the last assistant text:

```bash
jq -rs '[.[] | select(.type=="assistant") | .message.content[]? | select(.type=="text") | .text] | last' "$AH_TRANSCRIPT_PATH"
```

**stdout** is one of:

- nothing: no effect;
- plain text: context for the agent;
- a JSON object:

| Field | Step | Effect |
|---|---|---|
| `context` | both | text added to the agent's context |
| `tool_input` | pre | shallow-merged over the tool input |
| `tool_output` | post | replaces what the agent sees as the tool result |
| `decision` | both | `allow`, `ask` or `deny` |
| `reason` | both | explanation attached to the decision |

`tool_output` must match the tool's output shape, since the host ignores a value
that does not. A string is accepted for Bash, where it replaces `stdout` and clears
`stderr`, and for MCP tools, which are not validated. For other built-in tools, pass
the full object.

**Exit codes:**

| Code | Effect |
|---|---|
| `0` | stdout is applied |
| `2` | pre: deny the call; post: block with a reason. stderr is the reason |
| other, or timeout | the condition is skipped, logged, and named in a one-line context note |

## Execution

- Synchronous conditions that match one call run in parallel, each on the original
  payload, each in its own process group. A timeout kills the whole group.
- Results merge in a fixed order: the bundle layer, then each profile layer, then by
  filename.
  - `context` parts are concatenated.
  - `tool_input` patches merge in order, so a later key wins.
  - `tool_output` from the last writer wins, and more than one writer is logged.
  - `decision` precedence is `deny` > `ask` > `allow`.
- `.async` conditions start detached before the synchronous ones. They cannot
  change the call.
- Parallel tool calls run separate hook processes, so a condition can run
  concurrently with itself. Key any shared state by `AH_TOOL_USE_ID`.

Conditions run first in PreToolUse. A rewritten input is what every guardrail
after them judges (secrets, branch, kubectl, prod lockdown, credential guard, retry
breaker), so a rewrite cannot route a command around a guard. Hooks registered
separately in `settings.json`, such as a bundle's own `credential-guard.sh`, run
in parallel on the original input and can still block it.

## Rewrites and permissions

Claude Code applies a rewritten input only together with a permission decision.

| Session mode | Condition returns `tool_input` and… | Result |
|---|---|---|
| `bypassPermissions` | no decision | rewrite applied, `allow` |
| any | `"decision": "allow"` | rewrite applied and auto-approved |
| any | `"decision": "ask"` | rewrite applied, user asked (denied in `-p`) |
| not bypass | no decision | rewrite dropped, the agent is told why |

A rewrite never widens permissions without the condition saying so. Deny and ask
rules in settings still apply to the rewritten input.

## Harness support

| Capability | Claude Code | Codex | Copilot |
|---|---|---|---|
| Context (pre) | yes | no (no PreToolUse context channel) | yes |
| Context (post) | yes | yes | yes |
| Deny (pre) | yes | yes | yes |
| `ask` | yes | treated as deny | treated as deny |
| Input rewrite | yes | dropped with a note | dropped with a note |
| Output rewrite / post block | yes | dropped / becomes context | dropped / becomes context |

PostToolUse does not fire for tools that fail, so a `post` condition never sees
failed calls.

## Performance

Which conditions fire is answered from an index cached at
`~/.agentihooks/cache/conditions-index.<target>.<checkout-and-repo-crc>.json`. A cached lookup
costs one `stat` of `state.json`, one `stat` per candidate directory, and one small
JSON read; no directory is listed. The index is rebuilt when:

- a file is added, removed or renamed in a condition directory (its mtime changes);
- `state.json` changes (a new profile chain or bundle);
- a candidate profile directory appears or disappears.

Editing a script needs no rebuild, because scripts are executed fresh every time.
With nothing matching, a tool call pays only that lookup. A match pays one process
start per condition (a few ms for `bash`, tens of ms for Python).

## Inspecting

```bash
agentihooks conditions list
agentihooks conditions list --step pre
agentihooks conditions list --tool Bash --command "cd x && git push"
agentihooks conditions list --tool mcp__gateway-tools__github-create_pull_request
```

The output shows each layer directory and how many conditions it holds, the
conditions in execution order, the files that do not parse (with the reason), and,
with `--tool`, which conditions would fire. `AGENTIHOOKS_TARGET=codex` inspects
the codex profile chain.

## Examples

**Keep `git log` short** (`pre-bash.git-short_log.py`):

```python
import json, sys

payload = json.load(sys.stdin)
command = payload["tool_input"]["command"]
if command.strip() == "git log":
    print(json.dumps({"tool_input": {"command": "git log --oneline -20"}, "decision": "allow"}))
```

**Trim long test output** (`post-bash.pytest-trim.py`):

```python
import json, sys

payload = json.load(sys.stdin)
lines = payload["tool_response"].get("stdout", "").splitlines()
if len(lines) > 60:
    kept = "\n".join(lines[:10] + ["[... trimmed ...]"] + lines[-40:])
    print(json.dumps({"tool_output": kept, "context": f"pytest output trimmed from {len(lines)} lines"}))
```

**Audit every GitHub MCP call** (`post-mcp__gateway-tools-audit.async.sh`):

```bash
jq -c '{t: now, tool: .tool_name, input: .tool_input}' >> "$HOME/.agentihooks/logs/mcp-audit.jsonl"
```

**Remind before edits to migrations** (`pre-edit+write-migrations.sh`):

```bash
jq -r '.tool_input.file_path' | grep -q '/migrations/' && echo "Migrations are append-only; add a new file."
exit 0
```

## Enforcement matcher

An [enforcement](../pillars/context.md) can carry the same `matcher`. It is then
delivered only on matching tool calls and its `cadence` counts matching calls only:
the first matching call, then every Nth matching call. It is not injected at
SessionStart or on a user prompt.

```json
{"id": "kubectl-doctrine", "message": "Cluster writes go through GitOps.", "cadence": 1, "matcher": "bash.kubectl"}
```

```bash
agentihooks enforcement set "Cluster writes go through GitOps." 1 --matcher bash.kubectl
```

The MCP tool `enforcement_set` takes the same `matcher` argument.

## Extending to other hook events

The step table in `hooks/context/conditions.py` holds only `pre` and `post` today.
Adding an event takes one table row and a `run_step` call in that event's handler.
These tokens are reserved for that:

| Step | Event | Matched field |
|---|---|---|
| `fail` | PostToolUseFailure | tool name (the event must also be wired in `settings.base.json`) |
| `permission` | PermissionRequest | tool name |
| `start` | SessionStart | `source` (`startup`, `resume`, `clear`, `compact`) |
| `end` | SessionEnd | `reason` |
| `substart` / `substop` | SubagentStart / SubagentStop | `agent_type` |
| `compact` | PreCompact | `trigger` (`manual`, `auto`) |
| `notify` | Notification | `notification_type` |
| `prompt` | UserPromptSubmit | none: `prompt-<name>.<ext>` |
| `stop` | Stop | none: `stop-<name>.<ext>` |

## Configuration

| Variable | Default | Description |
|---|---|---|
| `CONDITIONS_ENABLED` | `true` | Run conditions on PreToolUse and PostToolUse |
| `CONDITIONS_TIMEOUT_SEC` | `10` | Per-condition timeout; the process group is killed on expiry |
| `CONDITIONS_MAX_PARALLEL` | `8` | Synchronous conditions run at once for one call |
| `CONDITIONS_TRUSTED_OWNERS` | `""` | Extra git remote owners whose repositories' directory layer may run (`*` = all). The linked bundle's owner is always trusted |
