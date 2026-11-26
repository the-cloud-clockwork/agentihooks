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
[Pull request #12934](https://github.com/the-cloud-clockwork/agentihooks/pull/12934)
added the reserved review share in autoscale and verified review harness affinity.

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

Autoscale sizes `eng`, `ci` and `plan` on every pass. It sizes the review lane
only while a reviewer is live or a review task waits, through the
[reserved review share](#reserved-review-share); otherwise it keeps the
configured review cap. Quota output combines the review ceiling with the
autoscaled ceilings of the other lanes and can still read decisions saved
before review was added.

Set the review cap of a running swarm with
`agentihooks swarm <slug> set max-review-agents=N` (or the bare pair
`agentihooks swarm <slug> max-review-agents=N`), a whole number stored as
`max_review` beside the other lane caps; it applies at the next tick and does
not pin the autoscaled caps. `swarm <slug> status` shows it as `review N`.
No `--max-review-agents` creation flag exists: a new swarm takes its review cap
from the template, and changing a template file does not change a running swarm.

## Pin the review harness, model and effort

The review lane takes the same `agent`, `model` and `effort` fields as the
other worker lanes. Set them on a running swarm; the next reviewer launch uses
them, and running reviewers keep the harness they launched on:

```bash
agentihooks swarm <id> set \
  review-agent=codex \
  review-model=gpt-6.1-sol \
  review-effort=xhigh
```

Or set them in the template's `review` entry so a new swarm starts pinned:

```json
{
  "name": "codex-review",
  "lanes": {
    "review": {
      "cap": 2,
      "agent": "codex",
      "model": "gpt-6.1-sol",
      "effort": "xhigh",
      "profile": "reviewer",
      "kind": "review"
    }
  }
}
```

With `agent` set to `codex`:

| Stage | Behaviour |
|---|---|
| Placement | Capacity offers the review lane Codex seats only. A Claude account with free seats takes no reviewer. |
| Codex full | The review lane gets no seat and its tasks wait until a Codex seat frees. The lane never falls back to Claude. |
| Launch | A reviewer launches on the explicit lane `model` at the lane `effort`. The command and template above keep the selected Codex model at `xhigh`. |
| Stuck harness | The bottleneck remedy moves only a pinned `eng` or `ci` lane. When Codex is the stuck stage, the review pin stays on Codex, and with no `eng` or `ci` lane pinned to Codex the remedy records `no lane is pinned to codex`. |

`claude` pins the lane to Claude the same way, and `auto`, the default, offers
both harnesses. See [Harness pin findings](../pillars/swarm.md#harness-pin-findings)
for pin diagnostics. While review tasks wait, the
[reserved review share](#reserved-review-share) counts only the free seats of
the pinned harness.

## Reserved review share

Under auto scaling, the default for a new swarm, autoscale splits one total
ceiling between the lanes on every pass. While review tasks wait, it holds
seats for the review lane before `plan`, `ci` and `eng` take theirs. The share
is a swarm setting:

```bash
agentihooks swarm <id> set review-share=30
```

| Setting | Stored field | Default | Values |
|---|---|---|---|
| `review-share` | `review_share` | `20` | A whole percent from 0 to 100 of the total ceiling. Values outside this range or noninteger values are refused. A swarm stored before the setting loads `20`. |

A manual scaling swarm has no autoscale decision, so the share does not
apply; the review lane keeps its configured cap.

### How many seats are held

Each pass reads the live reviewers, the waiting review tasks, the free quota
seats of the harnesses the review lane may use, and the total ceiling for the
pass. A waiting review task is an open review task that passes the claim
checks and has no fresh placement refusal.

1. With no live reviewer and no waiting review task, autoscale holds nothing
   and the review lane keeps its configured cap.
2. The limit is the live reviewers plus the waiting review tasks, counting no
   more waiting tasks than the lane's harnesses have free seats.
3. The wanted count is the larger of the live reviewers plus one when any
   review task waits, and the share of the total ceiling rounded up.
4. The held seats are the smallest of the total ceiling, the limit and the
   wanted count.

The held seats come off the total first. `plan`, `ci` and `eng` then take
their live agents and one seat each for waiting work from what is left. Seats
still left go one at a time to the lane with the lowest ceiling that wants
more; the review lane wants up to its limit. Capacity writes the review
ceiling to the review cap for that pass. With live reviewers and no waiting
review task, the limit equals the live reviewers, so they keep their seats and
the share holds nothing more.

A held seat can bring another lane's ceiling below its live agents. Running
agents keep working, and that lane spawns no new agent until its count falls
under its ceiling.

Every row below has a total ceiling of 10, enough waiting engineer work to
fill the remaining ceiling, and `plan` and `ci` with no live agent and no
waiting work. Unlisted harnesses have no free seats:

| Live engineers | Live reviewers | Free seats | Review `agent` | Waiting review tasks | Share | Limit | Wanted | Held | Ceilings |
|---|---|---|---|---|---|---|---|---|---|
| 8 | 0 | Codex 4 | `codex` | 5 | 30 | 4 | 3 | 3 | eng 7, review 3 |
| 8 | 0 | Codex 4 | `codex` | 5 | 0 | 4 | 1 | 1 | eng 9, review 1 |
| 8 | 0 | Codex 4 | `codex` | 0 | 30 | none | none | none | eng 10; review keeps its configured cap |
| 8 | 2 | Codex 4 | `codex` | 0 | 20 | 2 | 2 | 2 | eng 8, review 2 |
| 4 | 0 | Claude 6 | `codex` | 5 | 50 | 0 | 5 | 0 | eng 10, review 0 |
| 4 | 0 | Claude 6 | `auto` | 5 | 50 | 5 | 5 | 5 | eng 5, review 5 |
| 4 | 0 | Claude 6 | `auto` | 1 | 50 | 1 | 5 | 1 | eng 9, review 1 |

The fifth row is a Codex pinned lane while Codex is full: the share holds no
Claude seat, so the review ceiling is 0 and its tasks wait.

### Read the held share

While review tasks wait, the autoscale reason ends with the hold, as in the
first row above:

```text
Quota seats 4 (claude 0, codex 4); host room 2; ceiling 10 of 10; review share holds 3 seats for 5 waiting review tasks.
```

`agentihooks swarm <id> autoscale --json` prints that decision, including the
review ceiling, without writing it. `agentihooks swarm <id> status` prints the
scaling line with the review lane after the other lanes:

```text
scaling auto, live of ceiling eng 8 of 7, ci 0 of 0, plan 0 of 0, review 0 of 3, free quota seats claude 0 and codex 4, because Quota seats 4 (claude 0, codex 4); host room 2; ceiling 10 of 10; review share holds 3 seats for 5 waiting review tasks
```

With no live reviewer and no waiting review task, the review entry shows the
configured review cap.

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

The review kind names no proof fields, but `done` refuses a review task until
it has posted at least one finding with `agentihooks review post`. The refusal
reads the post count kept for that swarm and task, runs before any other proof
check and applies to every task in the review lane or of the review kind. A
review task handed to the merge pass with `swarm ready` goes back open after
its pull request merges, so it still closes through `done`. A review task that
records a pull request, through `--pr` or its stored one, closes only after
that pull request merges, unless `--filed` names a follow up. The `--must`,
`--check` and `--judge` contract goes into the agent's prompt as its proof
contract line.

A release review task links its release tag page, and `agentihooks review
post` and `agentihooks review read` take that link as is. The review record
asks the forge for the promotion pull request whose squash merge the tag
points at, then reads its head, posts every finding on it and keeps the record
under the pull request link, so the posts count for the task and `done` closes
it. A tag with no single promotion pull request, such as one made by the
manual release workflow or by reusing an older tag's tree, is refused with a
forge error naming the tag. GitLab has no promotion flow and refuses release
links. A merge queue link names a commit, not a change request, and stays
refused.

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
the same list to the hive record's `excluded` setting. This change leaves the
Pods with `eng`, `ci` and `plan` spokes and no review spoke:

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
instead. The reserved review share holds seats only while review tasks wait
or a reviewer is live, and only under auto scaling.

## Troubleshooting

| Symptom | Check or action |
|---|---|
| Task creation says review tasks must use the review lane | Supply `--lane review --kind review` together. A review kind on another lane, or another kind on the review lane, is refused. |
| `swarm done` says a review task is done only after it posts at least one finding | The task has no finding counted for this swarm and task. Post the findings with `agentihooks review post <url> --slug <slug> --finding <text>` as the reviewer seat, then repeat `done`. |
| A template has no visible review entry | Check the installed version and the configuration home used by `swarm templates`. Parsed templates include review even when the JSON omits it. |
| An older swarm has zero reviewer capacity | Configurations missing `max_review` default to zero. Editing a template or repeating `init` does not migrate that stored cap; raise it with `agentihooks swarm <slug> set max-review-agents=N`. |
| Autoscale changes the other ceilings but leaves review unchanged | Autoscale holds review seats only while a review task waits or a reviewer is live, and only with `scaling=auto`. List open review tasks with the ledger read above and check the scaling mode in `agentihooks swarm <id> status`; otherwise review keeps its configured cap. |
| The reason says the review share holds 0 seats | Check the total ceiling and the free seats of the review lane's harnesses. A zero total ceiling holds nothing. A lane pinned to Codex counts Codex seats only; wait for a Codex seat, or set `review-agent=auto` to let the share count Claude seats too. |
| Review takes more seats than its share | The wanted count is at least the live reviewers plus one while a task waits; the hold remains bounded by the total ceiling and the review limit. Remaining room can increase the final ceiling up to that limit. Lower `review-share` to shrink the initial hold. |
| A Codex pinned reviewer waits while Claude has free seats | Expected: a pinned lane never moves harness, and the bottleneck remedy moves only `eng` and `ci` pins. Free a Codex seat, add Codex quota, or set `review-agent=auto`. |
| `set review-share=N` is refused | The value must be a whole number from 0 to 100. |
| Review capacity is below its configured ceiling | Inspect account capacity, host room and claimable review demand. A cap permits capacity but does not reserve an account or force placement. |
| A review task is listed but no reviewer starts | Check that the review cap is above zero and the reviewer profile is installed where the launch lands. On the `events` engine, check that a hive logs `hive spoke for swarm <slug> lane review takes swarm.<slug>.ready.review`; a hive whose `HIVE_EXCLUDED` names `review` runs no review spoke. |
| A workstation spawn fails with `needs profile reviewer, which is not installed` | Install the profile with `agentihooks init`, or name an installed profile on the template's `review` lane. |
| A review spoke logs `poll failed` with a permissions violation | The hive member's credential predates the review queue grant. Run `agentihooks hive refresh` on a workstation or VM member and restart its hive service, or roll the pod on Kubernetes; see [Upgrade an existing hive](#upgrade-an-existing-hive). |
| Reviewers launch on the workstation although worker lanes run on the pods | The workstation hive's `excluded` list names `eng`, `ci`, `plan` and `dispatch` but not `review`. Add `review` to it with `agentihooks hive set`. |
| A kubernetes proof swarm launches reviewers off the hives | Expected: in a kubernetes proof the controller keeps `eng` and `review` launches off hives. |
