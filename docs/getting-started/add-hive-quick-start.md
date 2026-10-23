---
title: Add a Hive
nav_order: 7
parent: Getting Started
permalink: /docs/getting-started/add-hive-quick-start/
---

# Add a Hive

A hive is a machine or a Kubernetes pod that runs swarm agents. It joins a running hub with one private invite file and then pulls work from the swarm. The same invite enrolls a machine or a Kubernetes worker hive. The [hives guide](../pillars/hives.md) covers every option.

## Prerequisites

- A running hub: the [Kubernetes quick start](kubernetes-quick-start.md) or the [single machine quick start](single-machine-quick-start.md).
- On the hub side, a shell with the swarm Redis settings, `OPENBAO_URL` and `OPENBAO_TOKEN` set, and the hive endpoint (`agentihooks hive serve`, or the chart's `hive.join`) reachable over HTTPS from the new hive.
- The NATS address the new hive reaches, such as `nats://swarm-bus.example.internal:4222`.
- On a new machine: Python 3.12 or later, `uv tool install agentihooks`, and Claude Code or Codex signed in. In a cluster: Helm 3.8 or later and a namespace for the hive.

## Issue the invite

On the hub:

```bash
agentihooks hive invite lab-vm \
  --url https://swarm.example.internal:8770/hive/refresh \
  --bus-url nats://swarm-bus.example.internal:4222
```

`hive invite` mints a hive member named `lab-vm` and writes its invite into the OpenBao record `secret/k8s/agentihooks-hive-lab-vm` under the key `credential`, and prints `invited hive member <member id> as lab-vm`; the member id is the hive id. `--like <record>` copies the endpoint, the bus address and the ledger CA from another hive's record instead. Inviting the same label again replaces the member and revokes the previous one.

The invite carries the member's ledger credential. Move it only through your secret manager, keep the file mode 600, never source it and never paste it.

## Join a machine

Place the `credential` value of the record in a private file on the new machine, for example with `umask 077` and your OpenBao client, then:

```bash
agentihooks hive join ~/invite
agentihooks hive install
```

`hive join` reads the file without executing it, refreshes the member through the hive endpoint and saves its Redis and NATS settings in `hive.env` under the agentihooks home. `hive install` writes and enables the `agentihooks-hive` user service, which runs `agentihooks hive run`. Delete the invite file once the join succeeds.

A hub with a private CA must be trusted before joining:

```bash
export SSL_CERT_FILE=/etc/ssl/certs/swarm-ca-bundle.pem
```

## Join a Kubernetes hive

Create a Secret holding the invite under the key `invite`, from an ExternalSecret on the OpenBao record or locally:

```bash
kubectl -n hive-lab create secret generic hive-invite --from-file=invite=./invite
```

Write `hive-values.yaml`:

```yaml
hiveOnly: true

nats:
  enabled: false

workers:
  replicas: 1
  maxAgents: 4
  image:
    tag: release
  credentialSecret:
    name: hive-invite
    key: invite
  githubAuth:
    mode: token
    secretName: hive-github
    tokenKey: token
```

```bash
helm upgrade --install hive-lab oci://ghcr.io/the-cloud-clockwork/charts/agentihooks-swarm \
  --version <version> --namespace hive-lab -f hive-values.yaml
```

`hiveOnly: true` renders one worker hive and no hub or Redis, and `nats.enabled: false` keeps the bundled NATS out. Worker startup runs `agentihooks hive join` before it accepts work and stops if enrollment fails. Use one invite and one release per hive. For a private hub CA set `workers.caSecret`. GitHub and provider credentials stay separate Secret references.

## Verify

On the hub:

```bash
agentihooks hive list
agentihooks hive show <member id>
```

`hive list` shows the new hive `live` with its agent limit and headroom. `hive show` prints its record as JSON, including the `repos` the hive can work. In a cluster, `kubectl rollout status deployment/hive-lab-agentihooks-swarm-worker-hive -n hive-lab` reports `successfully rolled out`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `the invite needs a hive id, endpoint and ledger credential` | The file is not the record's `credential` value; copy that key again |
| `the invite file could not be read` | Check the path and that the joining user can read the file |
| `the hive answered for a different invited member` | The label was invited again after this file was issued; use the newest invite |
| Certificate verify failed during join | Set `SSL_CERT_FILE` on a machine or `workers.caSecret` in the chart to the hub CA bundle |
| The hive never shows `live` | `systemctl --user status agentihooks-hive` on a machine, or the worker pod logs in a cluster; check the NATS address from `--bus-url` is reachable from the hive |
| Launches wait with no hive listing a repository | Clone the repository under `HIVE_REPO_ROOT` on the hive, then check `hive show` |

Remove a hive with `agentihooks hive remove <id>`, which drains its agents before revoking the member.
