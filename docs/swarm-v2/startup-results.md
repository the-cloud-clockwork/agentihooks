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

| Scenario | Container | Image | Pull duration |
|---|---|---|---|
| cold-node/uncached-image |  | docker:dind | 3.3 / 3.3 (n 15) |
| cold-node/uncached-image |  | ghcr.io/the-cloud-clockwork/arc-runner:dev | 0.8 / 23.5 (n 30) |
| cold-node/uncached-image |  | registry.k8s.io/pause:3.10 | 0.5 / 0.5 (n 1) |
| warm-node/cached-image |  | alpine/k8s:1.28.15 | 0.0 / 0.0 (n 18) |
| warm-node/cached-image |  | alpine/k8s:1.33.5 | 0.0 / 0.0 (n 12) |
| warm-node/cached-image |  | alpine:3.20 | 0.0 / 0.0 (n 2) |
| warm-node/cached-image |  | python:3.12-slim | 0.0 / 0.0 (n 2) |
| warm-node/partial-image |  | alpine:3.20 | 0.0 / 7.9 (n 14) |
| warm-node/partial-image |  | docker:dind | 0.0 / 0.0 (n 294) |
| warm-node/partial-image |  | ghcr.io/the-cloud-clockwork/arc-runner:dev | 0.0 / 1.8 (n 588) |
| warm-node/uncached-image |  | alpine:3.20 | 1.5 / 6.9 (n 10) |
| warm-node/uncached-image |  | busybox:1.36 | 1.5 / 1.5 (n 1) |
| warm-node/uncached-image |  | debian:12-slim | 3.0 / 3.2 (n 2) |
| warm-node/uncached-image |  | docker:dind | 3.3 / 3.5 (n 15) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-api:dev | 0.6 / 0.7 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-api:dev@sha256:324e0f6ed28036c1eb63b04b13dbfc8adc2f01349706cbb30a904aa074b8908d | 0.5 / 0.8 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:dev | 0.7 / 0.7 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:dev@sha256:7c7a4885f5e198b4d16cfb700c928e7903ed98d13739576b871d8bfcc2810261 | 0.7 / 1.4 (n 60) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-embeddings:dev | 0.7 / 0.8 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-embeddings:dev@sha256:293e8ee5ba0cfaa36cd35d9a1c93e07ebc57456e2733548844e023ae2022ea45 | 0.8 / 0.8 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-mcp:dev | 0.8 / 0.8 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-mcp:dev@sha256:268118064014b1a54b7258d7892a2bc18a318742fa41c196c42090651e2c8fa7 | 0.8 / 0.9 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev | 0.5 / 0.7 (n 6) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:51f4a1d1aff65fa66f83562d42ac9907c26745d7ab90767d1763798d9c2a0da3 | 0.5 / 3.5 (n 8) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:9e72aa84ae817d1d670eb98f29398a09962cbf99b493fa3d8b2ea73e1f4feb97 | 3.5 / 4.0 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:c65941df8cd164807c8465fd25e37bb6a945943e4660f1d3e369bbdd7d12bcee | 3.5 / 3.8 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:d4b336b9d3b2fa15e741ce0ebcf10cf79bc996b53947eb675185bb126e93d212 | 0.6 / 4.0 (n 6) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/antoncore-operator-ops-runner:dev | 1.8 / 4.1 (n 64) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/antoncore-operator:dev | 1.2 / 1.2 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/arc-runner:dev | 0.6 / 23.7 (n 104) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/qitp-worker:dev | 1.0 / 2.3 (n 233) |
| warm-node/uncached-image |  | public.ecr.aws/aws-ec2/aws-node-termination-handler:v1.25.6 | 1.8 / 2.0 (n 10) |
| warm-node/uncached-image |  | quay.io/kata-containers/kata-deploy:4.2.0 | 54.5 / 54.8 (n 2) |
| warm-node/uncached-image |  | quay.io/prometheus/node-exporter:v1.9.1 | 1.4 / 7.0 (n 10) |
| warm-node/uncached-image |  | registry.k8s.io/nfd/node-feature-discovery:v0.19.0 | 2.8 / 3.2 (n 2) |

## anton-read-2.json

Source: read only kubectl and aws read of anton-prod, the next hour of events. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cold-node/uncached-image | 2 | 2 | none | sandbox 1 | 29.9 / 150.6 (n 2) | 0.6 / 30.0 (n 2) | 5.4 / 11.7 (n 2) | 2.0 / 8.0 (n 2) | 22.0 / 56.0 (n 2) | 0.5 / 74.9 (n 2) | 0.6 / 30.0 (n 2) | 0.0 / 0.0 (n 2) | 5.5 / 65.5 (n 2) | 0.0 / 0.0 (n 1) | n/a |
| warm-node/cached-image | 32 | 32 | none | sandbox 2 | n/a | 0.0 / 0.0 (n 32) | n/a | n/a | n/a | 0.3 / 0.6 (n 5) | 0.0 / 0.0 (n 32) | 0.0 / 0.0 (n 32) | 0.7 / 14.4 (n 32) | 0.0 / 1.0 (n 2) | n/a |
| warm-node/partial-image | 96 | 96 | none | sandbox 3 | n/a | 0.6 / 3.7 (n 96) | n/a | n/a | n/a | 1.4 / 94.1 (n 11) | 0.6 / 3.7 (n 96) | 0.0 / 0.0 (n 96) | 16.0 / 40.9 (n 96) | 1.0 / 1.0 (n 11) | n/a |
| warm-node/uncached-image | 465 | 465 | none | sandbox 28 | n/a | 1.0 / 18.7 (n 465) | n/a | n/a | n/a | 0.3 / 21.8 (n 130) | 1.0 / 18.7 (n 465) | 0.0 / 0.0 (n 465) | 0.3 / 14.4 (n 465) | 1.0 / 114.0 (n 37) | n/a |
| warm-node/unknown-image | 3 | 0 | image_pull 1, sandbox 1, unschedulable 1 | none | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

| Scenario | Container | Image | Pull duration |
|---|---|---|---|
| cold-node/uncached-image |  | docker:dind | 3.4 / 3.4 (n 1) |
| cold-node/uncached-image |  | ghcr.io/the-cloud-clockwork/arc-runner:dev | 0.8 / 25.9 (n 2) |
| cold-node/uncached-image |  | registry.k8s.io/pause:3.10 | 0.6 / 0.6 (n 1) |
| warm-node/cached-image |  | alpine/k8s:1.28.15 | 0.0 / 0.0 (n 18) |
| warm-node/cached-image |  | alpine/k8s:1.33.5 | 0.0 / 0.0 (n 12) |
| warm-node/cached-image |  | alpine:3.20 | 0.0 / 0.0 (n 3) |
| warm-node/partial-image |  | alpine:3.20 | 0.0 / 1.5 (n 6) |
| warm-node/partial-image |  | docker:dind | 0.0 / 0.0 (n 93) |
| warm-node/partial-image |  | ghcr.io/the-cloud-clockwork/arc-runner:dev | 0.0 / 3.2 (n 186) |
| warm-node/uncached-image |  | alpine:3.20 | 1.5 / 6.2 (n 9) |
| warm-node/uncached-image |  | debian:12-slim | 3.1 / 3.7 (n 6) |
| warm-node/uncached-image |  | docker:dind | 3.4 / 3.4 (n 13) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-api:dev | 0.9 / 0.9 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-api:dev@sha256:324e0f6ed28036c1eb63b04b13dbfc8adc2f01349706cbb30a904aa074b8908d | 0.8 / 0.8 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:dev | 0.5 / 0.5 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:dev@sha256:7c7a4885f5e198b4d16cfb700c928e7903ed98d13739576b871d8bfcc2810261 | 0.7 / 1.0 (n 60) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-embeddings:dev | 0.8 / 0.8 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-embeddings:dev@sha256:293e8ee5ba0cfaa36cd35d9a1c93e07ebc57456e2733548844e023ae2022ea45 | 2.7 / 2.7 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-mcp:dev | 0.8 / 0.8 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-mcp:dev@sha256:268118064014b1a54b7258d7892a2bc18a318742fa41c196c42090651e2c8fa7 | 0.8 / 0.8 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev | 0.6 / 0.6 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:1e13c88c05a542532e7136feb68663bed522f32df2804115f511bce7caae4cea | 3.1 / 3.5 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:28c628c99faf039c9afc81d0784a28b0f3aa78eadc26a55c1d33eae3bc95687b | 3.3 / 3.3 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:3b25f2fb79ecb41085308128bd604eae9ebf1d46b1314e7e6151703531d44fe8 | 3.0 / 4.1 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:6a689b04ce984a17e2d268a3229e39327bd9df0fb9c2405af5b41ca62573521f | 3.1 / 3.6 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:82b69361122e96d654c1fcdb603d1f62b06ad8e647fc9c062f44b83046f11c95 | 1.1 / 1.6 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:91760e805168097fec5fb41cc2bcbb0386e2445d62d5bfb5d40de659a25ac2ca | 2.2 / 2.9 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:9e72aa84ae817d1d670eb98f29398a09962cbf99b493fa3d8b2ea73e1f4feb97 | 0.9 / 4.2 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:bc16d11802f168edca5cf84d1a7b9d44ff2f11a15f2f20dc64a72f4a61949bc0 | 3.2 / 4.3 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/antoncore-operator-ops-runner:dev | 1.7 / 2.6 (n 64) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/antoncore-operator:dev | 1.0 / 8.1 (n 2) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/arc-runner:dev | 1.1 / 25.9 (n 61) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/qitp-worker:dev | 1.0 / 2.0 (n 233) |
| warm-node/uncached-image |  | public.ecr.aws/aws-ec2/aws-node-termination-handler:v1.25.6 | 1.8 / 1.9 (n 10) |
| warm-node/uncached-image |  | quay.io/kata-containers/kata-deploy:4.2.0 | 36.4 / 84.8 (n 6) |
| warm-node/uncached-image |  | quay.io/prometheus/node-exporter:v1.9.1 | 1.3 / 1.4 (n 10) |
| warm-node/uncached-image |  | registry.k8s.io/nfd/node-feature-discovery:v0.19.0 | 2.9 / 3.3 (n 6) |
| warm-node/uncached-image |  | registry.k8s.io/pause:3.10 | 0.6 / 2.1 (n 2) |

## kind-run-1.json

Source: Startup stages run 38150380291 at ec5276308ced2ea1c182dbce76c6e81a239a9d48, both nodes behind a 90 second registry outage. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm-node/cached-image | 11 | 11 | none | none | n/a | 0.0 / 0.0 (n 11) | n/a | n/a | n/a | 0.0 / 11.0 (n 11) | 0.0 / 0.0 (n 11) | 0.0 / 0.0 (n 11) | 2.0 / 4.0 (n 11) | 0.0 / 1.0 (n 11) | n/a |
| warm-node/uncached-image | 4 | 4 | none | registry_unavailable 2 | n/a | 11.1 / 180.0 (n 4) | n/a | n/a | n/a | 0.0 / 3.0 (n 4) | 10.1 / 11.1 (n 4) | 0.0 / 169.8 (n 4) | 0.0 / 0.9 (n 4) | 1.0 / 2.0 (n 4) | n/a |

| Scenario | Container | Image | Pull duration |
|---|---|---|---|
| warm-node/cached-image |  | docker.io/kindest/kindnetd:v20260820-69b56db7 | 0.0 / 0.0 (n 3) |
| warm-node/cached-image |  | docker.io/kindest/local-path-provisioner:v20260820-69b56db7 | 0.0 / 0.0 (n 1) |
| warm-node/cached-image |  | kind-registry:5000/agentihooks-worker:startup | 0.0 / 0.0 (n 2) |
| warm-node/cached-image |  | registry.k8s.io/coredns/coredns:v1.14.6 | 0.0 / 0.0 (n 2) |
| warm-node/cached-image |  | registry.k8s.io/kube-proxy:v1.37.0 | 0.0 / 0.0 (n 3) |
| warm-node/uncached-image |  | kind-registry:5000/agentihooks-worker:startup | 10.1 / 11.1 (n 4) |

## kind-run-2.json

Source: Startup stages run 38153743721 at f49241dc520bf8eeb84cb0816a2880e43a85e787, both nodes behind a 90 second registry outage. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm-node/cached-image | 11 | 11 | none | none | n/a | 0.0 / 0.0 (n 11) | n/a | n/a | n/a | 0.0 / 10.0 (n 11) | 0.0 / 0.0 (n 11) | 0.0 / 0.0 (n 11) | 2.0 / 3.0 (n 11) | 0.0 / 1.0 (n 11) | n/a |
| warm-node/uncached-image | 4 | 4 | none | registry_unavailable 2 | n/a | 11.4 / 186.0 (n 4) | n/a | n/a | n/a | 0.0 / 1.0 (n 4) | 11.4 / 12.6 (n 4) | 0.0 / 173.4 (n 4) | 0.7 / 2.0 (n 4) | 0.0 / 1.0 (n 4) | n/a |

| Scenario | Container | Image | Pull duration |
|---|---|---|---|
| warm-node/cached-image |  | docker.io/kindest/kindnetd:v20260820-69b56db7 | 0.0 / 0.0 (n 3) |
| warm-node/cached-image |  | docker.io/kindest/local-path-provisioner:v20260820-69b56db7 | 0.0 / 0.0 (n 1) |
| warm-node/cached-image |  | kind-registry:5000/agentihooks-worker:startup | 0.0 / 0.0 (n 2) |
| warm-node/cached-image |  | registry.k8s.io/coredns/coredns:v1.14.6 | 0.0 / 0.0 (n 2) |
| warm-node/cached-image |  | registry.k8s.io/kube-proxy:v1.37.0 | 0.0 / 0.0 (n 3) |
| warm-node/uncached-image |  | kind-registry:5000/agentihooks-worker:startup | 11.4 / 12.6 (n 4) |

## anton-read-3.json

Source: read only kubectl and aws read of anton-prod at 2026-10-12T10:17:25Z with the agent log of every swarm worker Pod, one launched by the controller at 10:03:14Z. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| warm-node/cached-image | 32 | 32 | none | none | n/a | 0.0 / 0.0 (n 32) | n/a | n/a | n/a | 0.4 / 0.5 (n 5) | 0.0 / 0.0 (n 32) | 0.0 / 0.0 (n 32) | 0.7 / 1.7 (n 32) | n/a | n/a |
| warm-node/partial-image | 142 | 142 | none | none | n/a | 0.6 / 2.9 (n 142) | n/a | n/a | n/a | 0.5 / 0.9 (n 7) | 0.6 / 2.9 (n 142) | 0.0 / 0.0 (n 142) | 15.7 / 39.6 (n 142) | 1.0 / 1.0 (n 6) | n/a |
| warm-node/uncached-image | 445 | 445 | none | none | n/a | 1.0 / 5.4 (n 445) | n/a | n/a | n/a | 0.4 / 0.9 (n 81) | 1.0 / 5.4 (n 445) | 0.0 / 0.0 (n 445) | 0.4 / 2.9 (n 445) | 6.0 / 17.0 (n 3) | 1.1 / 1.1 (n 1) |
| warm-node/unknown-image | 42 | 0 | registry_unauthorized 1, unschedulable 41 | none | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

| Scenario | Container | Image | Pull duration |
|---|---|---|---|
| warm-node/cached-image |  | alpine/k8s:1.28.15 | 0.0 / 0.0 (n 18) |
| warm-node/cached-image |  | alpine/k8s:1.33.5 | 0.0 / 0.0 (n 12) |
| warm-node/cached-image |  | python:3.12-slim | 0.0 / 0.0 (n 2) |
| warm-node/partial-image |  | docker:dind | 0.0 / 0.0 (n 142) |
| warm-node/partial-image |  | ghcr.io/the-cloud-clockwork/arc-runner:dev | 0.0 / 2.3 (n 284) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-api:dev | 0.9 / 1.2 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-api:dev@sha256:324e0f6ed28036c1eb63b04b13dbfc8adc2f01349706cbb30a904aa074b8908d | 0.9 / 0.9 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:dev | 1.0 / 1.2 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-brain-ops:dev@sha256:7c7a4885f5e198b4d16cfb700c928e7903ed98d13739576b871d8bfcc2810261 | 0.8 / 2.2 (n 59) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-embeddings:dev | 0.9 / 0.9 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-embeddings:dev@sha256:293e8ee5ba0cfaa36cd35d9a1c93e07ebc57456e2733548844e023ae2022ea45 | 0.9 / 1.1 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-mcp:dev | 0.6 / 1.0 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentibrain-mcp:dev@sha256:268118064014b1a54b7258d7892a2bc18a318742fa41c196c42090651e2c8fa7 | 1.0 / 5.4 (n 3) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-bundle:dev | 2.0 / 2.0 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev | 0.8 / 2.5 (n 18) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:25ce18729f7e4b811b75469e5b6516786b8c74ce67bb35d849e0c36a8960c67d | 0.8 / 7.6 (n 21) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:4b8a2c9db0c5b3b44a0007912e7ba6c71023af72d5ecadb0f790f091af8754b7 | 1.5 / 4.6 (n 6) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:8b489bf27bd4dbd1e9e28d47c08808f548548592354e91f7e1d5ff42a9976536 | 0.9 / 6.6 (n 13) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:9288601c0d3fd294257e7df6afb8dc871fadc8b592916171f501a4398c172406 | 1.9 / 5.3 (n 6) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-swarm:dev@sha256:c63cb71fef0a882e5f004df6d5717f478ed140b4d09027ce35d260ff6486618d | 4.2 / 4.7 (n 6) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-worker:dev | 21.2 / 21.2 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/agentihooks-worker@sha256:8a354562044754e32d66a18072a78fde7d81f107b8288ed819f87bdeb88317fd | 19.2 / 19.2 (n 1) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/antoncore-operator-ops-runner:dev | 1.4 / 2.9 (n 64) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/antoncore-operator:dev | 1.2 / 7.3 (n 4) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/arc-runner:dev | 0.6 / 4.3 (n 47) |
| warm-node/uncached-image |  | ghcr.io/the-cloud-clockwork/qitp-worker:dev | 1.0 / 3.1 (n 242) |

## anton-outage-1.json

Source: startup fixture anton-outage mode at 1dd43842b6bc51122e6bf19cb539d6fc619dc14a against anton-prod, read at 2026-10-16T20:49:20Z: one worker Pod of the agentihooks-worker:dev image scaled the empty Oracle research pool, which has no kata runtime, from zero and pulled through a registry that started 96 seconds after the Pod was scheduled. Cells are p50 / p95 in seconds with the sample count.

| Scenario | Samples | Started | Failures | Transient failures | Node provision to schedulable | Image pull with retries | Unschedulable to scale up | Scale up to EC2 launch | EC2 launch to node Ready | Node Ready or Pod creation to Scheduled | Image pull | Failed pulls before the pull that succeeded | Scheduled to last container start, less pulls and retries | Last container start to Pod Ready | Worker prepared to native agent ready and prompt delivered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cold-node/uncached-image | 1 | 1 | none | registry_unavailable 1 | 225.7 / 225.7 (n 1) | 112.0 / 112.0 (n 1) | 6.0 / 6.0 (n 1) | 21.3 / 21.3 (n 1) | 197.7 / 197.7 (n 1) | 0.8 / 0.8 (n 1) | 23.5 / 23.5 (n 1) | 88.5 / 88.5 (n 1) | 14.2 / 14.2 (n 1) | 1.0 / 1.0 (n 1) | n/a |

| Scenario | Container | Image | Pull duration |
|---|---|---|---|
| cold-node/uncached-image |  | 127.0.0.1:5000/the-cloud-clockwork/agentihooks-worker:dev | 23.5 / 23.5 (n 1) |

## Limits of this measurement

- Node provision runs from the Pod waiting to the new node accepting it: trigger, instance, join and schedule. Image pull with retries is the successful pull plus the time lost to failed pulls before it. Pod start (sandbox, containers, init containers) and bootstrap are separate columns. No column sums them into one launch figure.
- Only samples from a scale up of an empty pool onto a new instance that pulled uncached may be labelled cold scale from zero; the ondemand scale ups in these reads started from a non empty pool and are refused that label.
- Kubernetes keeps events for about an hour, so each Anton read covers the hour before it; Pods whose events expired are absent rather than counted as fast.
- EC2 launch times have one second precision and the join stage ends at the node's Ready transition.
- Bootstrap is the worker image's own report followed by a readiness file in the kind fixture. The native handshake runs from the worker's prepared report to the supervisor's prompt delivery once the native agent reported ready, read from the worker log of a controller launched Pod; it falls inside bootstrap and is not added to it.
- The kind fixture pulls from a registry on the same runner, so its pull times show image size and unpack cost, not registry distance; the Anton reads show the production registry path.
- With few cold samples the p95 equals the maximum.
- The cold node outage read runs on the Oracle research pool, which shares the swarm pool's shape, image and network but not its kata runtime: the swarm pool keeps two nodes by the operator's choice and cannot scale from zero. Its scale up time is the cluster autoscaler's ScaledUpGroup event and its launch is the instance's creation, so the instance column is scale up to Oracle instance creation and the join column runs to the node's Ready transition. The registry is a pull through cache of ghcr.io on the new node's loopback that started 96 seconds after the worker was scheduled, so its pull shows the production registry path after the outage; the registry Pod itself is not a sample. The fixture's outage_seconds of 90 is the wait it targets from seeing the worker scheduled; 96 seconds is measured from the worker's Scheduled event to the registry's Started event. The worker image was the :dev tag, not :release.
