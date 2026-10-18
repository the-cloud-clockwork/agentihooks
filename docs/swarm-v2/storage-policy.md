# Swarm v2 storage policy

Package SV2-FSY-05. Shared storage is allowed; shared mutable runtime identity is not.

## Rules

| Storage | Allowed | How a worker Pod gets it |
|---|---|---|
| Native runtime roots: the Pod home `/home/worker` itself, everything under `/home/worker/attempts`, `/tmp` and `/var/run/swarm`, and any `.codex`, `.claude`, `.config`, `.local` or `herdr` folder | Private to the attempt only | Pod `emptyDir`; never a shared writable volume |
| Read only configuration seed | Yes | Pod policy `mounts` entry with purpose `seed`, rendered read only |
| Checkpoint and artifact storage | Yes, scoped | Purpose `artifact`, rendered writable with `subPath` set to the execution id |
| Node cache store | Yes, read only into attempts | Purpose `cache`, rendered read only; only the store owner writes seeds |
| Any host path at or inside an operator home (`/home/<user>`, `/var/home/<user>`, `/root`, `/Users/<user>`, the WSL `/mnt/<drive>/Users/<user>`) or a parent of one (`/`, `/home`, `/var`, `/var/home`, `/Users`, `/mnt`, `/mnt/<drive>`) | Never, not even read only | Refused; a seed comes from a claim, NFS or a host path outside every home |

`scripts.swarm_v2.kubernetes.storage.MountChecker` enforces the table on every rendered Pod. `PodTemplate.render`
refuses a Pod that fails it with reason `storage`, and `python -m scripts.swarm_v2.kubernetes.storage <pod.json>`
checks any manifest. The checker reads the volumes themselves, never a purpose label:

- `emptyDir`, `configMap`, `secret`, `projected`, `downwardAPI` and `ephemeral` volumes are private.
- A volume is private only when every source it names is a private kind; any other volume, and a mount naming no volume, is shared.
- A shared mount marked read only on the mount, or on every `persistentVolumeClaim` or `nfs` source, is a seed and passes. `hostPath` has no source read only flag.
- A shared writable mount at or over `/home/worker`, at, over or under the attempt roots, `/tmp` or `/var/run/swarm`, or naming a native folder, is refused as `shared_runtime`, even with a subPath. Paths are normalised first, so `//home/worker` and `..` spellings do not slip through. A folder beside them, such as `/home/worker/artifacts`, is not a runtime root.
- Any other shared writable mount needs a `subPath` equal to, or inside, the Pod's execution id label;
  otherwise it is refused as `unscoped_shared_write`. `subPathExpr` is not accepted as proof of scope.
- A `hostPath` volume at an operator home is refused as `operator_home`, mounted or not, checked on every volume entry.
- `shared_runtime_mount_rejections_total`, the package measurement named by the spec, counts every reason.

The checker is lexical. A node host path that is a symlink into a home passes, because the kubelet follows it on the
node; node layout and pod policy authors are trusted, and the checker does not resolve node paths.

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

Rollout safety and rollback: set `SWARM_ARTIFACT_PUBLISHING=off` in the worker environment to pause only artifact
publication; every attempt keeps its files and runs as private local execution. Unset it once the backend is healthy.
A pod policy without `mounts` renders the private Pod of SV2-KUB-01. Committed artifact objects stay authoritative; do
not delete them as caches. The rehearsal (pause, resume, attempt files unchanged) runs in CI in the storage layout
contract tests.

## Authority

Publication scope comes only from the launch grant: the artifact store is built from the registration its authorization
returns for the attempt's grant token, and a store whose authorization answers with anything else, such as a seat or
task label, is refused. The artifact mount is scoped by `subPath` to the launch's execution id, never to a Pod label.

## Data safety before rollback

Authoritative objects, never deleted as caches: committed artifact objects and their records in the artifact store,
seeds published by the node cache owner, and every attempt worktree. Disposable: an attempt's private home and tmp
volumes, and the benchmark's own subfolder. Rollback pauses publication and leaves the artifact store unchanged; the
benchmark removes only the subfolder it created.

## Production path

`KubernetesTransport.apply_operation`, the controller's Pod create path, renders through `PodTemplate`, which runs the
storage checker. A refused policy answers `REFUSED` and sends no create to the API server.

## Reproducing a refusal

The storage layout contract fixtures hold the smallest Pods that reproduce each refusal: a shared writable Codex home,
the operator home and a writable node cache mount, beside the safe Pod rendered from the shared storage policy. Run
the checker command on any of them; a failing rejection or recovery case keeps its Pod there.

## Per execution persistent subdirectories

NFS and other shared filesystems are not unsafe in general. They suit artifact archives, checkpoint repositories,
repository mirrors and backups. Alternatives for state that must outlive a Pod:

1. An artifact mount scoped by `subPath` to the execution id, as rendered today.
2. A per execution directory on a tested shared filesystem for recovery material, published through the artifact
   adapter rather than used as a live runtime home.
3. Native runtime state stays on worker local storage and is captured by checkpoint, never mounted live from shared
   storage. Allowing a shared per execution runtime home needs measured locality and failure behaviour first.

Keep active checkouts and worktrees on worker local storage by default.

## Benchmark

`python -m scripts.swarm_v2.artifacts.benchmark <mounted folder> --sizes 1,16 --rounds 3` writes artifacts of each size
through the artifact adapter into a fresh subfolder, reports seconds and MiB per second per size, then takes that
subfolder away to show that publication pauses with the attempt file intact and resumes once it returns. It removes
only its own subfolder. Run it from a cloud worker against the on-prem mount at the rollout gate for the WAN figure;
the only figure so far is a local disk run, which says nothing about the WAN path.
