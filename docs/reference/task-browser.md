---
title: Task browser
parent: Reference
nav_order: 29
---

# Task browser

The task browser is the headless browser a swarm session reaches through the
`playwright-cmd` MCP server. It runs as `python -I -m scripts.profiles.browser`,
a proxy in front of an upstream `playwright-mcp` node process. Two changes keep
it from costing memory in sessions that never browse:

- **It starts on the first tool call.** Opening a session, and the harness
  listing its tools, no longer launch Playwright or `playwright-mcp`
  ([#4313](https://github.com/the-cloud-clockwork/agentihooks/pull/4313)).
- **Lanes that never browse do not mount it.** Engineer, CI and planner homes
  render without the proxy; master, frontend, QA and documentation homes keep
  it ([#4540](https://github.com/the-cloud-clockwork/agentihooks/pull/4540)).

## Which homes mount it

The profile render decides per chain, the same way for Claude, Codex and Copilot
homes.

| Chain contains | `playwright-cmd` mounted | Other `playwright*` MCP servers | Playwright Claude plugin |
|---|---|---|---|
| `master`, `qa` or `documentation` | Yes | Removed, except `playwright-ext-*` | Disabled |
| `engineer`, `cicd` or `planner` with the `frontend` overlay | Yes | Removed, except `playwright-ext-*` | Disabled |
| `engineer`, `cicd` or `planner` alone | No | Removed, except `playwright-ext-*` | Disabled |
| No swarm role | No change | Kept as the bundle layers define them | Kept |

A `playwright-ext-<account>` server, the logged in extension browser an overlay
declares, is never removed. The proxy's presence is part of the render stamp,
so a home rendered before the lane change renders again on its next launch.

The mounted entry carries no settings of its own:

```json
{
  "mcpServers": {
    "playwright-cmd": {
      "command": "/home/<user>/dev/tcc-ecosystem/.venv/bin/python",
      "args": ["-I", "-m", "scripts.profiles.browser"]
    }
  }
}
```

The command is the detected workspace interpreter, or the interpreter running
the render when none is found. To give an engineer, CI or planner seat the
browser, run its task with the frontend profile
(`agentihooks ledger --slug <slug> task set <id> profile=frontend`).

## Start on first call

| Moment | What runs |
|---|---|
| Session opens | The Python proxy only. Playwright and `playwright-mcp` stay unstarted. |
| Harness lists tools (`tools/list`) | Answered from the tool list cache when its key matches. With no matching cache, `playwright-mcp` starts once, its list is written to the cache and the upstream stays running. |
| First tool call | Playwright resolves the Chromium executable, then `playwright-mcp` starts headless and isolated with a 1920x1080 viewport. |
| Later tool calls | Reuse the same `playwright-mcp` session. |

Screenshots and other files a tool names land in the task folder: the swarm
task workspace (`AGENTIHOOKS_SWARM`, `AGENTIHOOKS_SWARM_TASK`, or the master
folder when no task is set), else `~/.agentihooks/browser`. A `filename`
argument keeps only its base name, so a call cannot write outside that folder.

## Tool list cache

The cache is one file shared by every session on the machine:
`~/.agentihooks/browser/tools.json`.

```json
{
  "key": "/home/<user>/.nvm/versions/node/v26.7.0/lib/node_modules/@playwright/mcp/cli.js 1791681289445995681",
  "tools": [{"name": "browser_close", "...": "..."}]
}
```

The key is the resolved path of the `playwright-mcp` on `PATH` and its
modification time in nanoseconds. Upgrading or reinstalling `playwright-mcp`
changes the key, and the next `tools/list` refreshes the file. An unreadable,
malformed or mismatched file counts as a miss. With no `playwright-mcp` on
`PATH`, nothing is cached and every `tools/list` starts the upstream, as before
the change.

Inspect the cache and the live processes:

```bash
jq -r '.key, (.tools | length)' ~/.agentihooks/browser/tools.json
ps -eo pid,rss,args | grep -E 'scripts.profiles.browser|playwright-mcp' | grep -v grep
```

An idle session shows one `scripts.profiles.browser` line of about 55 MB
resident and no `playwright-mcp` child. A child appears only after that session
calls a browser tool.

## Measured effect

Before #4313, at 21 live agent sessions, the browser helper held 1843 MB in all
(node child 1070 MB, Python proxy 773 MB) with swap at 4011 of 4096 MB. Over 24
hours of transcripts, browser tools were called by 0 of 52 CI sessions, 2 of
177 engineer sessions and 6 of 284 master sessions; the engineer calls came from
frontend homes.

After #4313 applied, the browser node helper was gone from every session
started since then: browser memory per session fell from 37 to 120 MB to 0.
Median resident memory per Claude session did not fall (engineer 392.5 to
402 MB, CI 447 to 458 MB), and swap stayed full (4096 to 4095 MB at 23 and 29
live sessions), because the Python proxy still ran at about 56 MB in every
lane. #4540 removed that proxy from engineer, CI and planner homes.

## Limitations

- The cache key covers the `playwright-mcp` binary only. A change to the launch
  flags that alters the tool set needs a binary update or the cache file
  removed.
- A failed upstream start ends the proxy, and the session loses its browser
  tools until it restarts.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A seat has no `playwright-cmd` tools | Its chain is engineer, CI or planner without the frontend overlay | Set the task's profile to `frontend` |
| Browser tools are missing or stale after a `playwright-mcp` upgrade on another path | The cache key matches the binary still first on `PATH` | Fix `PATH`, or delete `~/.agentihooks/browser/tools.json` |
| Every session starts `playwright-mcp` at once | No `playwright-mcp` on `PATH`, so nothing is cached | Install `playwright-mcp` through `agentihooks deps ensure` |
| The first browser call is slow | Playwright and the upstream start on that call | Expected once per session |
| Browser tools stop answering in a session | The upstream failed to start and the proxy ended | Restart the session; read the harness MCP log for the start error |
