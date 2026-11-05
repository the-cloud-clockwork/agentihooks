---
title: Headless profile runs
parent: Reference
nav_order: 74
---

# Headless profile runs
{: .no_toc }

`agentihooks headless run` runs one full rendered profile unattended on Claude
Code or codex-cli and prints the result as one JSON object. The profile keeps
its MCP servers, skills, rules, settings and guardrail hooks; only swarm and
fleet coordination output is dropped. A run has a timeout, can be held to a
JSON schema, and leaves its transcript in a run folder.

Use it for scripted or CI work that needs a complete profile. The judge and
scouts use the clamped runs in [Headless clamps](headless-clamp.md), which
strip tools, MCP and hooks; this command does not clamp anything.

1. TOC
{:toc}

## Usage

```bash
agentihooks headless run PROFILE --input TEXT|FILE \
  [--schema FILE] [--harness auto|claude|codex] [--out DIR] \
  [--tier high|low] [--effort EFFORT] [--timeout SECONDS]
```

| Flag | Default | Meaning |
|---|---|---|
| `PROFILE` | required | Profile to run, the same name `agentihooks select-profile` takes |
| `--input` | required | Prompt text, or the path of a file holding it |
| `--schema FILE` | none | JSON schema file the output must match |
| `--harness` | `auto` | `claude` or `codex`; `auto` reads `AGENTIHOOKS_TARGET` and falls back to `claude` |
| `--out DIR` | a new folder under the AgentiHooks home | Run folder for the transcript and harness files; created when missing |
| `--tier` | `high` | Model tier, mapped to a model per harness (see [Model tiers](#model-tiers)) |
| `--effort` | `medium` | Effort passed to `select-profile --effort` (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`) |
| `--timeout` | `1800` | Seconds before the run is killed |

`--input` is read as a file when the value names a readable file; anything
else, a directory path or a prompt too long to be a file name included, is
used as the prompt text itself.

## Examples

Plain run, default run folder:

```bash
agentihooks headless run engineer --input 'Summarize the open follow ups in this repo.'
```

Prompt from a file, structured output on Codex with the low tier, a ten minute
limit and a chosen run folder:

```bash
cat > schema.json <<'EOF'
{"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
EOF
agentihooks headless run engineer --input prompt.md --schema schema.json \
  --harness codex --tier low --timeout 600 --out runs/codex
```

Read the report in a script:

```bash
report=$(agentihooks headless run engineer --input prompt.md --schema schema.json) \
  && jq -r '.output.answer' <<<"$report" \
  || jq -r '.status, .transcript' <<<"$report"
```

Live runs recorded on the delivering pull request, one per harness:

```
$ agentihooks headless run engineer --input 'Reply with JSON whose answer is the word pong.' --schema schema.json --harness claude --tier low --timeout 600 --out runs/claude
{"status": "ok", "output": {"answer": "pong"}, "session": "f55952f3-bf25-43e7-b012-1a74d8ba8168", "exit_code": 0, "transcript": "runs/claude/transcript.jsonl", "run_dir": "runs/claude"}
$ agentihooks headless run engineer --input 'Reply with JSON whose answer is the word pong.' --schema schema.json --harness codex --tier low --timeout 600 --out runs/codex
{"status": "ok", "output": {"answer": "pong"}, "session": "01a17c84-c843-7670-8cfd-6e1fe4908627", "exit_code": 0, "transcript": "runs/codex/transcript.jsonl", "run_dir": "runs/codex"}
```

## Output

stdout carries exactly one JSON line:

| Field | Meaning |
|---|---|
| `status` | `ok`, `failed`, `timeout` or `invalid` |
| `output` | The harness's final answer: the parsed object when a schema was given and matched, otherwise the reply text; `null` on timeout |
| `session` | Claude `session_id` or Codex `thread_id` read from the transcript; `null` when none was seen |
| `exit_code` | The harness exit code; `null` on timeout |
| `transcript` | Path of `transcript.jsonl` in the run folder |
| `run_dir` | The run folder |

| Status | When | Command exit |
|---|---|---|
| `ok` | The harness exited 0 and, with a schema, its output matched it | 0 |
| `failed` | The harness exited non zero; `output` holds whatever reply was read | 1 |
| `timeout` | The run passed `--timeout`; its whole process group was killed | 1 |
| `invalid` | The harness exited 0 but the output was not JSON or did not match the schema; `output` holds the raw reply | 1 |

An invalid flag exits 2 with argparse usage on stderr. A missing or non JSON
schema file exits 1 before any harness starts, with
`agentihooks headless: no schema file <path>` or
`agentihooks headless: schema <path> is not JSON: <error>` on stderr and
nothing on stdout.

## How a run works

1. The harness is chosen: `--harness`, or for `auto` the `AGENTIHOOKS_TARGET`
   value, else `claude`.
2. The run folder is created. Without `--out` it is
   `<AGENTIHOOKS_HOME>/headless/<UTC stamp>-<8 hex>`, for example
   `~/.agentihooks/headless/20261105T140312Z-3f9a1c2e`; `AGENTIHOOKS_HOME`
   unset or empty means `~/.agentihooks`. Two runs never share a default folder.
3. The tier is turned into a model and the command starts
   `agentihooks select-profile PROFILE --agent <harness> --model <model> --effort <effort> -- <harness flags>`,
   so the profile is rendered and mounted exactly as for any other launch (see
   [`agentihooks select-profile`](cli-commands.md#agentihooks-select-profile)).
4. The harness runs with stdin closed, its stdout written to
   `transcript.jsonl` and its stderr to `stderr.txt` in the run folder. The
   environment carries `AGENTIHOOKS_WORKER_RUN=1` and
   `AGENTIHOOKS_WORKER_RUN_ID=<run folder name>`.
5. The final answer and session id are read from the transcript and, with a
   schema, validated.

Harness flags passed after `--`:

| Harness | Flags |
|---|---|
| Claude Code | `-p <input> --output-format stream-json --verbose --no-session-persistence --permission-mode bypassPermissions`, plus `--json-schema <schema>` with a schema |
| codex-cli | `exec --json --skip-git-repo-check -o <run folder>/last-message.txt`, plus `--output-schema <run folder>/output-schema.json` with a schema, then `-- <input>` |

Claude's answer is the last `result` event in the stream: its
`structured_output` when present, else its `result` text. Codex's answer is
`last-message.txt`; its session is the first `thread.started` event.
`--skip-git-repo-check` lets a Codex run start from any folder, a git checkout
or not.

### Worker mode

`AGENTIHOOKS_WORKER_RUN=1` keeps every guardrail hook and turns off swarm and
fleet coordination inside the run: broadcast delivery, inbox delivery, swarm
refocus, ledger hooks, claim and wait nudges and plan accept capture. A
headless run in a swarm agent's shell therefore does not act on, or answer,
that agent's swarm traffic.

### Run folder

| File | Contents |
|---|---|
| `transcript.jsonl` | Harness stdout: Claude `stream-json` events or Codex `--json` events |
| `stderr.txt` | Harness stderr, including `select-profile` refusals |
| `last-message.txt` | Codex only: the final message |
| `output-schema.json` | Codex only, with `--schema`: the schema handed to `--output-schema` |

Default run folders accumulate under `<AGENTIHOOKS_HOME>/headless/`; remove
old ones by hand.

## Model tiers

| Harness | `high` | `low` | Override |
|---|---|---|---|
| Claude | `opus` | `sonnet` | `AGENTIHOOKS_TIER_HIGH_CLAUDE`, `AGENTIHOOKS_TIER_LOW_CLAUDE` |
| Codex | `gpt-6.1-sol` | `gpt-6-luna` | `AGENTIHOOKS_TIER_HIGH_CODEX`, `AGENTIHOOKS_TIER_LOW_CODEX` |

An override that is set and non empty replaces the default model for that
harness and tier, for example
`AGENTIHOOKS_TIER_LOW_CLAUDE=haiku agentihooks headless run engineer --input x --tier low`.

## Limitations

- Harnesses are `claude` and `codex`. With `--harness auto` and
  `AGENTIHOOKS_TARGET` set to anything else, such as `copilot`, the command
  stops with `ValueError: no headless adapter for copilot`; pass `--harness`
  explicitly.
- No retry: a reply that misses the schema is `invalid` at once. The clamped
  runner in [Headless clamps](headless-clamp.md) retries; this command does not.
- Claude runs with `--permission-mode bypassPermissions` and Codex with its
  configured sandbox; the profile's hooks are the guard, not the harness
  permission prompt.
- The run is not persisted as a resumable Claude session
  (`--no-session-persistence`); `session` identifies it in the transcript only.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `status` `failed`, empty `output` | Read `stderr.txt` in the run folder. A `select-profile` refusal (unknown profile, bad effort, home binding mismatch) exits 2 there; see [Profile launch home](profile-launch-home.md) |
| `status` `invalid` | `output` holds the raw reply. Check the schema's `required` keys against the prompt; say in the prompt that the reply is JSON |
| `status` `timeout` | Raise `--timeout`, or read `transcript.jsonl` for where the run stalled |
| Codex `Not inside a trusted directory` | An older build without `--skip-git-repo-check`; upgrade AgentiHooks |
| `no headless adapter for <name>` | `AGENTIHOOKS_TARGET` names an unsupported harness; pass `--harness claude` or `--harness codex` |
| Wrong model in the transcript | A `AGENTIHOOKS_TIER_<TIER>_<HARNESS>` override is set in the environment or a loaded env file |
