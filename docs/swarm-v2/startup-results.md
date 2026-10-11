# Swarm v2 startup stages

## anton-read-1.json

Source: read only kubectl and aws read of anton-prod at 2026-10-11T14:57:15Z. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Node provision | Image init | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Scheduled to last container start, less the pull | Last container start to Pod Ready |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cold-node/uncached-image | 16 | 16 | none | 54.3 / 73.6 (n 16) | 87.8 / 89.8 (n 16) | 8.3 / 26.6 (n 16) | 10.0 / 12.0 (n 16) | 36.0 / 46.0 (n 16) | 1.1 / 95.9 (n 16) | 24.5 / 31.4 (n 16) | 62.4 / 70.5 (n 16) | n/a |
| warm-node/cached-image | 33 | 33 | none | n/a | 0.7 / 1.8 (n 33) | n/a | n/a | n/a | 0.4 / 0.6 (n 7) | 0.0 / 0.0 (n 33) | 0.7 / 1.8 (n 33) | 1.0 / 1.0 (n 1) |
| warm-node/partial-image | 301 | 301 | none | n/a | 15.7 / 46.0 (n 301) | n/a | n/a | n/a | 0.6 / 1.1 (n 11) | 0.6 / 2.7 (n 301) | 15.0 / 45.3 (n 301) | 0.0 / 1.0 (n 9) |
| warm-node/uncached-image | 495 | 495 | none | n/a | 1.6 / 19.9 (n 495) | n/a | n/a | n/a | 0.4 / 1.0 (n 109) | 1.0 / 6.6 (n 495) | 0.4 / 13.7 (n 495) | 1.0 / 93.0 (n 28) |
| warm-node/unknown-image | 1 | 0 | image_pull 1 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## anton-read-2.json

Source: read only kubectl and aws read of anton-prod, the next hour of events. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Node provision | Image init | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Scheduled to last container start, less the pull | Last container start to Pod Ready |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cold-node/uncached-image | 2 | 2 | none | 29.4 / 75.7 (n 2) | 6.1 / 95.5 (n 2) | 5.4 / 11.7 (n 2) | 2.0 / 8.0 (n 2) | 22.0 / 56.0 (n 2) | 0.5 / 74.9 (n 2) | 0.6 / 30.0 (n 2) | 5.5 / 65.5 (n 2) | 0.0 / 0.0 (n 1) |
| warm-node/cached-image | 32 | 32 | none | n/a | 0.7 / 14.4 (n 32) | n/a | n/a | n/a | 0.3 / 0.6 (n 5) | 0.0 / 0.0 (n 32) | 0.7 / 14.4 (n 32) | 0.0 / 1.0 (n 2) |
| warm-node/partial-image | 96 | 96 | none | n/a | 18.2 / 43.0 (n 96) | n/a | n/a | n/a | 1.4 / 94.1 (n 11) | 0.6 / 3.7 (n 96) | 16.0 / 40.9 (n 96) | 1.0 / 1.0 (n 11) |
| warm-node/uncached-image | 465 | 465 | none | n/a | 1.6 / 37.2 (n 465) | n/a | n/a | n/a | 0.3 / 21.8 (n 130) | 1.0 / 18.7 (n 465) | 0.3 / 14.4 (n 465) | 1.0 / 114.0 (n 37) |
| warm-node/unknown-image | 3 | 0 | image_pull 1, sandbox 1, unschedulable 1 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## kind-run-1.json

Source: Startup stages run 38150051995 at 570264583, one node interrupted briefly. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Node provision | Image init | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Scheduled to last container start, less the pull | Last container start to Pod Ready |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm-node/cached-image | 11 | 11 | none | n/a | 1.0 / 3.0 (n 11) | n/a | n/a | n/a | 0.0 / 11.0 (n 11) | 0.0 / 0.0 (n 11) | 1.0 / 3.0 (n 11) | 0.0 / 2.0 (n 11) |
| warm-node/uncached-image | 3 | 3 | none | n/a | 11.0 / 27.0 (n 3) | n/a | n/a | n/a | 0.0 / 4.0 (n 3) | 10.1 / 10.5 (n 3) | 0.5 / 17.0 (n 3) | 1.0 / 2.0 (n 3) |

## kind-run-2.json

Source: Startup stages run 38150380291 at ec5276308, both nodes behind a 90 second registry outage. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Node provision | Image init | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Scheduled to last container start, less the pull | Last container start to Pod Ready |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm-node/cached-image | 11 | 11 | none | n/a | 2.0 / 4.0 (n 11) | n/a | n/a | n/a | 0.0 / 11.0 (n 11) | 0.0 / 0.0 (n 11) | 2.0 / 4.0 (n 11) | 0.0 / 1.0 (n 11) |
| warm-node/uncached-image | 4 | 4 | none | n/a | 12.0 / 180.0 (n 4) | n/a | n/a | n/a | 0.0 / 3.0 (n 4) | 10.1 / 11.1 (n 4) | 0.9 / 169.8 (n 4) | 1.0 / 2.0 (n 4) |

## Limits of this measurement

- Node provision is trigger plus instance plus join; image initialization is the pull plus the Pod start less the pull. They are reported apart and never summed into one launch figure.
- Only samples from a scale up of an empty pool onto a new instance that pulled uncached may be labelled cold scale from zero; the ondemand scale ups in these reads started from a non empty pool and are refused that label.
- Kubernetes keeps events for about an hour, so each Anton read covers the hour before it; Pods whose events expired are absent rather than counted as fast.
- EC2 launch times have one second precision and the join stage ends at the node's Ready transition.
- Bootstrap is the worker image's own report followed by a readiness file in the kind fixture; the native harness handshake needs a controller grant and provider credentials and is not measured here.
- The kind fixture pulls from a registry on the same runner, so its pull times show image size and unpack cost, not registry distance; the Anton reads show the production registry path.
- With few cold samples the p95 equals the maximum.
