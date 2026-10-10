# Swarm v2 storage policy

Package SV2-FSY-05. Shared storage is allowed; shared mutable runtime identity is not.

## Rules

| Storage | Allowed | How a worker Pod gets it |
|---|---|---|
| Native runtime roots: the Pod home `/home/worker`, the attempt roots under `/home/worker/attempts`, `/tmp`, `/var/run/swarm`, and any `.codex`, `.claude`, `.config`, `.local` or `herdr` folder | Private to the attempt only | Pod `emptyDir`; never a shared writable volume |
| Read only configuration seed | Yes | Pod policy `mounts` entry with purpose `seed`, rendered read only |
| Checkpoint and artifact storage | Yes, scoped | Purpose `artifact`, rendered writable with `subPath` set to the execution id |
| Node cache store | Yes, read only into attempts | Purpose `cache`, rendered read only; only the store owner writes seeds |
| The operator home from the node (`/`, `/home`, `/home/<user>`, `/root`, `/Users/<user>`) | Never, not even read only | Refused |

`scripts.swarm_v2.kubernetes.storage.MountChecker` enforces the table on every rendered Pod. `PodTemplate.render`
refuses a Pod that fails it with reason `storage`, and `python -m scripts.swarm_v2.kubernetes.storage <pod.json>`
checks any manifest. The checker reads the volumes themselves, never a purpose label:

- `emptyDir`, `configMap`, `secret`, `projected`, `downwardAPI` and `ephemeral` volumes are private.
- Every other volume kind, and a mount naming no volume, is shared.
- A shared mount marked read only, on the mount or at its source, is a seed and passes.
- A shared writable mount at, over or under a native runtime root is refused as `shared_runtime`, even with a subPath.
- Any other shared writable mount needs a `subPath` equal to, or inside, the Pod's execution id label;
  otherwise it is refused as `unscoped_shared_write`. `subPathExpr` is not accepted as proof of scope.
- A `hostPath` volume at an operator home is refused as `operator_home`, mounted or not.

Rejections are counted per reason in `shared_runtime_mount_rejections_total`.

## Node cache store

Attempts mount the node cache store read only, so the kernel refuses a seed write whatever the file modes say.
`cache.attach` performs no write on a read only store: it verifies the entry, counts a corrupt one as a miss and leaves
it for the owner, and skips the least recently used touch. `cache.publish` from an attempt whose store is read only is
refused. The store owner, a process with its own writable mount, is the only writer: it publishes, evicts and drops
corrupt entries. Eviction on a store that attempts only read falls back to publish order.

## Lost shared storage

`scripts.swarm_v2.artifacts.publication.publish` reads the attempt file and puts it through the artifact store. When the
backend is unreachable it returns `paused` with the cause and leaves the attempt files untouched; a retry with the same
artifact id after the backend returns commits once. A content conflict is still an error.

Rollback: run private local execution with artifact publishing paused until the backend is healthy. Committed artifact
objects stay authoritative; do not delete them as caches.

## Per execution persistent subdirectories

NFS and other shared filesystems are not unsafe in general. They suit artifact archives, checkpoint repositories,
repository mirrors and backups. Alternatives for state that must outlive a Pod:

1. An artifact mount scoped by `subPath` to the execution id, as rendered today.
2. A per execution directory on a tested shared filesystem for recovery material, published through the artifact
   adapter rather than used as a live runtime home.
3. Native runtime state stays on worker local storage and is captured by checkpoint, never mounted live from shared
   storage. Allowing a shared per execution runtime home needs measured locality and failure behaviour first.

Keep active checkouts and worktrees on worker local storage by default.

## Not yet measured

Cloud to on-prem artifact throughput and failure behaviour over the WAN path is a live measurement taken at the rollout
gate; no figure exists yet.
