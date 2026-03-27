# Claude Code Settings Cheat Sheet

> Comprehensive reference for Claude Code configuration. Use as a resource for building profiles, managed deployments, and automation.

---

## Configuration Scopes & Files

| Scope | File | Precedence | Shared? |
|---|---|---|---|
| Managed | `managed-settings.json` (system dirs), MDM, server | 1 (highest) | IT-deployed |
| CLI flags | `--permission-mode`, `--model`, etc. | 2 | No |
| Local | `.claude/settings.local.json` | 3 | No (gitignored) |
| Project | `.claude/settings.json` | 4 | Yes (committed) |
| User | `~/.claude/settings.json` | 5 (lowest) | No |

**System dirs for managed settings:**
- macOS: `/Library/Application Support/ClaudeCode/`
- Linux/WSL: `/etc/claude-code/`
- Windows: `C:\Program Files\ClaudeCode\`

**Other config files:**
- `~/.claude.json` — global prefs, OAuth, MCP servers (user/local scope), per-project state
- `.mcp.json` — project-scoped MCP servers
- `~/.claude/CLAUDE.md` — user-scope instructions
- `CLAUDE.md` or `.claude/CLAUDE.md` — project-scope instructions
- `.claude/agents/*.md` — subagent definitions
- `.claude/skills/` — skill definitions

---

## Permission Modes (`permissions.defaultMode`)

| Mode | Auto-approves | Best for | Shift+Tab cycle |
|---|---|---|---|
| `default` | File reads only | Sensitive work, getting started | Yes |
| `acceptEdits` | File reads + edits | Iterating on code you review | Yes |
| `plan` | File reads only (no edits/commands) | Exploring, planning refactors | Yes |
| `auto` | All actions (classifier checks) | Long tasks, reducing prompt fatigue | Yes (with `--enable-auto-mode`) |
| `dontAsk` | Only pre-approved tools | CI, locked-down envs | Never in cycle |
| `bypassPermissions` | Everything, no checks | Containers/VMs only | Only if started with it |

**Set mode:**
```json
{ "permissions": { "defaultMode": "acceptEdits" } }
```

**CLI:**
```bash
claude --permission-mode plan
claude --dangerously-skip-permissions  # = bypassPermissions
claude --enable-auto-mode              # adds auto to Shift+Tab cycle
claude --allow-dangerously-skip-permissions  # adds bypass to cycle without activating
```

---

## Permission Rules

**Evaluation order:** deny > ask > allow (first match wins)

```json
{
  "permissions": {
    "allow": [
      "Bash(npm run *)",
      "Bash(git commit *)",
      "Read",
      "Edit(/src/**/*.ts)",
      "Glob(*)",
      "Grep(*)"
    ],
    "ask": [
      "Bash(git push *)",
      "Bash(terraform apply *)"
    ],
    "deny": [
      "Bash(curl *)",
      "Bash(rm -rf *)",
      "Read(./.env)",
      "Read(./secrets/**)",
      "WebFetch",
      "Agent(Explore)"
    ]
  }
}
```

### Rule Syntax

| Pattern | Matches |
|---|---|
| `Bash` or `Bash(*)` | All Bash commands |
| `Bash(npm run *)` | Commands starting with `npm run ` |
| `Bash(* --version)` | Commands ending with ` --version` |
| `Bash(git * main)` | `git checkout main`, `git merge main`, etc. |
| `Read(./.env)` | Specific file relative to cwd |
| `Read(./secrets/**)` | Recursive glob (gitignore spec) |
| `Read(~/Documents/*.pdf)` | Home-relative path |
| `Read(//Users/alice/file)` | Absolute path (`//` prefix) |
| `Edit(/src/**/*.ts)` | Project-root-relative |
| `WebFetch(domain:example.com)` | Domain filter |
| `mcp__puppeteer` | All tools from MCP server |
| `mcp__puppeteer__puppeteer_navigate` | Specific MCP tool |
| `Agent(Explore)` | Specific subagent |

**Note:** `Bash(ls *)` (with space) enforces word boundary — matches `ls -la` but not `lsof`. `Bash(ls*)` matches both.

---

## All settings.json Keys

### Core Settings

| Key | Type | Description | Example |
|---|---|---|---|
| `model` | string | Default model | `"claude-sonnet-4-6"` |
| `availableModels` | string[] | Restrict `/model` choices | `["sonnet", "haiku"]` |
| `modelOverrides` | object | Map model IDs to provider IDs (Bedrock ARNs) | `{"claude-opus-4-6": "arn:..."}` |
| `effortLevel` | string | Persist effort level: `low`, `medium`, `high` | `"medium"` |
| `language` | string | Response language | `"japanese"` |
| `outputStyle` | string | Adjust system prompt style | `"Explanatory"` |
| `agent` | string | Run main thread as a named subagent | `"code-reviewer"` |
| `autoUpdatesChannel` | string | `"stable"` or `"latest"` | `"stable"` |

### Permission Settings

| Key | Type | Description |
|---|---|---|
| `permissions.allow` | string[] | Auto-approve rules |
| `permissions.ask` | string[] | Force-prompt rules |
| `permissions.deny` | string[] | Block rules |
| `permissions.defaultMode` | string | Default permission mode |
| `permissions.additionalDirectories` | string[] | Extra working dirs |
| `permissions.disableBypassPermissionsMode` | string | Set `"disable"` to block bypass mode |

### Environment

| Key | Type | Description | Example |
|---|---|---|---|
| `env` | object | Env vars for every session | `{"FOO": "bar"}` |
| `apiKeyHelper` | string | Script to generate auth | `/bin/gen_key.sh` |
| `forceLoginMethod` | string | `"claudeai"` or `"console"` | `"console"` |
| `forceLoginOrgUUID` | string | Auto-select org during login | UUID |
| `awsAuthRefresh` | string | AWS credential refresh script | `aws sso login --profile x` |
| `awsCredentialExport` | string | AWS credential export script | `/bin/gen_aws.sh` |

### Hooks

| Key | Type | Description |
|---|---|---|
| `hooks` | object | Hook event → handler arrays |
| `disableAllHooks` | bool | Kill switch for all hooks |
| `allowManagedHooksOnly` | bool | (Managed) Only managed/SDK hooks |
| `allowedHttpHookUrls` | string[] | URL allowlist for HTTP hooks |
| `httpHookAllowedEnvVars` | string[] | Env var allowlist for HTTP hook headers |

### Status Line & UI

| Key | Type | Description | Example |
|---|---|---|---|
| `statusLine` | object | Custom status bar | `{"type":"command","command":"..."}` |
| `fileSuggestion` | object | Custom `@` file picker | `{"type":"command","command":"..."}` |
| `respectGitignore` | bool | `@` picker respects .gitignore (default: true) | `false` |
| `spinnerVerbs` | object | Custom spinner text | `{"mode":"append","verbs":["Pondering"]}` |
| `spinnerTipsEnabled` | bool | Show tips in spinner (default: true) | `false` |
| `spinnerTipsOverride` | object | Custom spinner tips | `{"excludeDefault":true,"tips":["..."]}` |
| `prefersReducedMotion` | bool | Reduce UI animations | `true` |
| `showClearContextOnPlanAccept` | bool | Show "clear context" on plan accept | `true` |
| `companyAnnouncements` | string[] | Startup announcements | `["Welcome to Acme Corp"]` |

### Session & Memory

| Key | Type | Description |
|---|---|---|
| `cleanupPeriodDays` | int | Delete sessions older than N days (default: 30). `0` = disable persistence |
| `autoMemoryDirectory` | string | Custom auto-memory dir (not in project settings) |
| `plansDirectory` | string | Custom plan files dir (default: `~/.claude/plans`) |
| `alwaysThinkingEnabled` | bool | Enable extended thinking by default |
| `includeGitInstructions` | bool | Include git workflow in system prompt (default: true) |

### MCP Servers

| Key | Type | Description |
|---|---|---|
| `enableAllProjectMcpServers` | bool | Auto-approve all project `.mcp.json` servers |
| `enabledMcpjsonServers` | string[] | Specific MCP servers to approve |
| `disabledMcpjsonServers` | string[] | Specific MCP servers to reject |
| `allowManagedMcpServersOnly` | bool | (Managed) Only admin-approved MCP servers |
| `allowedMcpServers` | object[] | (Managed) MCP server allowlist |
| `deniedMcpServers` | object[] | (Managed) MCP server denylist |

### Worktree

| Key | Type | Description |
|---|---|---|
| `worktree.symlinkDirectories` | string[] | Dirs to symlink into worktrees |
| `worktree.sparsePaths` | string[] | Sparse checkout paths |

### Teams & Collaboration

| Key | Type | Description |
|---|---|---|
| `teammateMode` | string | `auto`, `in-process`, or `tmux` |
| `fastModePerSessionOptIn` | bool | Require `/fast` each session |
| `voiceEnabled` | bool | Push-to-talk voice dictation |
| `feedbackSurveyRate` | float | Survey probability (0–1) |
| `attribution` | object | Git commit/PR attribution text |

### Auto Mode Classifier

```json
{
  "autoMode": {
    "environment": [
      "Source control: github.example.com/acme-corp",
      "Trusted cloud buckets: s3://acme-builds",
      "Trusted internal domains: *.corp.example.com"
    ],
    "allow": [
      "Deploying to staging is allowed"
    ],
    "soft_deny": [
      "Never run database migrations outside the migrations CLI"
    ]
  }
}
```

**Warning:** Setting `allow` or `soft_deny` **replaces** the entire default list. Always start with `claude auto-mode defaults`.

**CLI commands:**
```bash
claude auto-mode defaults   # print built-in rules
claude auto-mode config     # effective config (your settings + defaults)
claude auto-mode critique   # AI feedback on your custom rules
```

---

## Sandbox Settings (`sandbox.*`)

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "autoAllowBashIfSandboxed": true,
    "allowUnsandboxedCommands": false,
    "excludedCommands": ["docker", "git"],
    "filesystem": {
      "allowWrite": ["/tmp/build", "~/.kube", "~/.terraform.d"],
      "denyWrite": ["/etc", "/usr/local/bin"],
      "denyRead": ["~/.aws/credentials", "~/.ssh"],
      "allowRead": ["."],
      "allowManagedReadPathsOnly": false
    },
    "network": {
      "allowedDomains": ["github.com", "*.npmjs.org", "registry.terraform.io"],
      "allowManagedDomainsOnly": false,
      "allowUnixSockets": ["~/.ssh/agent-socket"],
      "allowAllUnixSockets": false,
      "allowLocalBinding": false,
      "httpProxyPort": 8080,
      "socksProxyPort": 8081
    },
    "enableWeakerNestedSandbox": false,
    "enableWeakerNetworkIsolation": false
  }
}
```

**Path prefixes:**
| Prefix | Meaning |
|---|---|
| `/` | Absolute from filesystem root |
| `~/` | Relative to home |
| `./` or none | Project root (project settings) or `~/.claude` (user settings) |

**Platform support:** macOS (Seatbelt), Linux/WSL2 (bubblewrap + socat)

---

## Hook Events — Complete Reference

### Hook Types

| Type | Description |
|---|---|
| `command` | Run a shell command (stdin JSON, stdout/stderr/exit code) |
| `http` | POST event data to a URL |
| `prompt` | Single-turn LLM evaluation (returns `{ok, reason}`) |
| `agent` | Multi-turn subagent verification (reads files, runs tools, returns `{ok, reason}`) |

### All Events

| Event | When | Matcher Field | Can Block? |
|---|---|---|---|
| `SessionStart` | Session begins/resumes | source: `startup`, `resume`, `clear`, `compact` | No |
| `SessionEnd` | Session terminates | reason: `clear`, `resume`, `logout`, etc. | No |
| `UserPromptSubmit` | User submits prompt | (none) | No (inject context) |
| `PreToolUse` | Before tool executes | tool name | Yes (exit 2 or JSON deny) |
| `PostToolUse` | After tool succeeds | tool name | Yes (decision: block) |
| `PostToolUseFailure` | After tool fails | tool name | No |
| `PermissionRequest` | Permission dialog appears | tool name | Yes (allow/deny/ask) |
| `Stop` | Claude finishes responding | (none) | Yes (decision: block) |
| `StopFailure` | Turn ends from API error | error type | No |
| `SubagentStart` | Subagent spawned | agent type | No |
| `SubagentStop` | Subagent finishes | agent type | No |
| `TaskCreated` | Task created via TaskCreate | (none) | No |
| `TaskCompleted` | Task marked completed | (none) | No |
| `TeammateIdle` | Team teammate going idle | (none) | No |
| `Notification` | Notification sent | type: `permission_prompt`, `idle_prompt`, etc. | No |
| `InstructionsLoaded` | CLAUDE.md loaded | reason: `session_start`, `compact`, etc. | No |
| `ConfigChange` | Config file changed | source: `user_settings`, `project_settings`, etc. | Yes (decision: block) |
| `CwdChanged` | Working directory changed | (none) | No |
| `FileChanged` | Watched file changed on disk | filename (basename) | No |
| `WorktreeCreate` | Worktree being created | (none) | Custom behavior |
| `WorktreeRemove` | Worktree being removed | (none) | Custom behavior |
| `PreCompact` | Before compaction | trigger: `manual`, `auto` | No |
| `PostCompact` | After compaction | trigger: `manual`, `auto` | No |
| `Elicitation` | MCP requests user input | MCP server name | No |
| `ElicitationResult` | User responds to elicitation | MCP server name | No |

### Exit Codes

| Code | Meaning |
|---|---|
| `0` | Allow — proceed normally. Stdout added to context (SessionStart, UserPromptSubmit) |
| `2` | Block — cancel action. Stderr shown as feedback to Claude |
| Other | Proceed. Stderr logged but not shown (visible in verbose mode `Ctrl+O`) |

### JSON Output Patterns

**PreToolUse — deny with reason:**
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Use rg instead of grep"
  }
}
```

**PreToolUse — allow (skip prompt, deny rules still apply):**
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow"
  }
}
```

**PermissionRequest — auto-approve:**
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow"
    }
  }
}
```

**PermissionRequest — approve and set mode:**
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow",
      "updatedPermissions": [
        { "type": "setMode", "mode": "acceptEdits", "destination": "session" }
      ]
    }
  }
}
```

**PostToolUse — inject context:**
```json
{ "additionalContext": "Formatted output here..." }
```

**Stop — block (keep working):**
```json
{ "decision": "block", "reason": "Tests not passing yet" }
```

**Prompt/Agent hook response:**
```json
{ "ok": false, "reason": "Not all tasks are complete" }
```

### Hook Configuration Format

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "jq -r '.tool_input.file_path' | xargs prettier --write",
            "timeout": 30
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "prompt",
            "prompt": "Check if all tasks are complete.",
            "model": "haiku"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "agent",
            "prompt": "Verify this command is safe. $ARGUMENTS",
            "timeout": 60
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "http",
            "url": "http://localhost:8080/hooks/tool-use",
            "headers": { "Authorization": "Bearer $MY_TOKEN" },
            "allowedEnvVars": ["MY_TOKEN"]
          }
        ]
      }
    ]
  }
}
```

### Common Input Fields (stdin JSON)

```json
{
  "session_id": "abc123",
  "cwd": "/home/user/project",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "npm test" },
  "transcript_path": "/path/to/transcript.jsonl",
  "stop_hook_active": false
}
```

---

## CLI Flags — Quick Reference

### Session

| Flag | Short | Description |
|---|---|---|
| `--continue` | `-c` | Continue most recent conversation |
| `--resume` | `-r` | Resume by session ID or name |
| `--name` | `-n` | Name the session |
| `--fork-session` | | Create new ID when resuming |
| `--from-pr` | | Resume sessions linked to a PR |
| `--session-id` | | Use specific UUID |
| `--worktree` | `-w` | Start in isolated git worktree |

### Execution Mode

| Flag | Short | Description |
|---|---|---|
| `--print` | `-p` | Non-interactive (SDK) mode |
| `--permission-mode` | | Set permission mode |
| `--dangerously-skip-permissions` | | bypassPermissions mode |
| `--enable-auto-mode` | | Add auto to Shift+Tab cycle |
| `--allow-dangerously-skip-permissions` | | Add bypass to cycle without activating |
| `--bare` | | Minimal mode (no hooks/MCP/plugins) |
| `--init` | | Run init hooks then interactive |
| `--init-only` | | Run init hooks then exit |

### Model & Effort

| Flag | Description |
|---|---|
| `--model` | Set model (`sonnet`, `opus`, or full ID) |
| `--effort` | `low`, `medium`, `high`, `max` (Opus only) |
| `--fallback-model` | Auto-fallback when overloaded (print mode) |

### Tools & Permissions

| Flag | Description |
|---|---|
| `--allowedTools` | Auto-approve specific tools (pattern syntax) |
| `--disallowedTools` | Remove tools from context entirely |
| `--tools` | Restrict available built-in tools |

### Prompt

| Flag | Description |
|---|---|
| `--system-prompt` | Replace entire system prompt |
| `--system-prompt-file` | Replace from file |
| `--append-system-prompt` | Append to default prompt |
| `--append-system-prompt-file` | Append from file |

### Budget & Limits

| Flag | Description |
|---|---|
| `--max-budget-usd` | Dollar cap for API calls (print mode) |
| `--max-turns` | Turn limit (print mode) |

### MCP & Plugins

| Flag | Description |
|---|---|
| `--mcp-config` | Load MCP servers from JSON |
| `--strict-mcp-config` | Only use `--mcp-config` servers |
| `--plugin-dir` | Load plugins from directory |
| `--chrome` / `--no-chrome` | Enable/disable Chrome integration |
| `--channels` | MCP channel notifications |

### Output

| Flag | Description |
|---|---|
| `--output-format` | `text`, `json`, `stream-json` (print mode) |
| `--input-format` | `text`, `stream-json` (print mode) |
| `--json-schema` | Validated structured output (print mode) |
| `--include-partial-messages` | Include streaming events |
| `--verbose` | Full turn-by-turn logging |
| `--debug` | Debug mode with category filtering |

### Other

| Flag | Description |
|---|---|
| `--add-dir` | Add working directories |
| `--agent` | Run as specific subagent |
| `--agents` | Define subagents via JSON |
| `--setting-sources` | Limit which settings scopes load |
| `--settings` | Load additional settings file |
| `--remote` | Create web session on claude.ai |
| `--remote-control` | Start Remote Control server |
| `--teleport` | Resume web session locally |
| `--teammate-mode` | `auto`, `in-process`, `tmux` |
| `--no-session-persistence` | Don't save session to disk |

---

## Managed-Only Settings (Enterprise)

These only work in managed settings (cannot be overridden):

| Key | Description |
|---|---|
| `allowManagedPermissionRulesOnly` | Only managed permission rules apply |
| `allowManagedHooksOnly` | Only managed/SDK hooks load |
| `allowManagedMcpServersOnly` | Only admin-approved MCP servers |
| `channelsEnabled` | Allow channel message delivery |
| `allowedChannelPlugins` | Channel plugin allowlist |
| `blockedMarketplaces` | Blocked plugin sources |
| `strictKnownMarketplaces` | Restrict marketplace additions |
| `sandbox.network.allowManagedDomainsOnly` | Only managed network domains |
| `sandbox.filesystem.allowManagedReadPathsOnly` | Only managed read paths |
| `disableAutoMode` | Set `"disable"` to prevent auto mode |
| `pluginTrustMessage` | Custom plugin install warning |

---

## Global Config (`~/.claude.json` only)

| Key | Description | Default |
|---|---|---|
| `autoConnectIde` | Auto-connect to IDE from external terminal | `false` |
| `autoInstallIdeExtension` | Auto-install VS Code extension | `true` |
| `editorMode` | `"normal"` or `"vim"` | `"normal"` |
| `showTurnDuration` | Show "Cooked for 1m 6s" messages | `true` |
| `terminalProgressBarEnabled` | Terminal progress bar (ConEmu, Ghostty, iTerm2) | `true` |

---

## Common Profile Recipes

### Read-Only Analyst
```json
{
  "permissions": {
    "defaultMode": "plan",
    "allow": ["Read", "Glob(*)", "Grep(*)"],
    "deny": ["Bash", "Edit", "Write"]
  }
}
```

### CI/CD Worker (locked down)
```json
{
  "permissions": {
    "defaultMode": "dontAsk",
    "allow": [
      "Bash(npm test)",
      "Bash(npm run build)",
      "Bash(npm run lint)",
      "Read",
      "Glob(*)",
      "Grep(*)"
    ],
    "deny": [
      "Bash(npm publish *)",
      "Bash(git push *)",
      "WebFetch"
    ]
  }
}
```

### DevOps Agent (sandbox + IaC)
```json
{
  "permissions": {
    "defaultMode": "acceptEdits",
    "allow": [
      "Bash(terraform plan *)",
      "Bash(terraform init *)",
      "Bash(kubectl get *)",
      "Bash(kubectl describe *)",
      "Bash(aws s3 ls *)",
      "Bash(aws sts get-caller-identity)",
      "Read",
      "Edit",
      "Glob(*)",
      "Grep(*)"
    ],
    "ask": [
      "Bash(terraform apply *)",
      "Bash(terraform destroy *)",
      "Bash(kubectl apply *)",
      "Bash(kubectl delete *)",
      "Bash(aws s3 rm *)",
      "Bash(helm install *)",
      "Bash(helm upgrade *)",
      "Bash(argocd app sync *)"
    ],
    "deny": [
      "Bash(kubectl exec *)",
      "Bash(terraform apply -auto-approve *)",
      "Bash(rm -rf *)"
    ]
  },
  "sandbox": {
    "enabled": true,
    "filesystem": {
      "allowWrite": ["~/.kube", "~/.terraform.d", "/tmp/terraform-*"],
      "denyRead": ["~/.aws/credentials"]
    },
    "network": {
      "allowedDomains": [
        "registry.terraform.io",
        "*.hashicorp.com",
        "*.amazonaws.com",
        "*.kubernetes.io"
      ]
    }
  }
}
```

### Coding Agent (agenticore pattern)
```json
{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "allow": [
      "Bash(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)",
      "Glob(*)",
      "Grep(*)",
      "Task(*)"
    ],
    "deny": [
      "Bash(git push origin main)",
      "Bash(git push origin dev)",
      "Bash(gh pr merge *)"
    ]
  }
}
```

---

## Environment Variables (via `env` key)

| Variable | Description |
|---|---|
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Cap response token size |
| `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` | Enable agent teams (`"1"`) |
| `ENABLE_TOOL_SEARCH` | Enable tool search (`"true"`) |
| `CLAUDE_CODE_USE_POWERSHELL_TOOL` | Enable PowerShell on Windows (`"1"`) |
| `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS` | Remove git from system prompt |
| `CLAUDE_CODE_ENABLE_TELEMETRY` | Enable OTEL telemetry (`"1"`) |
| `OTEL_METRICS_EXPORTER` | OTEL exporter type |
| `ANTHROPIC_MODEL` | Override model |
| `CLAUDE_CODE_SIMPLE` | Bare mode (set by `--bare`) |

---

## Sources

- [Settings Reference](https://code.claude.com/docs/en/settings)
- [Permissions](https://code.claude.com/docs/en/permissions)
- [Permission Modes](https://code.claude.com/docs/en/permission-modes)
- [Hooks Guide](https://code.claude.com/docs/en/hooks-guide)
- [Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [CLI Reference](https://code.claude.com/docs/en/cli-reference)
- [JSON Schema](https://json.schemastore.org/claude-code-settings.json)
