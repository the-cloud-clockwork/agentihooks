---
title: Reproducible hive
parent: Reference
nav_order: 21
---

# Reproducible hive

A new Kubernetes hive comes up from an empty namespace through two inputs: the antoncore GitOps values of one hive
only release, and one agentihooks command on the UI hive. `scripts/packaging/hive-reproduce.sh` runs every step after
the values merge and prints the wall time of each, up to the first engineer the hive's join report grades healthy.

1. TOC
{:toc}

---

## What a hive is made of

| Piece | Where it lives | Written by |
|---|---|---|
| The release: worker hive Deployment and its worktrees ConfigMap | the agentihooks chart with `hiveOnly: true`, values in antoncore `k8s/values-overlays/<release>/values.yaml` | a pull request into antoncore `dev` |
| The ArgoCD application, which creates the namespace | antoncore `k8s/argocd/apps/<release>.yaml` | the same pull request |
| ExternalSecrets for the member credential, ledger address and CA, accounts and settings | antoncore `k8s/manifests/<release>/` | the same pull request |
| The member credential (`credential`), ledger address (`swarm_url`) and ledger CA (`ca_pem`) | OpenBao `secret/k8s/<release>` | `agentihooks hive invite` |
| Claude and Codex accounts, agent settings, GitHub App | OpenBao `agentihooks/claude-creds`, `agentihooks/codex-creds`, `k8s/agentihooks-swarm-workstation-hive-settings`, `k8s/agenticore` | shared with every hive, by reference |

`hiveOnly: true` renders only the worker hive: no controller, ledger, Redis, network policy or ledger test hook, so a
second release never starts a second swarm. One release is one hive, because one credential is one hive id.

## Create a hive from nothing

On the workstation (the UI hive), with `OPENBAO_URL` and `OPENBAO_TOKEN` set and `gh` and `kubectl` logged in:

```bash
agentihooks hive create --dry-run   # every step and every rendered file, nothing written
agentihooks hive create
```

| Step | What it does | Skipped on a rerun when |
|---|---|---|
| name | `swarm-hive-<epoch seconds>`, the release, namespace, NFS home folder and herdr label; the SSH port is the first port from 2222 free on the swarm LAN address | an unfinished create is saved in `hive-create.json` under the agentihooks home |
| invite | `hive invite` into `k8s/agentihooks-hive-<name>`, like `k8s/agentihooks-swarm-workstation-hive` | the record holds a credential |
| ssh keys | a new ed25519 host key and every `~/.ssh/*.pub` as `ssh_host_key`, `ssh_host_public_key` and `ssh_authorized_keys` in the same record | the record holds a host key |
| manifests | renders antoncore `k8s/templates/agentihooks-hive.yaml`, adds the hive to the NFS home list and opens one pull request into antoncore `dev` | the saved create holds the pull request |
| merge | `gh pr merge --squash --auto`, then waits for the merge | the pull request merged |
| settings | `hive setup` into the hive settings record | never; it rewrites the same keys |
| live | waits for the member's heartbeat | never; a live hive passes at once |
| herdr | trusts the host key in `~/.ssh/known_hosts`, then `hive add ssh://worker@<LAN address>:<port> --label <name>` | the saved create holds the added member |
| engineer | waits for a non master agent working on the hive | never |

Each wait is bounded by `--timeout` (1800 seconds). A refusal prints `hive create refused: <reason>` and exits 1; a
rerun resumes the saved create from its first unfinished step. A finished create makes the next run start a new hive.
`--node`, `--repo`, `--base` and `--like` override the node, the GitOps repository, its branch and the record copied.

## The one command

On the UI hive, with `OPENBAO_URL` and `OPENBAO_TOKEN` set:

```bash
agentihooks hive invite anton-2 --like k8s/agentihooks-swarm-workstation-hive
```

- It mints a hive member named after the label and writes `credential`, `swarm_url` and `ca_pem` into
  `k8s/agentihooks-hive-<label>` (`--path` names another record). Other keys in the record are kept.
- `--like <record>` copies the hive refresh URL, the Redis host, the ledger address and the ledger CA from a hive
  that already works, so no address is typed. `--url`, `--redis-url` and `--swarm-url` override the first three. The
  CA comes from the `--like` record, else from this host's ledger CA; without `--like` the Redis URL is this host's.
- It prints the member id and key names only, never a value:
  `invited hive member <id> as anton-2; OpenBao secret/k8s/agentihooks-hive-anton-2: wrote ca_pem, credential, swarm_url (3 keys total)`.
- A rerun mints a new member and revokes the one the record held, ending with `; revoked the previous member <id>`.
- Refusals print `hive invite refused: <reason>` and exit 1: no OpenBao settings, an unknown hive URL, a `--like`
  record that is empty, or an OpenBao error. A failed write revokes the member it just minted; if that revoke fails
  too, the refusal names the member left behind.

## Runbook

### 1. Values (one pull request into antoncore `dev`)

Copy the three files of `agentihooks-hive-anton-2` under a new release name and change the name, the record path
and the node. Pick a node whose free memory requests cover `workers.maxAgents` times
`workers.memoryPerAgentMi.request`:

```bash
kubectl describe node <node> | sed -n '/Allocated resources/,/Events/p'
```

The application tracks the agentihooks `main` chart. Merge it only once a release on `main` carries `hiveOnly`:
before that the chart ignores the key and renders a whole swarm into the namespace. After the merge, ArgoCD creates the namespace and the release. The pod waits in `CreateContainerConfigError` until its
credential Secret exists.

### 2. Invite, join and time it

```bash
scripts/packaging/hive-reproduce.sh anton-2 agentihooks-hive-anton-2
```

| Step | Done when | Bound |
|---|---|---|
| namespace | the namespace holds its `HIVE_EXTERNAL_SECRETS` (4) ExternalSecrets | `HIVE_STEP_TIMEOUT_S` (900) |
| invite | `hive invite` wrote the record | none |
| secrets synced | every ExternalSecret in the namespace is Ready, after a forced sync | `HIVE_STEP_TIMEOUT_S` |
| pod ready | the worker hive pod is Ready, after a rollout restart so it reads the new credential | `HIVE_STEP_TIMEOUT_S` |
| heartbeat | `hive show <id>` has a heartbeat under 90 seconds old | `HIVE_STEP_TIMEOUT_S` |
| engineer working | the swarm runs a non master agent on the hive in state working | `HIVE_ENGINEER_TIMEOUT_S` (1800) |
| join report | a join report on the hive grades an agent complete or partial | `HIVE_STEP_TIMEOUT_S` |

`HIVE_RECORD` and `HIVE_LIKE` override the record paths, `HIVE_EXTERNAL_SECRETS` the number of ExternalSecrets the
namespace holds; `HIVE_TIMING_FILE` appends the table to a file. A step past its
bound prints `timed out: <what>` with the table so far and exits 1. Each row carries its UTC time, the seconds the step
took and the seconds since the script started.

The clock starts when the script starts, after ArgoCD created the namespace; the time from the antoncore merge to the
namespace is the namespace's `creationTimestamp` minus the merge time.

The script refuses to start unless at least one swarm has `hive-placement=on`; it does not check that this is the
swarm holding the engineer task. The refusal names `agentihooks swarm <slug> set
hive-placement=on`; turning placement on is the operator's call. The engineer step also needs an open engineer task
for a repo the hive lists. Placement fills hives first, so the next engineer the tick spawns lands on the new hive while
it has a free slot.

A hive is healthy at its first join report graded `complete` (every check passed) or `partial` (only optional checks,
such as the brain overlay, missed). A `failed` grade, a required check missed, never counts.

### 3. Check by hand

```bash
agentihooks hive show <id>             # repos, slots and heartbeat
agentihooks hive compare               # configuration against this hive
kubectl -n <namespace> logs deploy/<release>-agentihooks-swarm-worker-hive | grep worker-hive:
```

## Removing a hive

Revert the antoncore pull request; ArgoCD prunes the release. Then `agentihooks hive revoke <id>` deletes the member's
ledger credential and Redis user. The OpenBao record stays until it is deleted by hand.

## Troubleshooting

| Symptom | Reading | Action |
|---|---|---|
| `secrets synced` times out | an ExternalSecret cannot read its record | `kubectl -n <namespace> describe externalsecret`; check the record path the invite printed |
| the pod stays in `CreateContainerConfigError` | a Secret named in `envFromSecrets` or `credentialSecret` is missing | the same `describe`; every Secret comes from one ExternalSecret |
| `heartbeat` times out with the pod Ready | `hive run` cannot reach the swarm Redis named in the credential | the pod log; rerun the invite with `--redis-url` set to an address the node reaches |
| `engineer working` times out | placement is off, no engineer task is open, or the hive lacks the task's repo | `agentihooks hive show <id>` for repos and slots |

## Related pages

- [Hives](../pillars/hives.md)
- [Deployment modes](deployment-modes.md)
