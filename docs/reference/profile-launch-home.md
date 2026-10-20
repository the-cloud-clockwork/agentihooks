---
title: Profile launch home
parent: Reference
nav_order: 18
---

# Profile launch home

`agentihooks init-agent --profile NAME` renders the profile once, records the
rendered home in the launch report, and starts the session on exactly that
home. The launcher it writes runs `agentihooks select-profile NAME
--home=<home>`, which checks the recorded home and mounts it without rendering
again. A change to the editable package between prepare and launch, such as a
pull of `dev` into the primary checkout, or a launcher running from another
checkout than the one that prepared the launch, no longer moves the session to
a different home, so the binding canary validates the home the launch recorded.

## Launch sequence

1. **Prepare.** `init-agent` renders the profile for the chosen harness, with
   its `--overlay` and `--bundle-revision` choices, into
   `~/.agentihooks/profiles/.homes/<profile>/<key>/<harness>` and sets
   `CLAUDE_CONFIG_DIR` (Claude) or `CODEX_HOME` (Codex) to it. It always
   renders here: an `AGENTIHOOKS_RECORDED_PROFILE_HOME` inherited from its
   caller is dropped first.
2. **Record.** It writes a pending launch report
   (`AGENTIHOOKS_PROFILE_REPORT`) holding the profile, the harness and the
   rendered binding of that home: home path, persona digest, source manifest
   digest and canary.
3. **Launch.** The launcher script runs, after the terminal or herdr pane
   opens:

   ```bash
   agentihooks select-profile NAME \
     [--overlay=<overlay> ...] [--bundle-revision=<commit>] \
     --home=<recorded home> --agent claude|codex -- <harness arguments>
   ```

   Fresh launches, `--resume` and `--handoff` all take this command.
4. **Mount.** `select-profile` sees `--home` and skips the render. It inspects
   the home, then exports it as `CLAUDE_CONFIG_DIR` or `CODEX_HOME` and starts
   `agentihooks claude` or `agentihooks codex`.
5. **Validate.** The session's opening prompt asks it to run the binding canary
   command from its mounted instructions. That writes `validated` to the
   launch report, and `init-agent` prints `profile_validation=validated` and
   `profile_binding=<json>`, whose `home` is the recorded home.

## What the home check requires

`select-profile --home` passes the home to the same inspection the launch
report used. The home is refused unless all of these hold:

| Check | Refusal |
|---|---|
| The directory exists | `missing profile home: <home>` |
| `.profile-binding.json` in the home is readable JSON | `missing or invalid rendered profile evidence: <error>` |
| Its `profile`, `harness` and resolved `home` match the request | `profile mismatch in rendered binding` |
| The persona file (`CLAUDE.md` or `AGENTS.md`) still has the rendered digest | `mounted persona changed since render` |
| `<harness>.sources.json` beside the home still has the rendered digest | `rendered source manifest changed` |

A swarm Codex launch (`AGENTIHOOKS_SWARM` set) is also refused when the home
resolves to the operator's own Codex home:
`swarm codex profile resolves to the operator Codex home`.

On a refusal `select-profile` prints `agentihooks select-profile: <reason>` to
stderr, exits 2, and, when `AGENTIHOOKS_PROFILE_REPORT` is set, marks the
pending report `failed` with the reason `profile selection failed: <reason>`.
`init-agent` then reports the launch failed rather than waiting for the route
timeout.

## Using `select-profile` directly

Without `--home`, `select-profile` renders the profile itself and mounts the
current home for that profile and overlays, as before. Pass `--home` to pin a
run to a home rendered earlier. `--dry-run` prints the environment and the
harness command without starting it:

```text
$ agentihooks select-profile documentation \
    --home=$HOME/.agentihooks/profiles/.homes/documentation/<key>/claude \
    --agent claude --dry-run -- --model opus
AGENTIHOOKS_PROFILE=documentation
AGENTIHOOKS_BASE_CHANNELS=brain,amygdala
AGENTIHOOKS_OVERLAYS=
AGENTIHOOKS_BUNDLE_REVISION=
CLAUDE_CONFIG_DIR=/home/<user>/.agentihooks/profiles/.homes/documentation/<key>/claude
argv=agentihooks claude --model opus --effort medium
```

The same home given with another profile name is refused:

```text
$ agentihooks select-profile engineer --home=<documentation home> --agent claude --dry-run
agentihooks select-profile: profile mismatch in rendered binding
$ echo $?
2
```

`--home` maps to `AGENTIHOOKS_RECORDED_PROFILE_HOME` inside the selector. The
variable is internal: neither `init-agent` nor `select-profile` reads it from
the caller's environment, so only an explicit `--home` skips the render.

## Limits

- The Codex app-server transport (`--codex-transport app_server`) launches
  `scripts.codex_session serve`, which still renders inside its server
  process; it does not take the recorded home.
- A home whose persona or source manifest is rewritten after the launch was
  recorded is refused, not re-rendered. Launch again to render a fresh home.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `profile mismatch in rendered binding` | The recorded home belongs to another profile or harness, or was moved. Launch again with the intended `--profile` and `--agent`. |
| `mounted persona changed since render` or `rendered source manifest changed` | Files in the home changed after `init-agent` recorded it. Launch again to render and record a fresh home. |
| `missing profile home` | The home was removed between prepare and launch. Launch again. |
| `mounted profile canary did not validate before timeout` | The session started but never ran the canary command. Check the session pane; the home it mounted is in its `CLAUDE_CONFIG_DIR` or `CODEX_HOME`. |

See [`agentihooks init-agent`](cli-commands.md#agentihooks-init-agent) and
[`agentihooks select-profile`](cli-commands.md#agentihooks-select-profile) for
the full flag lists.
