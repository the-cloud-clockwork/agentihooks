---
title: Kubernetes Quick Start
nav_order: 6
parent: Getting Started
permalink: /docs/getting-started/kubernetes-quick-start/
---

# Kubernetes Quick Start

Install the swarm hub on a Kubernetes cluster with the `agentihooks-swarm` Helm chart: the ledger, Redis, the controller, the event hub and a bundled NATS server with JetStream. Every release publishes the chart as an OCI artifact on ghcr beside the images, with the chart version equal to the release version. Agents run on hives that join afterwards; see [Add a hive](add-hive-quick-start.md).

## Prerequisites

- A Kubernetes cluster and a `kubectl` context with rights to create a namespace, Deployments, StatefulSets, Services and Secrets.
- Helm 3.8 or later, which pulls OCI charts.
- A storage class for the Redis, ledger and JetStream volumes.
- A namespace for the release:

  ```bash
  kubectl create namespace agentihooks
  ```

## Install the hub

Write `values.yaml`:

```yaml
image:
  tag: release

hub:
  enabled: true

nats:
  enabled: true
  config:
    jetstream:
      enabled: true
```

Install it, naming the release tag you want without its `v` prefix, such as `2.4.1` for `v2.4.1`:

```bash
helm upgrade --install agentihooks-swarm \
  oci://ghcr.io/the-cloud-clockwork/charts/agentihooks-swarm \
  --version <version> --namespace agentihooks -f values.yaml
```

The repository's `deploy/helm/agentihooks-swarm/examples/` folder holds the same hub values as `hub.yaml`, a hub on a NATS outside the release as `external-nats.yaml`, and a worker hive with no hub as `hive-only.yaml`.

### Use an existing NATS

To run the hub on a NATS server you already operate, turn the bundled one off and point the hub bus at yours:

```yaml
hub:
  enabled: true
  bus:
    url: nats://nats.nats.svc.cluster.local:4222

nats:
  enabled: false
```

The server needs JetStream enabled for the hub's durable consumers.

### Use your own Redis

Without Redis settings the chart runs a bundled Redis inside the namespace. To point the swarm at a Redis you run, put its full connection URL, credential included, in a Secret and name it:

```yaml
redis:
  existingSecret:
    name: swarm-redis
    key: url
```

Create that Secret from your secret manager, for example through an ExternalSecret; never put the URL in values or a shell history. With `existingSecret` set the bundled Redis is not rendered. That URL reaches every swarm container; to give the ledger, the controller and the workers their own least privilege users, set `redis.roles.ledger`, `redis.roles.controller` and `redis.roles.worker` to their own Secrets, all three or none.

## Verify

```bash
kubectl rollout status statefulset/agentihooks-swarm-ledger -n agentihooks
kubectl rollout status deployment/agentihooks-swarm-controller -n agentihooks
kubectl rollout status deployment/agentihooks-swarm-hub -n agentihooks
helm test agentihooks-swarm --namespace agentihooks
```

Each rollout reports `successfully rolled out` and `helm test` runs the ledger health pod to `Succeeded`. Reach the ledger page locally:

```bash
kubectl -n agentihooks port-forward service/agentihooks-swarm-ledger 8765:8765
```

Then open `http://127.0.0.1:8765/`. Publish the ledger outside the cluster with the chart's `ingress` values or your own route.

## Enroll workers

The hub runs no agents. Each machine or worker pod that runs agents joins as a hive with one invite: issue it with `agentihooks hive invite`, then join a machine with `agentihooks hive join` or install a hive only release of this chart. Follow [Add a hive](add-hive-quick-start.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `helm upgrade` cannot pull the chart | Check the version exists on the release page; Helm older than 3.8 cannot pull OCI charts |
| Pods stay `Pending` | `kubectl -n agentihooks describe pod <pod>`; a missing storage class or node capacity holds the volumes or the pods |
| The ledger or controller cannot reach Redis | `kubectl -n agentihooks logs statefulset/agentihooks-swarm-ledger`; with `existingSecret`, the Secret name or key does not match, or the URL is unreachable from the namespace |
| The hub logs bus connection errors | `kubectl -n agentihooks logs deployment/agentihooks-swarm-hub`; with the bundled NATS check its pod is ready, with an external NATS check `hub.bus.url` |
| `helm test` fails | `kubectl -n agentihooks logs agentihooks-swarm-ledger-healthz`; the ledger did not answer its health route |

A release name without `agentihooks-swarm` in it prefixes every resource with `<release>-agentihooks-swarm`; a name that contains it is used as is.
