---
title: Agents, workflows and plan ingestion
parent: Reference
nav_order: 76
---

# Agents, workflows and plan ingestion
{: .no_toc }

The bundle is the agent catalogue. Every profile that ships in the built-in
profiles, the package roles, the bundle, a shared or user bundle or a linked
profile is an agent, and
its `profile.yml` is its card. A workflow runs profiles as stages; a single
agent run is a one-stage workflow. A plan file becomes ledger phases and slices
through one normalizer.

1. TOC
{:toc}

## Agent cards

A card is two fields of `profile.yml`:

```yaml
description: Answers one question with an evidence set gathered from the web and local files
capabilities:
  - research
  - web
```

- `description` is a string, empty when absent.
- `capabilities` is a list of nonempty strings, each kept once, empty when
  absent.
- `required_capabilities` keeps its hive meaning: the capabilities a hive must
  advertise before it runs the profile. It is not part of the card.

A malformed field refuses the card with the profile name and the field.

Swarm-v2 section 9.2 records the decision: the bundle catalogue replaces the
agentibridge agent registry. There is no registration step, service or
database. A profile is discoverable once it ships in one of the layers above.

### Commands

```bash
agentihooks agents list                        # every card as JSON
agentihooks agents list --capability research  # only cards declaring it
agentihooks agents show scouter                # one card as JSON
```

Profiles are resolved in the existing profile lookup order, so a bundle profile
shadows a package role of the same name. An unknown name exits nonzero with
`Unknown agent profile: <name>`.

### MCP tools

The `agents` MCP category serves the same cards and runs one agent.

| Tool | Arguments | Returns |
|---|---|---|
| `agent_list` | `capability` (optional) | every card, or only those declaring the capability |
| `agent_show` | `name` | one card |
| `agent_run` | `profile`, `input`, `wait` (default true) | the finished run state, or with `wait=false` the running state at once |

`agent_run` saves a one-stage workflow named `agent-<profile>` whose single
stage `run` runs the profile on `input` as the case, with one iteration. Any
outcome ends the run. With `wait=false` a detached `agentihooks workflow resume`
carries the run on; read it with `agentihooks workflow status <run id>`.

`agent_run` is refused inside a worker run (`agent_run is refused inside a
worker run, so runs cannot nest`). Every rendered home sets
`MCP_CATEGORIES=all` for the stdio transport, so specialists see the tool and the refusal is what keeps
runs flat.

## Workflows

A workflow is a YAML file with a `workflow` header and a `stages` map.

| Header key | Meaning | Default |
|---|---|---|
| `name` | workflow name | required |
| `intent` | what the run must achieve | required |
| `head` | first stage | required |
| `exit` | `<stage>.pass` that ends the run | required |
| `max_iterations` | loops through the stages before the run is exhausted | 3 |
| `on_exhausted` | `block`, `escalate` or `keep-best` | `block` |

| Stage key | Meaning | Default |
|---|---|---|
| `profile` | the agent card that runs the stage | required |
| `tier` | `high` or `low` model tier | `high` |
| `effort` | reasoning effort | `medium` |
| `count` | parallel runs of the stage | 1 |
| `input` | `case` and earlier stage ids fed to the stage | none |
| `filters` | filter profiles applied to the output, such as `judge-blind` | none |
| `iterations` | attempts inside the stage | 1 |
| `exit` | the condition that ends the stage | none |
| `next` | the stage for each outcome `pass`, `retry`, `enhance`; `exit` ends the run | none |

The package ships `simple-workflow`: scout, an evidence verdict, plan, build
and a build verdict.

### Layers

Workflows load from the package, the bundle, the shared bundle, the user
bundle, each chained profile and then the project's `.agentihooks/workflows`.
A later layer wins by name. `list`, `show` and `validate` read the project
layer; `run` skips it.

### Commands

```bash
agentihooks workflow list                        # every workflow with its layer and file
agentihooks workflow show simple-workflow        # one workflow with defaults filled in
agentihooks workflow validate path/to/flow.yaml  # check a file or a loaded name
agentihooks workflow run simple-workflow --case case.md [--run-id ID] [--harness auto|claude|codex]
agentihooks workflow status <run id>             # saved state of a run
agentihooks workflow resume <run id>             # continue a saved run
```

## Plan ingestion

The plan normalizer reads a plan file and returns its title, Markdown, ledger
content (title and phases) and the slice names of each phase. It accepts three
shapes.

| Shape | Recognised by | Result |
|---|---|---|
| JSON | the file parses as JSON: a `phases` list, or a bare list | one phase per entry, rendered to Markdown |
| Marked Markdown | a phase marker comment directly under a heading | one phase per marked heading, with its slices |
| Raw Markdown | no phase markers | one phase titled by the first level one heading or the file name |

A marked phase:

```markdown
## Agent cards from profiles
<!-- phase: id=cards depends=runner planning=auto -->

The phase description is the prose up to the first slice or heading.

<!-- slice: cat1 -->
### cat1 — Profiles declare description and capabilities
```

- Marker keys: `id`, `depends` (comma separated phase ids or positions),
  `planning` (`manual` or `auto`) and `not_duplicate` (a plain words reason).
  Any other key is refused.
- Every phase heading sits at one heading level.
- A file with slice markers needs at least one phase marker, and each slice
  name appears once in the file.
- A marked phase holding headings but no slice markers is refused: phase
  '<title>' has no slice markers, so it holds prose only.
- The file must be UTF-8, nonempty and free of YAML front matter.
- Dependencies must name phases in the plan or already on the ledger.

The marker field checks list every field error at once.

### Ingest command

```bash
agentihooks plan ingest plan.md --ledger <slug> [--planning auto|manual] [--dry-run] [--repo OWNER/NAME]
```

`path` is the plan file, or `-` for standard input. `--ledger` names the
target ledger. `--planning` sets planning for every phase. `--dry-run` prints
what would be written and writes nothing. `--repo` names the repository for
the plan issue, by default the current one.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A profile is missing from `agents list` | it ships in no installed layer | add it to the bundle or link it, then `agentihooks init` |
| `agent_run` answers the nesting refusal | the caller is itself a worker run | run the agent from the parent session or add a stage to the workflow |
| `workflow show` loads an unexpected file | a later layer defines the same name | `agentihooks workflow list` names the layer and file |
| A plan is refused with `slice markers need phase markers above them` | slice markers in a file with no phase marker | add a phase marker under each phase heading |
