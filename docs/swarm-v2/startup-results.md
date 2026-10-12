# Swarm v2 startup stages

## anton-read-1.json

Source: read only kubectl and aws read of anton-prod at 2026-10-11T14:57:15Z. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cold-node/uncached-image | 16 | 16 | none | sandbox 15 | 55.4 / 163.1 (n 16) | 24.5 / 31.4 (n 16) | 8.3 / 26.6 (n 16) | 10.0 / 12.0 (n 16) | 36.0 / 46.0 (n 16) | 1.1 / 95.9 (n 16) | 24.5 / 31.4 (n 16) | 0.0 / 0.0 (n 16) | 62.4 / 70.5 (n 16) | n/a | n/a |
| warm-node/cached-image | 33 | 33 | none | sandbox 1 | n/a | 0.0 / 0.0 (n 33) | n/a | n/a | n/a | 0.4 / 0.6 (n 7) | 0.0 / 0.0 (n 33) | 0.0 / 0.0 (n 33) | 0.7 / 1.8 (n 33) | 1.0 / 1.0 (n 1) | n/a |
| warm-node/partial-image | 301 | 301 | none | sandbox 7 | n/a | 0.6 / 2.7 (n 301) | n/a | n/a | n/a | 0.6 / 1.1 (n 11) | 0.6 / 2.7 (n 301) | 0.0 / 0.0 (n 301) | 15.0 / 45.3 (n 301) | 0.0 / 1.0 (n 9) | n/a |
| warm-node/uncached-image | 495 | 495 | none | sandbox 26 | n/a | 1.0 / 6.6 (n 495) | n/a | n/a | n/a | 0.4 / 1.0 (n 109) | 1.0 / 6.6 (n 495) | 0.0 / 0.0 (n 495) | 0.4 / 13.7 (n 495) | 1.0 / 93.0 (n 28) | n/a |
| warm-node/unknown-image | 1 | 0 | image_pull 1 | none | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## anton-read-2.json

Source: read only kubectl and aws read of anton-prod, the next hour of events. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cold-node/uncached-image | 2 | 2 | none | sandbox 1 | 29.9 / 150.6 (n 2) | 0.6 / 30.0 (n 2) | 5.4 / 11.7 (n 2) | 2.0 / 8.0 (n 2) | 22.0 / 56.0 (n 2) | 0.5 / 74.9 (n 2) | 0.6 / 30.0 (n 2) | 0.0 / 0.0 (n 2) | 5.5 / 65.5 (n 2) | 0.0 / 0.0 (n 1) | n/a |
| warm-node/cached-image | 32 | 32 | none | sandbox 2 | n/a | 0.0 / 0.0 (n 32) | n/a | n/a | n/a | 0.3 / 0.6 (n 5) | 0.0 / 0.0 (n 32) | 0.0 / 0.0 (n 32) | 0.7 / 14.4 (n 32) | 0.0 / 1.0 (n 2) | n/a |
| warm-node/partial-image | 96 | 96 | none | sandbox 3 | n/a | 0.6 / 3.7 (n 96) | n/a | n/a | n/a | 1.4 / 94.1 (n 11) | 0.6 / 3.7 (n 96) | 0.0 / 0.0 (n 96) | 16.0 / 40.9 (n 96) | 1.0 / 1.0 (n 11) | n/a |
| warm-node/uncached-image | 465 | 465 | none | sandbox 28 | n/a | 1.0 / 18.7 (n 465) | n/a | n/a | n/a | 0.3 / 21.8 (n 130) | 1.0 / 18.7 (n 465) | 0.0 / 0.0 (n 465) | 0.3 / 14.4 (n 465) | 1.0 / 114.0 (n 37) | n/a |
| warm-node/unknown-image | 3 | 0 | image_pull 1, sandbox 1, unschedulable 1 | none | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## kind-run-1.json

Source: Startup stages run 38150380291 at ec5276308ced2ea1c182dbce76c6e81a239a9d48, both nodes behind a 90 second registry outage. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm-node/cached-image | 11 | 11 | none | none | n/a | 0.0 / 0.0 (n 11) | n/a | n/a | n/a | 0.0 / 11.0 (n 11) | 0.0 / 0.0 (n 11) | 0.0 / 0.0 (n 11) | 2.0 / 4.0 (n 11) | 0.0 / 1.0 (n 11) | n/a |
| warm-node/uncached-image | 4 | 4 | none | registry_unavailable 2 | n/a | 11.1 / 180.0 (n 4) | n/a | n/a | n/a | 0.0 / 3.0 (n 4) | 10.1 / 11.1 (n 4) | 0.0 / 169.8 (n 4) | 0.0 / 0.9 (n 4) | 1.0 / 2.0 (n 4) | n/a |

## kind-run-2.json

Source: Startup stages run 38153743721 at f49241dc520bf8eeb84cb0816a2880e43a85e787, both nodes behind a 90 second registry outage. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm-node/cached-image | 11 | 11 | none | none | n/a | 0.0 / 0.0 (n 11) | n/a | n/a | n/a | 0.0 / 10.0 (n 11) | 0.0 / 0.0 (n 11) | 0.0 / 0.0 (n 11) | 2.0 / 3.0 (n 11) | 0.0 / 1.0 (n 11) | n/a |
| warm-node/uncached-image | 4 | 4 | none | registry_unavailable 2 | n/a | 11.4 / 186.0 (n 4) | n/a | n/a | n/a | 0.0 / 1.0 (n 4) | 11.4 / 12.6 (n 4) | 0.0 / 173.4 (n 4) | 0.7 / 2.0 (n 4) | 0.0 / 1.0 (n 4) | n/a |

## anton-read-3.json

Source: read only kubectl and aws read of anton-prod at 2026-10-12T10:17:25Z with the agent log of every swarm worker Pod, one launched by the controller at 10:03:14Z. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm-node/cached-image | 32 | 32 | none | none | n/a | 0.0 / 0.0 (n 32) | n/a | n/a | n/a | 0.4 / 0.5 (n 5) | 0.0 / 0.0 (n 32) | 0.0 / 0.0 (n 32) | 0.7 / 1.7 (n 32) | n/a | n/a |
| warm-node/partial-image | 142 | 142 | none | none | n/a | 0.6 / 2.9 (n 142) | n/a | n/a | n/a | 0.5 / 0.9 (n 7) | 0.6 / 2.9 (n 142) | 0.0 / 0.0 (n 142) | 15.7 / 39.6 (n 142) | 1.0 / 1.0 (n 6) | n/a |
| warm-node/uncached-image | 445 | 445 | none | none | n/a | 1.0 / 5.4 (n 445) | n/a | n/a | n/a | 0.4 / 0.9 (n 81) | 1.0 / 5.4 (n 445) | 0.0 / 0.0 (n 445) | 0.4 / 2.9 (n 445) | 6.0 / 17.0 (n 3) | 1.1 / 1.1 (n 1) |
| warm-node/unknown-image | 42 | 0 | registry_unauthorized 1, unschedulable 41 | none | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## Limits of this measurement

- Node provision runs from the Pod waiting to the new node accepting it: trigger, instance, join and schedule. Image pull with retries is the successful pull plus the time lost to failed pulls before it. Pod start (sandbox, containers, init containers) and bootstrap are separate columns. No column sums them into one launch figure.
- Only samples from a scale up of an empty pool onto a new instance that pulled uncached may be labelled cold scale from zero; the ondemand scale ups in these reads started from a non empty pool and are refused that label.
- Kubernetes keeps events for about an hour, so each Anton read covers the hour before it; Pods whose events expired are absent rather than counted as fast.
- EC2 launch times have one second precision and the join stage ends at the node's Ready transition.
- Bootstrap is the worker image's own report followed by a readiness file in the kind fixture. The native handshake runs from the worker's prepared report to the supervisor's prompt delivery once the native agent reported ready, read from the worker log of a controller launched Pod; it falls inside bootstrap and is not added to it.
- The kind fixture pulls from a registry on the same runner, so its pull times show image size and unpack cost, not registry distance; the Anton reads show the production registry path.
- With few cold samples the p95 equals the maximum.
