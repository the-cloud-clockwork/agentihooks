---
title: CI lab node cache
parent: Reference
nav_order: 91
---

# CI lab node cache

The lab chart runs `ci-lab-node-cache`, a DaemonSet that keeps a bare agentihooks mirror
and a uv dependency cache on each eligible CI node. Its `prepare` init container pulls the
worker image and creates the cache directories; the `warm` container refreshes them. The
host directory survives pod replacement on that node. A new or replaced node starts with
its own cache; this is neither a shared volume nor a backup.

This delivery prepares the caches and records the last successful fetch. The
[worker Deployment](ci-lab.md#worker-deployment) mounts `git` and `uv` read only, and its pod
store borrows the node mirror's objects, so a worker reports a git hit for a head the node mirror
holds without fetching it. No code sets the scheduler's `ci_lab_mirror_age_seconds` yet. The
[metric contract](ci-lab.md#metrics) names that series; the DaemonSet itself exposes no metrics
endpoint.

## Placement and security

The chart fixes two node selector values: `node-pool=oci-ci` and `workload=ci`, under the
deployment's label domain. Both must match. It tolerates the deployment's `remote=oci` and
`workload=ci` taints with `operator: Equal` and `effect: NoSchedule`. These selectors and
tolerations are template settings, not Helm values. Inspect their exact keys in the
rendered chart or running DaemonSet:

```bash
kubectl -n ci-lab get ds ci-lab-node-cache \
  -o jsonpath='{.spec.template.spec.nodeSelector}{"\n"}{.spec.template.spec.tolerations}{"\n"}'
```

The warmer runs as UID and GID 1001 by default, with `runAsNonRoot`, `RuntimeDefault`
seccomp, all capabilities dropped, no privilege escalation and a read only root
filesystem. Service account token mounting and service links are disabled. It writes only
to the cache mount and an `emptyDir` at `/tmp`; its `HOME` is `/tmp`.

The `prepare` init container overrides UID and GID to root and keeps only `CHOWN` so it can
create `/cache/git` and `/cache/uv` and hand those directories to the configured UID. It
also has a read only root filesystem and no privilege escalation. Both containers use the
configured worker image with `imagePullPolicy: Always`, and both use the node cache resource
settings. Pulling the image during init prepares the node's image cache; it does not make
an independently scheduled worker wait for the image pull or for cache warming.

## Configuration

The deployment repository owns `k8s/charts/ci-lab`. Change its values through the deployment
pull request and CI process. The DaemonSet is included in the chart without a separate
enable flag.

| Helm value | Shipped setting | Effect |
|---|---|---|
| `mirror.intervalSeconds` | `60` | Sleep after each refresh attempt, including a failed attempt |
| `nodeCache.path` | Deployment host cache root in `values.yaml` | `DirectoryOrCreate` hostPath mounted at `/cache`; discover the actual path below |
| `nodeCache.uid` | `1001` | UID and GID of the warmer and owner assigned to `git` and `uv` by init |
| `nodeCache.resources.requests.cpu` | `50m` | CPU request for each container |
| `nodeCache.resources.requests.memory` | `128Mi` | Memory request for each container |
| `nodeCache.resources.limits.cpu` | `"1"` | CPU limit for each container |
| `nodeCache.resources.limits.memory` | `1Gi` | Memory limit for each container |
| `worker.image.repository` | `ghcr.io/the-cloud-clockwork/agentihooks-ci-lab-worker` | Image used by init and the warmer |
| `worker.image.tag` | `dev` | Image tag for both containers |
| `scheduler.repository` | `https://github.com/the-cloud-clockwork/agentihooks.git` | Git remote fetched by the warmer |

Read the deployed host path without dumping container environment values:

```bash
kubectl -n ci-lab get ds ci-lab-node-cache \
  -o jsonpath='{.spec.template.spec.volumes[?(@.name=="cache")].hostPath.path}{"\n"}'
```

For example, save this override as `ci-lab-cache.example.yaml` in your deployment checkout.
`/var/cache/ci-lab` is an example override, not the chart's default host path:

```yaml
mirror:
  intervalSeconds: 90
nodeCache:
  path: /var/cache/ci-lab
  uid: 1001
  resources:
    requests:
      cpu: 50m
      memory: 128Mi
    limits:
      cpu: "1"
      memory: 1Gi
worker:
  image:
    repository: ghcr.io/the-cloud-clockwork/agentihooks-ci-lab-worker
    tag: dev
scheduler:
  repository: https://github.com/the-cloud-clockwork/agentihooks.git
```

Render only the cache objects for inspection:

```bash
helm template ci-lab k8s/charts/ci-lab --namespace ci-lab \
  -f ci-lab-cache.example.yaml --show-only templates/node-cache.yaml \
  > node-cache.rendered.yaml
```

The output contains the `ci-lab-node-cache` ConfigMap and DaemonSet. The ConfigMap carries
`warm-node-cache.sh` at the read only `/scripts` mount. A script checksum on the pod
template rolls pods when the script changes. Changing the host path selects a different
node directory; it does not migrate an existing cache. The ownership command changes the
two cache directories only, so changing `nodeCache.uid` does not recursively repair files
already owned by the previous UID.

## Refresh behavior

The warmer calls `warm-node-cache.sh /cache "$REPOSITORY" /tmp` immediately, then sleeps
for `INTERVAL_SECONDS`, supplied by `mirror.intervalSeconds`. The elapsed time between
refresh starts is the work duration plus this sleep. A failed script prints
`node cache refresh failed` and the loop tries again after the sleep.

| Container path | Contents |
|---|---|
| `/cache/git/agentihooks.git` | Bare repository; on the host, `git/agentihooks.git` under `nodeCache.path` |
| `/cache/git/agentihooks.git/fetched-at` | Epoch seconds of the last successful fetch |
| `/cache/git/.warm.lock` | Exclusive lock held during a refresh pass |
| `/cache/uv` | uv cache; the script sets `UV_CACHE_DIR` to this directory |
| `/cache/uv/warmed-lock` | Blob ID of the dev requirements lock whose uv installation last succeeded |
| `/tmp/warm-uv` | Disposable venv and requirements used for warming; removed after a successful install |

Each pass first takes a nonblocking exclusive `flock` on `git/.warm.lock`. When another
pass holds it, the script prints `node cache refresh skipped: another run holds the cache lock`
and exits successfully. While holding the lock, it removes stale `*.lock` files inside the
mirror before fetching. The cache lock stays outside the mirror and is retained throughout
uv warming, so overlapping warmer runs cannot remove each other's active Git locks.

The pass initializes the bare mirror only when its `HEAD` file is absent, sets
`remote.origin.url` from `scheduler.repository`, then runs:

```bash
git -C /cache/git/agentihooks.git fetch --prune --quiet origin \
  '+refs/heads/*:refs/heads/*' '+refs/pull/*/head:refs/pull/*/head'
```

Both forced refspecs track branch updates and pull request heads, including rewritten
heads; `--prune` removes stale destination refs. After a successful fetch the script writes
`fetched-at.tmp` and renames it to `fetched-at`, then prints `mirror fetched`. A failed
fetch exits the script before uv warming and leaves the last successful stamp unchanged.
The timestamp measures git freshness, independently of whether uv warming succeeds.

The script reads the blob ID of `.github/test-requirements.lock` from `refs/heads/dev`.
When the lock exists and differs from `uv/warmed-lock`, it extracts the lock into a
temporary requirements file, creates a venv with `uv venv`, and runs `uv pip install`
against it. If dev also contains `.github/test-excludes.txt`, it passes that extracted file
with `--excludes`. Only a successful install removes the scratch venv and writes the lock
blob ID to `warmed-lock`.

| Condition | Result |
|---|---|
| Dev has no requirements lock | `uv cache skipped: dev has no .github/test-requirements.lock`; the pass succeeds after fetching git |
| Lock blob matches `warmed-lock` | `uv cache current for lock <blob>`; no venv or install |
| Lock blob changed and install succeeds | `uv cache warmed for lock <blob>`; subsequent passes skip the install |
| Venv creation or dependency installation fails | No new `warmed-lock`; the loop retries on the next pass while retaining the successful git fetch stamp |

The lock blob is the only warm marker key. An excludes-only change or a worker image
change with an unchanged lock does not trigger another install. The worker image must
provide git, flock, uv and a Python version compatible with the dev lock; the script sets no
Python version option and does not validate every cached artifact on a marker hit.

## Verify every node

After CI deploys the chart, compare the eligible node count with the DaemonSet and check
pod placement:

```bash
selector=$(kubectl -n ci-lab get ds ci-lab-node-cache -o json \
  | jq -r '.spec.template.spec.nodeSelector | to_entries | map(.key + "=" + .value) | join(",")')
kubectl get nodes -l "$selector" -o name | wc -l
kubectl -n ci-lab get ds ci-lab-node-cache
kubectl -n ci-lab get pods -l app.kubernetes.io/component=node-cache -o wide
```

`DESIRED` must match the eligible CI node count. `CURRENT`, `READY`, `UP-TO-DATE` and
`AVAILABLE` must reach that count for a completed rollout. There is no readiness probe for
cache contents: a running, Ready pod can repeatedly fail to fetch or install dependencies.

Select a cache pod on each eligible node and run these read only checks, replacing the
example pod name with that pod's name:

```bash
cache_pod=ci-lab-node-cache-example
kubectl -n ci-lab logs "$cache_pod" -c prepare
kubectl -n ci-lab logs "$cache_pod" -c warm --tail=80
kubectl -n ci-lab exec "$cache_pod" -c warm -- sh -ec '
  test -f /cache/git/agentihooks.git/HEAD
  git -C /cache/git/agentihooks.git rev-parse --is-bare-repository
  cat /cache/git/agentihooks.git/fetched-at
  git -C /cache/git/agentihooks.git rev-parse refs/heads/dev:.github/test-requirements.lock
  cat /cache/uv/warmed-lock
'
```

Expect `true`, a recent epoch fetch stamp and two equal lock blob IDs. A missing
`warmed-lock` fails the command; a different ID means the current dev lock has not finished
warming. If dev intentionally has no lockfile, verify the explicit skip log instead.
Keep the node counts and per-node checks as delivery evidence. These checks establish
cache preparation, not a worker cache hit or a scheduler metric sample.

The platform's `tests/unit/ci_lab/test_node_cache.py` checks the rendered hostPath,
placement, security, resources, worker image init and interval override. Its fake git and
uv cases cover one-time mirror initialization, both refspecs, fetch stamp preservation
on failure, once-per-lock warming, optional excludes and missing locks. Run these through
the deployment repository's CI; a green fake-tool run does not establish live cache warmth.

## Troubleshooting

| Symptom | Check and action |
|---|---|
| Fewer desired pods than CI nodes | Compare both fixed selector keys and values with node labels; inspect the rendered chart for the exact label domain |
| Pending or init failing | Read pod events, node taints, requests and limits, worker image pull status and hostPath permissions; `prepare` needs its restricted root/CHOWN operation to succeed |
| `node cache refresh failed` and old or absent fetch stamp | Read the preceding git error; check repository access, DNS, egress and mirror ownership. Repair deployment configuration through its pull request process |
| `mirror fetched` but no current `warmed-lock` | Read the uv error. For example, Python 3.12 cannot install a lock dependency requiring Python 3.13. Fix the worker image or dev lock through its owning repository and deploy it |
| Pod is Ready while warming keeps failing | Ready tracks the container, not cache success. Compare the fetch stamp and warmed lock with the checks above |
| Warmer reports current after an excludes-only change | It keys on the lock blob only; an unchanged requirements lock skips installation |
| Errors after changing the UID or cache path | Existing nested files keep their old ownership, and another host path starts another cache; plan the deployment change accordingly |
| Worker still misses git or no mirror age sample appears | This DaemonSet neither mounts worker volumes nor exports the scheduler series. Check the worker's read only mounts and git hit counter in [Worker Deployment](ci-lab.md#worker-deployment); no code sets the mirror age series yet |
