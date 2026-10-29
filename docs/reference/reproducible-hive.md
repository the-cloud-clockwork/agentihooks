---
title: Reproducible hive
parent: Reference
nav_order: 21
---

# Reproducible hive

Two workstation commands add and remove a Kubernetes hive: `agentihooks hive create` brings a new hive live in herdr
with an engineer working on it, and `agentihooks hive remove` drains a hive and deletes everything it owns. Every
other step, the antoncore pull request, the OpenBao record, the member credential and the herdr entry, runs inside
them.

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
| drain | marks the hive's registry record draining, so placement sends it no new agent, and orders each agent on it, through its inbox, to finish its step and hand off; its successor starts on another hive. Waits up to `--wait` (3600) seconds; past that it refuses naming the agents still running, and a rerun keeps waiting |
| member | revokes the hive's member credential and its Redis user, and its NATS credential when a NATS signer is configured |
| herdr | deletes the herdr machine and its `known_hosts` entry |
| registry | deletes the hive's registry records, keeping its wait history in the swarm, releases the hive name of every node holding that name and deletes the saved create when it names that hive |
| manifests | opens one pull request into antoncore `dev` deleting the hive's ArgoCD application, manifests and values, and turns on auto merge; where the repository refuses auto merge, it prints that the pull request must be merged once its checks pass |
| openbao | deletes the hive's record `k8s/agentihooks-hive-<name>` |

A refusal prints `hive remove refused: <reason>` and exits 1. `--label`, `--path`, `--repo` and `--base` override the
herdr label and manifest name, the OpenBao record, the GitOps repository and its branch.

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
| `drain` refuses | an agent did not hand off within `--wait` | rerun `hive remove`; it keeps waiting |

## Related pages

- [Hives](../pillars/hives.md)
- [Deployment modes](deployment-modes.md)
