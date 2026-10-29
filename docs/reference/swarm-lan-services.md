---
title: Swarm LAN services
parent: Reference
nav_order: 49
---

# Swarm LAN services
{: .no_toc }

The swarm tick, ledger writes and remote workers depend on the LAN services in
`agentihooks-swarm`. On Anton, MetalLB supplies their shared address
`10.10.30.207`. MetalLB must be the only load balancer controller managing these
services. A running Postgres pod does not guarantee that clients can keep their
connections through that address.

1. TOC
{:toc}

---

## What failed on 20 October

The investigation in [antoncore issue 999](https://github.com/the-cloud-clockwork/antoncore/issues/999)
confirmed two interacting faults:

| Fault | Evidence | Effect |
|-------|----------|--------|
| k3s servicelb competed with MetalLB | Service managed fields named both `k3s` and `controller` as status writers; `service-lb-controller` events replaced the MetalLB address with node addresses | MetalLB speakers withdrew the address with `ipNotAllowed`, then announced it again |
| Speaker membership changed or split | `Marking k8s-worker-1 as failed` and `serviceWithdrawn` with `notOwner` aligned with failures | The L2 owner moved between nodes; connections through the previous owner were reset with `externalTrafficPolicy: Cluster` |

The k3s server on `k8s-master` had no disable flag and no main configuration
file, despite the provisioner and recorded configuration disabling servicelb.
Its controller had rewritten jump-lan status 1697 times since the server restart
on 14 October. The affected shared services included postgres-lan, redis-lan,
ledger-lan, jump-lan, worker-ssh-lan and hive-lan. Other LoadBalancer services
also showed the competing status writes.

The address of `k8s-worker-1` had been leased to a phone by the router. Memberlist
failures occurred at 11:09:17, 11:52:04, 12:22:41 and 12:23:38 UTC. During the
12:23:01 to 12:24:00 split, its speaker marked the other three nodes failed with
zero peer confirmations. The address conflict was fixed separately through
[antoncore pull request 997](https://github.com/the-cloud-clockwork/antoncore/pull/997)
and the router change. A node rejoin alone was the initial hypothesis, not the
established cause.

Status rewrites alone did not establish a connection outage: same-node
re-announcements at 12:43 and 12:54 produced no client errors. Diagnose the
speaker owner move and the client error together before attributing a new
reset to this incident. The evidence does not establish that every Redis
timeout or DNS failure has this cause.

## Configuration and activation

[Antoncore pull request 1000](https://github.com/the-cloud-clockwork/antoncore/pull/1000)
merged a `k3s-server-config` DaemonSet into `dev`. The `cluster-rbac` ArgoCD
application reconciles it. The DaemonSet selects control plane nodes, mounts
`/etc/rancher` from the host and writes this drop-in:

```yaml
disable+:
  - servicelb
```

The destination is
`/etc/rancher/k3s/config.yaml.d/40-disable-servicelb.yaml`. The `disable+` key
appends servicelb to the disabled components list. It preserves other disabled
components. The writer then sleeps; it does not restart k3s. Its log says
`wrote 40-disable-servicelb.yaml on <node>; applies on next k3s restart`.

Keep this invariant in the Git managed server configuration. Changing swarm
client URLs, database retry limits or the service's traffic policy does not
activate the delivered fix. No AgentiHooks environment variable controls this
k3s setting.

The delivery's activation procedure requires an operator lifecycle window:

1. Confirm the DaemonSet wrote the drop-in on `k8s-master`.
2. Restart the k3s server unit on that node so it reads the drop-in.
3. Remove leftover `svclb-*` DaemonSets from `kube-system` and the unmanaged
   `metallb-system/controller` Deployment identified in the investigation.
   Preserve the chart-managed `metallb-controller`.
4. Verify that only MetalLB writes service status and that no new
   `service-lb-controller` updates appear.

These are the delivered activation requirements, not proof that they have run.
The merged manifest does not perform the restart or cleanup. Configuration
changes belong in antoncore and follow its GitOps delivery; diagnostics below
are read only.

## Inspect service ownership

Use the cluster context for Anton. This prints addresses, traffic policy and
status field managers without reading credentials:

```bash
kubectl -n agentihooks-swarm get service agentihooks-swarm-postgres-lan \
  -o json --show-managed-fields | jq '{
    ingress: .status.loadBalancer.ingress,
    trafficPolicy: .spec.externalTrafficPolicy,
    statusWriters: [.metadata.managedFields[]? |
      select(.subresource == "status") | {manager, time}]
  }'
kubectl get events -A --field-selector reason=UpdatedLoadBalancer \
  --sort-by=.metadata.creationTimestamp
kubectl -n kube-system get daemonsets -o name | rg 'svclb-|k3s-server-config'
kubectl -n metallb-system get deployments
kubectl -n kube-system logs daemonset/k3s-server-config --tail=20
```

The intended address is `10.10.30.207`; a node address list in its place warrants
checking for competing controllers. The intended current status writer is
MetalLB's `controller`. Managed fields show recorded ownership and its update
time, not a complete history. Kubernetes events also expire: an old count or
an empty event list cannot prove a clean after window.

Read speakers across nodes for the same named time window. List pods first,
then substitute an actual speaker pod name:

```bash
kubectl -n metallb-system get pods -o wide
SPEAKER_POD='replace-with-speaker-pod-name'
WINDOW_START='2026-10-20T10:00:00Z'
kubectl -n metallb-system logs "$SPEAKER_POD" --all-containers=true \
  --since-time="$WINDOW_START" --timestamps \
  | rg 'ipNotAllowed|notOwner|serviceWithdrawn|Marking.*failed|peer confirmations'
```

Repeat for each speaker and retain both the node and the timestamps. For a
restarted speaker, `kubectl logs --previous` can recover only its previous
container log if it is still retained. Inspect the controller pods listed
above for the same window. On the k3s server host, the server journal identifies
`service-lb-controller` activity:

```bash
sudo journalctl -u k3s \
  --since '2026-10-20 10:00:00 UTC' --until '2026-10-20 13:10:00 UTC' \
  --no-pager | rg 'service-lb-controller|LoadBalancer|servicelb'
```

Run host journal commands on the named host. Missing retained logs leave an
evidence gap; a current healthy address cannot reconstruct the incident.

## Count affected client operations

For a comparable before and after check, use equal named windows and retain
speaker ownership changes, service updates and client failures separately.
On the workstation that ran the tick:

```bash
journalctl --user -u agentihooks-swarm \
  --since '2026-10-20 08:59:00 UTC' --until '2026-10-20 13:10:00 UTC' \
  --no-pager | rg -c 'DatabaseError.*server closed the connection unexpectedly'
kubectl -n agentihooks-swarm logs agentihooks-swarm-postgres-0 \
  --since-time='2026-10-20T08:59:00Z' --timestamps \
  | rg 'idle-in-transaction timeout|connection to client lost|could not accept SSL'
```

The journal command counts matching lines; `rg -c` exits with status one and
prints no count when there are none. The pod command has no end bound: keep
only lines through the window end before comparing counts.

Issue 999 records the following tick error lines in that incident window:

| UTC minute | DatabaseError lines |
|------------|---------------------|
| 10:38 | 4 |
| 10:39 | 1 |
| 10:40 | 2 |
| 11:09 | 1 |
| 11:10 | 2 |
| 11:53 | 5 |
| 12:22 | 1 |
| 12:23 | 2 |
| 12:25 | 3 |
| 12:26 | 2 |
| Total | 23 |

The Postgres record also contained 14 idle-in-transaction terminations in the
affected minutes, five `connection to client lost` lines at 11:53:02, and SSL
accept resets at 12:23:19 and 12:24:26. These are separate observations of the
outage; adding them does not yield a count of unique broken connections.
Postgres remained running. No client errors accompanied the same-node
re-announcements at 12:43 and 12:54.

## Troubleshooting

| Observation | Check or action |
|-------------|-----------------|
| Drop-in writer reports success but node addresses still appear | Confirm k3s has restarted since the write; writing the file alone does not disable the running controller |
| `svclb-*` DaemonSets remain after activation | Check the server startup configuration and the delivery's cleanup requirement |
| `ipNotAllowed` withdrawals with both status writers | Investigate competing servicelb and MetalLB status updates |
| `notOwner` withdrawals and client errors together | Compare speaker memberlist health, node address uniqueness and L2 ownership on every node |
| Database is running but every swarm loses its connection | Check the shared LAN path as well as [database session limits](shared-ledger-database.md); pod uptime alone does not distinguish them |
| More than one MetalLB controller Deployment | Identify chart ownership before cleanup; the incident found an unmanaged `controller` beside `metallb-controller` |
| Logs or events no longer cover the incident | Report the missing window and collect a new bounded window; do not infer historical owner changes from present status |

## Delivery evidence and limits

The delivered configuration was absent in the
[red CI baseline](https://github.com/the-cloud-clockwork/antoncore/actions/runs/39427452706/job/118643653849).
The final implementation passed
[green CI](https://github.com/the-cloud-clockwork/antoncore/actions/runs/39428209362/job/118646633136),
with 1330 tests passing as recorded in the delivery comment. Pull request 1000
merged on 20 October 2026. The current antoncore `dev` manifest still writes
`disable+: [servicelb]` without a restart command.

This proves the configuration delivery. The incident record does not provide
an equal post-activation window proving zero address rewrites or connection
resets. Confirm activation and measure it before claiming the running cluster
is repaired.
