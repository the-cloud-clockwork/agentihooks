---
title: Reproducible hive
parent: Reference
nav_order: 21
---

# Reproducible hive

Two workstation commands add and remove a Kubernetes hive: `agentihooks hive create` brings a new hive live in herdr
with an engineer working on it, and `agentihooks hive remove` drains a hive and deletes everything it owns. Every
other step, the antoncore pull request, the OpenBao record, the member credential and the herdr entry, runs inside
them. [Invite a member by hand](#invite-a-member-by-hand) runs the credential step alone, and
[Time a run from zero to a healthy engineer](#time-a-run-from-zero-to-a-healthy-engineer) times the path with
`scripts/packaging/hive-reproduce.sh`.

1. TOC
{:toc}

---

## What a hive is made of

| Piece | Where it lives | Written by |
|---|---|---|
| The release: worker hive Deployment and its worktrees ConfigMap | the agentihooks chart with `hiveOnly: true` and `nats.enabled: false`, values in antoncore `k8s/values-overlays/<release>/values.yaml` | `hive create`, as a pull request into antoncore `dev` |
| The ArgoCD application, which creates the namespace | antoncore `k8s/argocd/apps/<release>.yaml` | the same pull request |
| ExternalSecrets for the member credential, ledger address and CA, accounts and settings | antoncore `k8s/manifests/<release>/` | the same pull request |
| The member credential (`credential`), ledger address (`swarm_url`) and ledger CA (`ca_pem`) | OpenBao `secret/k8s/<release>` | `hive create` |
| Claude and Codex accounts, agent settings, GitHub App | OpenBao `agentihooks/claude-creds`, `agentihooks/codex-creds`, `k8s/agentihooks-swarm-workstation-hive-settings`, `k8s/agenticore` | shared with every hive, by reference |

`hiveOnly: true` renders only the worker hive: no controller, ledger, Redis, network policy or ledger test hook, so a
second release never starts a second swarm. One release is one hive, because one credential is one hive id.

`nats.enabled: false` leaves out the bundled NATS, which the chart otherwise installs with every release.

## Create a hive from nothing

On the workstation (the UI hive), with `OPENBAO_URL` and `OPENBAO_TOKEN` set and `gh` and `kubectl` logged in:

```bash
agentihooks hive create --dry-run   # every step and every rendered file, nothing written
agentihooks hive create
```

| Step | What it does | Skipped on a rerun when |
|---|---|---|
| name | the node's stable name `swarm-hive-NNN`, the lowest free ordinal given to that node once and kept in the Redis hash `agentihooks-hive:machines`; it is the release, namespace, NFS home folder and herdr label. The LAN address is the one the `agentihooks-swarm` shared MetalLB services carry; the SSH port is the port of the existing `<name>-ssh-lan` service, else the first port from 2222 free on it | an unfinished create is saved in `hive-create.json` under the agentihooks home |
| invite | `hive invite` into `k8s/agentihooks-hive-<name>`, like `k8s/agentihooks-swarm-workstation-hive` | the record holds a credential |
| ssh keys | a new ed25519 host key and every `~/.ssh/*.pub` as `ssh_host_key`, `ssh_host_public_key` and `ssh_authorized_keys` in the same record | the record holds a host key |
| manifests | renders antoncore `k8s/templates/agentihooks-hive.yaml` and opens one pull request into antoncore `dev`; the rendered manifests make the hive's NFS home folder, so the NFS server never restarts | the saved create holds the pull request |
| merge | `gh pr merge --squash --auto`; where the repository refuses auto merge, waits for the pull request checks to pass and runs `gh pr merge --squash`; then waits for the merge. A failed check stops the create naming it | the pull request merged |
| settings | `hive setup` into the hive settings record | never; it rewrites the same keys |
| live | waits for the member's heartbeat | never; a live hive passes at once |
| registry | records the hive name and its repos in the member's hive registry record, so `hive list` shows `name=<name>`; the repos are `--repos` (comma separated `host/owner/name`, kept across a resumed create), else the repos of every swarm the hive joins. Every heartbeat keeps these repos listed beside the checkouts the hive reports | never; it rewrites the same fields |
| herdr | trusts the host key in `~/.ssh/known_hosts`, then saves `ssh://worker@<LAN address>:<port>` in herdr with `herdr machine add --label <name>`; the pod keeps the member the invite wrote, so the step mints no second member and delivers no credential | the saved create holds the added member |
| engineer | waits for a non master agent working on the hive | never |

It needs antoncore `k8s/templates/agentihooks-hive.yaml` on `dev` and at least one running swarm with
`hive-placement=on`, because the engineer comes from that swarm's placement; without one it refuses before naming a
hive. Each wait
is bounded by `--timeout` (1800 seconds). A refusal prints `hive create refused: <reason>` and exits 1; a
rerun resumes the saved create from its first unfinished step. A hive is one machine: a create on a node that already has a hive redeploys that hive under the same name, keeping
its record, port, pull request, home folder and herdr entry, and a create on another node names the next hive. Grow a
hive with larger requests and more max agents in its values, never with a second hive on the same node.
`--node`, `--repo`, `--base` and `--like` override the node, the GitOps repository, its branch and the record copied.

## Remove a hive

On the workstation, with the same settings:

```bash
agentihooks hive remove <name> --dry-run   # every step, nothing written
agentihooks hive remove <name>
```

`<name>` is the hive name (`swarm-hive-NNN`) or its member id.

| Step | What it does |
|---|---|
| drain | marks the hive's registry record draining, so placement sends it no new agent, and orders each agent on it, through its inbox, to finish its step and hand off; its successor starts on another hive. Waits up to `--wait` (3600) seconds for them to end. Then every claim an unfinished agent on the hive still holds without a handoff is released; a finished agent closed through ready or done and keeps its task with the swarm, and its task reopens; every unfinished agent left on the hive is then retired, before any later step deletes the hive. A retirement the store refuses stops the remove, and a rerun retries it. It reports how many agents it ordered, how many handed off, which claims it released and which agents it retired |
| member | revokes the hive's member credential and its Redis user, and its NATS credential when a NATS signer is configured |
| herdr | deletes the herdr machine and its `known_hosts` entry |
| registry | deletes the hive's registry records, keeping its wait history in the swarm, releases the hive name of every node holding that name and deletes the saved create when it names that hive |
| manifests | opens one pull request into antoncore `dev` deleting the hive's ArgoCD application, manifests and values, and turns on auto merge; where the repository refuses auto merge, it prints that the pull request must be merged once its checks pass |
| openbao | deletes the hive's record `k8s/agentihooks-hive-<name>` |

A refusal prints `hive remove refused: <reason>` and exits 1. `--label`, `--path`, `--repo` and `--base` override the
herdr label and manifest name, the OpenBao record, the GitOps repository and its branch.

## Invite a member by hand

`hive create` runs its invite step through `agentihooks hive invite`. Run it alone to put a new member credential into
the OpenBao record of a hive whose GitOps values are already synced, for example to replace a revoked member:

```bash
agentihooks hive invite <label> --like k8s/agentihooks-swarm-workstation-hive
```

It needs `OPENBAO_URL` and `OPENBAO_TOKEN` and reaches the swarm Redis this workstation uses. It mints one member named
`<label>` and merges these keys into the record:

| Key | Value |
|---|---|
| `credential` | the member's `hive.env`: `AGENTIHOOKS_HIVE_ID`, `AGENTIHOOKS_HIVE_URL`, `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL`, `AGENTIHOOKS_HIVE_REDIS_URL`; `AGENTIHOOKS_SWARM_URL` when a ledger address is known; with a NATS address also `AGENTIHOOKS_BUS=nats` and `AGENTIHOOKS_BUS_URL`, and with a bus grant `AGENTIHOOKS_BUS_CREDENTIALS`, `AGENTIHOOKS_BUS_BIND=1` and `AGENTIHOOKS_BUS_SWARMS` |
| `swarm_url` | the ledger address the hive reaches |
| `ca_pem` | the ledger CA |

| Flag | Default | Meaning |
|---|---|---|
| `--path` | `k8s/agentihooks-hive-<label>` | the record under the `secret` mount that the hive's ExternalSecrets read |
| `--like` | none | a working hive's record; the hive URL, Redis URL, NATS address, ledger address and CA are copied from it |
| `--url` | from `--like` | the hive endpoint the new hive reaches for `hive refresh`; required without `--like` |
| `--redis-url` | from `--like`, else this workstation's | the Redis URL the new hive connects to |
| `--swarm-url` | from `--like` | the ledger address the new hive reaches |
| `--bus-url` | from `--like`, else the hub signer record | the NATS address the new hive reaches |
| `--swarm` | every swarm | a swarm the member's bus credential reaches; repeat for several. Applies only when `AGENTIHOOKS_NATS_SIGNER` names the hub signer record |

With `AGENTIHOOKS_NATS_SIGNER` set and that signer record already holding its member swarm listing (`agentihooks hive
scope` writes it), the new member is listed there with its swarms, the credential carries `AGENTIHOOKS_BUS_SCOPED=1`,
and the invite force syncs the ExternalSecret `AGENTIHOOKS_NATS_SIGNER_SYNC` (namespace/name, default
`agentihooks-swarm/agentihooks-swarm-nats-signer`). The success line then ends `requested a sync of the signer record
<target>`, or `could not force the sync of <target> (<reason>), so hive join sees it at the next refresh`; a failed
sync never fails the invite. Without the listing the member is not listed and the credential is not scoped.

Success prints `invited hive member <id> as <label>; OpenBao secret/<path>: wrote <keys> (<n> keys total)`, with key
names only. A rerun on the same record revokes the member the record held before and adds `revoked the previous member
<id>`. When the OpenBao write fails the new member is revoked and the command exits with the error. A `--like` record
that is empty or missing, or no hive URL from `--url` or `--like`, refuses before any member is minted.

The pod reads the new credential only after its ExternalSecrets sync and the worker hive Deployment restarts:

```bash
kubectl -n <namespace> annotate externalsecret --all force-sync=$(date +%s) --overwrite
kubectl -n <namespace> rollout restart deployment -l app.kubernetes.io/component=worker-hive
```

## Time a run from zero to a healthy engineer

`scripts/packaging/hive-reproduce.sh` runs the invite path against a namespace ArgoCD already synced and records the
wall time of every step up to the first healthy engineer: an engineer working on the new hive whose join report grades
`complete` or `partial`. Run it on the workstation from an agentihooks checkout, with
the same settings as `hive invite` and `kubectl` reaching the cluster:

```bash
scripts/packaging/hive-reproduce.sh <label> [<namespace>]
```

`<namespace>` defaults to `agentihooks-hive-<label>`; pass the hive name for a hive `hive create` made, whose namespace
is its name. It refuses at once unless a swarm has `hive-placement=on`, and fails if the namespace does not exist.

| Step | Done when |
|---|---|
| `namespace` | the namespace lists at least `HIVE_EXTERNAL_SECRETS` ExternalSecrets |
| `invite` | `hive invite <label> --path <record> --like <like record>` printed a member id |
| `secrets synced` | after a forced sync, every ExternalSecret in the namespace is Ready |
| `pod ready` | after a rollout restart, the worker hive pod is Ready |
| `heartbeat` | the member's hive registry record is live |
| `engineer working` | an agent that is not a master works on the member's hive |
| `join report` | a join report on the hive grades `complete` or `partial` |

Each step prints `hive-reproduce: <step> after <seconds>s: <detail>`. At the end it prints a table of every step with
its UTC time, the seconds it took and the seconds since the start; the elapsed seconds of the `join report` row are
the wall time. A step past its limit prints `hive-reproduce: <step> after <seconds>s: timed out: <reason>` and the
table so far, and exits 1. The outcome line, `healthy engineer after <seconds>s` or `failed at <step>`, is written only
to `HIVE_TIMING_FILE`.

A failure outside a timed wait exits at once with no table: a missing namespace (the `kubectl get namespace` error),
an invite that refuses or prints no member id (`hive-reproduce: hive invite printed no member: <output>`), and a
failed forced sync or rollout restart (the `kubectl` error).

| Setting | Default | Meaning |
|---|---|---|
| `HIVE_RECORD` | `k8s/agentihooks-hive-<label>` | the OpenBao record the invite writes |
| `HIVE_LIKE` | `k8s/agentihooks-swarm-workstation-hive` | the record the invite copies |
| `HIVE_EXTERNAL_SECRETS` | `4` | the ExternalSecrets the namespace must hold and sync |
| `HIVE_STEP_TIMEOUT_S` | `900` | the limit of every step but `engineer working` |
| `HIVE_ENGINEER_TIMEOUT_S` | `1800` | the limit of `engineer working` |
| `HIVE_TIMING_FILE` | none | a file each run appends to: one line with the UTC time, the label and the outcome, then the step table |
| `HIVE_PYTHON` | `python3` | the interpreter that reads the swarm Redis; it must import agentihooks |
| `HIVE_KEEP` | `no` | `yes` keeps the invited member when the script exits |
| `HIVE_REMOVE_WAIT_S` | `300` | the `--wait` passed to `hive remove` |

When the script exits after its invite, on success or failure, it runs `hive remove <member> --label <label> --path
<record> --wait <HIVE_REMOVE_WAIT_S>` on the member it invited, unless `HIVE_KEEP=yes`. That is the full
[remove](#remove-a-hive): it drains the hive's agents, then deletes the member, the herdr entry, the registry records,
the OpenBao record, and opens a pull request into antoncore `dev` deleting the hive's manifests. A failed remove prints
`hive-reproduce: hive remove <member> failed; run agentihooks hive remove <member> --label <label>`; add `--path
<record>` when `HIVE_RECORD` was set. Set `HIVE_KEEP=yes` to keep the hive after a timing run:

```bash
HIVE_KEEP=yes HIVE_TIMING_FILE=~/hive-timing.log scripts/packaging/hive-reproduce.sh swarm-hive-002 swarm-hive-002
```

Record the `healthy engineer after <seconds>s` line and the step table from the timing file as the wall time of the
run.

## Check by hand

```bash
agentihooks hive list                  # every hive, its state and agents
agentihooks hive show <id>             # repos, slots and heartbeat
kubectl -n <name> logs deploy/<name>-agentihooks-swarm-worker-hive | grep worker-hive:
```

## Troubleshooting

| Symptom | Reading | Action |
|---|---|---|
| `live` times out | the pod is not Ready or cannot reach the swarm Redis named in its credential | `kubectl -n <name> describe pod` and the pod log |
| the pod stays in `CreateContainerConfigError` | a Secret its ExternalSecrets write is missing | `kubectl -n <name> describe externalsecret`; check the record the invite step printed |
| the pod stays in `ContainerCreating` | its NFS home folder does not mount on the node it landed on | the pod events name the mount; the node must reach the hive home NFS server |
| `engineer` times out | placement is off, no engineer task is open, or the hive lacks the task's repo | `agentihooks hive show <id>` for repos and slots |

## Related pages

- [Hives](../pillars/hives.md)
- [Deployment modes](deployment-modes.md)
