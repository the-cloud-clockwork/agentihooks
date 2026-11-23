---
title: Review lane
parent: Reference
nav_order: 97
permalink: /docs/reference/swarm-review-lane/
---

# Review lane

The `review` lane gives review tasks their own kind, reviewer names, numbered
seats and capacity ceiling alongside `eng`, `ci` and `plan`.
[Pull request #12916](https://github.com/the-cloud-clockwork/agentihooks/pull/12916)
added the lane.

## Names and seats

An agent on this lane has type `reviewer`, for example
`reviewer@a1b2c3-0001`. Parsing that name identifies the `review` lane. Review
seats use the worker numbering scheme, such as `review-2@<slug>`; the seat
address is distinct from the name of its current occupant.

Numbered seats provide the identity used by shared seat memory and inbox
routing. See [Agent names](../pillars/swarm.md#agent-names) and
[Context recycle](../pillars/swarm.md#context-recycle) for the shared conventions.
The lane sets the name and seat format. Launch, notes priming and handoff for
a reviewer happen only when a launch path places one on the lane; see
[Limits](#limits).

## Configure a new swarm

Every parsed template includes a `review` entry. Its defaults are:

| Field | Default | Meaning |
|---|---|---|
| `cap` | `1` | Maximum reviewer count requested when creating the swarm |
| `profile` | `reviewer` | Profile name recorded for the lane |
| `kind` | `review` | Default task kind recorded for the lane |

Save this JSON as `review-workers.json` under
`$AGENTIHOOKS_HOME/swarm-templates/`, or
`~/.agentihooks/swarm-templates/` when `AGENTIHOOKS_HOME` is unset:

```json
{
  "name": "review-workers",
  "lanes": {
    "review": {
      "cap": 1,
      "profile": "reviewer",
      "kind": "review"
    }
  }
}
```

Omitted lanes receive their usual defaults. The cap must be a nonnegative
integer; `0` requests no reviewer capacity. A swarm created without
`--template` gets the same defaults, so it starts with a review cap of `1`.
Creation has no review cap flag, so a template with `"cap": 0` on its `review`
lane is the way to create a swarm with no reviewer capacity. The template must
be available in the configuration home of the process that creates the swarm.
A user template with the same name as a built-in takes precedence.

List the available templates, then select yours when creating a new swarm with
your plan. Replace the placeholders with the swarm name, repository directory
and plan file:

```bash
agentihooks swarm templates

agentihooks swarm init <name> \
  --repo <repository-directory> \
  --plan <plan-file> \
  --template review-workers
```

Creation records the template name and lane map, and copies the review cap to
the stored `SwarmConfig.max_review` field. Repeating `init` for an existing
swarm leaves its configuration unchanged. See
[Templates](../pillars/swarm.md#templates) for the other lane fields and
[Commands](../pillars/swarm.md#commands) for swarm creation requirements.

### Existing configurations and autoscale

An existing stored configuration without `max_review` loads it as `0`. Template
defaults do not migrate existing swarms. A template saved from a swarm uses
that swarm's actual review cap, including zero.

Capacity includes the review lane when reading configured and effective caps.
The configured cap is a ceiling: ready work, account availability and host room
still limit placement. Review demand is kept on its own lane.

Autoscale sizes `eng`, `ci` and `plan`; it preserves the configured review cap.
Its ceiling calculation does not allocate an automatic reviewer share. Quota
output combines the fixed review ceiling with the autoscaled ceilings of the
other lanes and can still read decisions saved before review was added.

No `--max-review-agents` creation flag or `max-review-agents` setting
exists. Configure the review cap in the template used for
new swarm creation; changing a template file does not change a running swarm.

## Add and inspect a review task

Use the API for writes. A review task must specify both `--lane review` and
`--kind review`. The lane accepts only review tasks, and the review kind is
accepted only on that lane. Selecting the lane does not fill in the kind during
task creation.

This example also records a proof contract for the supplied change. Replace
`<slug>` and `<agent>` with the swarm and the caller's name:

```bash
agentihooks api \
  --slug <slug> \
  --as <agent> \
  task add - \
  "Review the supplied pull request" \
  --lane review \
  --kind review \
  --description "Review the supplied change and record findings" \
  --plan-slice none \
  --must "Findings identify the reviewed change" \
  --check "Inspect the findings and their evidence" \
  --judge reviewer
```

`-` requests a task id from the API. The description and proof contract should
identify the actual pull request or merge request and its expected evidence.
Use the ledger command for a filtered read:

```bash
agentihooks ledger \
  --slug <slug> \
  tasks \
  --lane review
```

The tick includes `review` when listing ready tasks. An open review task that
passes the normal claimability checks appears on that lane, and new review
work makes a drained swarm return to `running`. Existing lanes retain their
own task listings and caps.

The review kind names no proof fields, so `done` closes a review task with no
verdict, proof or pull request. A review task that records a pull request,
through `--pr` or its stored one, closes only after that pull request merges,
unless `--filed` names a follow up. The `--must`, `--check` and `--judge`
contract goes into the agent's prompt as its proof contract line.

## Limits

The tick's lane list and the event engine's offer scope are separate. The
event readiness reactor offers only `eng`, `ci` and `plan` work, so it never
offers a review task. Hive placement covers the `eng`, `ci`, `plan`,
`dispatch` and `expert` lanes and any task that names a hive, so a review task
that names no hive is placed by the deployment's other backends. A stored
review task, a ready-task listing or a swarm returning to `running` therefore
does not show that a reviewer will launch. See the
[event driven engine](../pillars/swarm.md#event-driven-engine).

The `reviewer` package role runs the `swarm-reviewer` skill. Its conditions
refuse edits, Serena writes and shell writes outside `~/scratchpad` and the
seat's task folder; see [Read only roles](read-only-roles.md). Naming the
profile in a template records the selection. Nothing adds review tasks on its
own: no pull request, merge request, merge group or release event creates one,
and autoscale has no reviewer share.

## Troubleshooting

| Symptom | Check or action |
|---|---|
| Task creation says review tasks must use the review lane | Supply `--lane review --kind review` together. A review kind on another lane, or another kind on the review lane, is refused. |
| A template has no visible review entry | Check the installed version and the configuration home used by `swarm templates`. Parsed templates include review even when the JSON omits it. |
| An older swarm has zero reviewer capacity | Configurations missing `max_review` default to zero. Editing a template or repeating `init` does not migrate that stored cap. |
| Autoscale changes the other ceilings but leaves review unchanged | Review retains its configured cap; autoscale has no reviewer share. |
| Review capacity is below its configured ceiling | Inspect account capacity, host room and claimable review demand. A cap permits capacity but does not reserve an account or force placement. |
| A review task is listed but no reviewer starts | The event engine never offers review work, and a hive places a review task only when the task names that hive. Check which placement backend the deployment uses for it and that the reviewer profile is installed there. |
