---
title: Review lane
parent: Reference
nav_order: 97
permalink: /docs/reference/swarm-review-lane/
---

# Review lane

The `review` lane gives review tasks their own kind, reviewer names, numbered
seats and capacity ceiling alongside `eng`, `ci` and `plan`, and launches a
reviewer with the `reviewer` profile wherever an engineering task launches.
[Pull request #12916](https://github.com/the-cloud-clockwork/agentihooks/pull/12916)
added the lane;
[pull request #12938](https://github.com/the-cloud-clockwork/agentihooks/pull/12938)
carried it through every launch path. See
[Where reviewers launch](#where-reviewers-launch).

For fleet review rules, platform connections and both target configurations,
see [Reviewer bundle overlay](reviewer-bundle-overlay.md).

## Names and seats

An agent on this lane has type `reviewer`, for example
`reviewer@a1b2c3-0001`. Parsing that name identifies the `review` lane. Review
seats use the worker numbering scheme, such as `review-2@<slug>`; the seat
address is distinct from the name of its current occupant.

Numbered seats provide the identity used by shared seat memory and inbox
routing. See [Agent names](../pillars/swarm.md#agent-names) and
[Context recycle](../pillars/swarm.md#context-recycle) for the shared conventions.
The lane sets the name and seat format. A reviewer launches on the same paths
as an engineer; see [Where reviewers launch](#where-reviewers-launch).

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
own task listings and caps. On the `events` engine the readiness reactor also
offers a ready review task on the review ready queue, where a review spoke
takes it.

The review kind names no proof fields, so `done` closes a review task with no
verdict, proof or pull request. A review task that records a pull request,
through `--pr` or its stored one, closes only after that pull request merges,
unless `--filed` names a follow up. The `--must`, `--check` and `--judge`
contract goes into the agent's prompt as its proof contract line.

## Where reviewers launch

A review task launches a reviewer on every path an engineering task takes. The
reviewer runs the lane's profile, `reviewer` unless the template's `review`
lane names another; a profile set on the task, or a saved profile decision,
takes precedence.

| Path | What a review task gets |
|---|---|
| Workstation spawn | `review` is a model pick lane with `eng` and `ci`. With effort `auto` the spawn picks the model inside the task's size and rank band; any other effort keeps the lane's model and effort. The launch command carries `--profile reviewer`. |
| Opening prompt | The first line names the role: `You are reviewer@a1b2c3-0001, a reviewer in swarm <slug>, working in the repo <repo>.` A lane `role` replaces `a reviewer`. |
| Hive placement | `review` is a hive lane, so with hive placement on the routed runtime sends a review launch to a hive. A task pinned to the workstation stays there. |
| Kubernetes backend | The backend's default lanes are `eng`, `ci` and `review`, so a review launch runs as a worker Pod. A task pinned to the workstation, a task with `needs`, or a `frontend` profile stays on the workstation. |
| Kubernetes proof | In a proof swarm whose slug starts with `proof-` and equals the value of `AGENTIHOOKS_CONTROL_KUBERNETES_PROOF`, the controller keeps `review` launches off hives, as it does `eng`; `ci` still goes to hives. |
| Hive lane spokes | `agentihooks hive run` on a worker hive, and `agentihooks swarm spoke` on a workstation with `AGENTIHOOKS_DEPLOYMENT=distributed`, run a review spoke for every `events` swarm beside the `eng`, `ci` and `plan` spokes. |
| Ready queue | On the `events` engine the readiness reactor opens `swarm.<slug>.ready.review` with the other lane queues and offers each ready review task on it. |
| Hive member grant | A member's NATS grant takes from and acknowledges the review queue stream, `QUEUE_review`, like every other lane queue. |
| Hive orders | A hive order carries its task's lane and profile. A review task's order launches on the `review` lane with role `reviewer`, and its launch assignment keeps the `reviewer` profile. Placement maps the `reviewer` profile to the `review` role. |
| Seat spawn | Seat spawn accepts the review lane and launches a `reviewer` agent through the same runtime. |

### Keep reviewers off a hive

A hive runs a review spoke unless `HIVE_EXCLUDED` names `review`. The value is
a comma separated list with no spaces. On a worker hive Pod the chart sets it
from `workers.excluded`, default `[master]`, and the Pod's start step writes
the same list to the hive record's `excluded` setting. This values change
leaves the Pods with `eng`, `ci` and `plan` spokes and no review spoke:

```yaml
workers:
  excluded: [master, review]
```

On a workstation or VM, set it for the process that runs the spokes:

```bash
HIVE_EXCLUDED=master,review agentihooks hive run
```

`HIVE_EXCLUDED` decides only which spokes run there. Placement reads the hive
record's `excluded` setting and drops a hive whose list names the lane, the
role or the profile, so `review` or `reviewer` keeps reviewer launches off it:

```bash
agentihooks hive set desk excluded=eng,ci,review,plan,dispatch
```

See [Placement by role](../pillars/hives.md#placement-by-role).

### Check that a reviewer can launch

1. Confirm the reviewer profile is installed where the reviewer launches. A
   workstation spawn that cannot find it fails with
   `task <id> needs profile reviewer, which is not installed`; install it with
   `agentihooks init`.
2. On a hive, read the spoke log for the line
   `hive spoke for swarm <slug> lane review takes swarm.<slug>.ready.review`.
3. Check that the swarm's review cap is above zero; see
   [Existing configurations and autoscale](#existing-configurations-and-autoscale).

### Upgrade an existing hive

A hive member's credential keeps the grant it was minted with. A member minted
before the review queue was added is refused when its review spoke takes from
`QUEUE_review`, and the spoke logs
`hive spoke for swarm <slug> lane review poll failed (...)` on each poll, with
the NATS client's permissions violation in the parentheses. The other lane
spokes keep running. On a workstation or VM member run
`agentihooks hive refresh`, then restart its hive service with
`systemctl --user restart agentihooks-hive`. On Kubernetes a pod roll is
enough: the worker start step runs `hive join`, which mints the grant again.
The workstation root hive mints its stored credential again
when `hive run` starts and finds its grant differs from a fresh mint. See
[Upgrading to the stop stream](swarm-bus-scope.md#upgrading-to-the-stop-stream)
for the same steps.

## Limits

The `reviewer` package role runs the `swarm-reviewer` skill. Its conditions
refuse edits, Serena writes and shell writes outside `~/scratchpad` and the
seat's task folder; see [Read only roles](read-only-roles.md). Another profile
named on the template's `review` lane launches reviewers with that profile
instead. Nothing adds review tasks on its
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
| A review task is listed but no reviewer starts | Check that the review cap is above zero and the reviewer profile is installed where the launch lands. On the `events` engine, check that a hive logs `hive spoke for swarm <slug> lane review takes swarm.<slug>.ready.review`; a hive whose `HIVE_EXCLUDED` names `review` runs no review spoke. |
| A workstation spawn fails with `needs profile reviewer, which is not installed` | Install the profile with `agentihooks init`, or name an installed profile on the template's `review` lane. |
| A review spoke logs `poll failed` with a permissions violation | The hive member's credential predates the review queue grant. Run `agentihooks hive refresh` on a workstation or VM member and restart its hive service, or roll the pod on Kubernetes; see [Upgrade an existing hive](#upgrade-an-existing-hive). |
| Reviewers launch on the workstation although worker lanes run on the pods | The workstation hive's `excluded` list names `eng`, `ci`, `plan` and `dispatch` but not `review`. Add `review` to it with `agentihooks hive set`. |
| A kubernetes proof swarm launches reviewers off the hives | Expected: in a kubernetes proof the controller keeps `eng` and `review` launches off hives. |
