---
title: Review lane
parent: Reference
nav_order: 95
permalink: /docs/reference/swarm-review-lane/
---

# Review lane

The `review` lane gives review tasks their own kind, reviewer names, numbered
seats and capacity ceiling alongside `eng`, `ci` and `plan`. This reference
covers the lane infrastructure introduced by
[pull request #12916](https://github.com/the-cloud-clockwork/agentihooks/pull/12916).

## Names and seats

An agent on this lane has type `reviewer`, for example
`reviewer@a1b2c3-0001`. Parsing that name identifies the `review` lane. Review
seats use the worker numbering scheme, such as `review-2@<slug>`; the seat
address is distinct from the name of its current occupant.

Numbered seats provide the identity used by shared seat memory and inbox
routing. See [Agent names](../pillars/swarm.md#agent-names) and
[Context recycle](../pillars/swarm.md#context-recycle) for the shared conventions.
Reviewer launch, notes priming and handoff execution require their lifecycle
integration; the lane registration alone establishes the name and seat format.

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
integer; `0` requests no reviewer capacity. The template must be available in
the configuration home of the process that creates the swarm. A user template
with the same name as a built-in takes precedence.

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

This delivery adds no `--max-review-agents` creation flag or
`max-review-agents` setting. Configure the review cap in the template used for
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

## Delivery limits

The tick's lane list and the event engine's offer scope are separate. In this
delivery the event readiness reactor retains its existing offer lanes and
does not offer review work. A stored review task, a ready-task listing or a
swarm returning to `running` therefore does not establish that a hive will
launch a reviewer. See the [event driven engine](../pillars/swarm.md#event-driven-engine).

The reviewer profile implementation, launch paths, worker gates, automatic
pull request or merge request triggers, merge group and release reviews,
review verdicts and completion proof belong to separate integrations. Naming a
profile in a template records the selection; the profile and the launch
integration must also be installed for it to run.

## Troubleshooting

| Symptom | Check or action |
|---|---|
| Task creation says review tasks must use the review lane | Supply `--lane review --kind review` together. A review kind on another lane, or another kind on the review lane, is refused. |
| A template has no visible review entry | Check the installed version and the configuration home used by `swarm templates`. Parsed templates in this delivery include review even when the JSON omits it. |
| An older swarm has zero reviewer capacity | Configurations missing `max_review` default to zero. Editing a template or repeating `init` does not migrate that stored cap. |
| Autoscale changes the other ceilings but leaves review unchanged | Review retains its configured cap; an automatic reviewer quota share is outside this delivery. |
| Review capacity is below its configured ceiling | Inspect account capacity, host room and claimable review demand. A cap permits capacity but does not reserve an account or force placement. |
| A review task is listed but no reviewer starts | Check the offer and launch integration and the installed reviewer profile. This lane infrastructure alone does not offer review work through the event engine. |
