Build the shared worker environment from the repository root:

```bash
docker build \
  --platform linux/amd64 \
  --file docker/swarm-node/Dockerfile \
  --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" \
  --tag agentihooks-worker:dev \
  .
```

The supported architecture is Linux amd64. `versions.lock` pins the base digest,
Debian snapshot and shell package versions, native herdr, Claude and Codex release
checksums, and the hashed Python requirements closure. The agentihooks source
wheel uses version zero with its tested source revision recorded in the manifest.
Changing requirements also requires updating their checksum in `versions.lock`.
Template inputs come from the repository's packaged profiles, never a host home
or credential store. Runtime homes are writable; templates and the environment
are owned by root and read only to the worker user.

The default command verifies and reports installed inventory without a network.
Every command runs under Tini as PID one, which reaps children. Production
execution supplies `python /opt/swarm-node/supervisor.py ATTEMPT LAUNCH_JSON`.
The supervisor owns a headless herdr server, an exporter and one main agent in
a herdr pane. Viewers can attach and detach without owning those processes.

Before an attempt starts, `python -m scripts.swarm_v2.worker_home bootstrap`
renders its Claude and Codex profiles into a private attempt home under
`/home/worker/attempts/<attempt>`, one home per target, through the same target
adapters `agentihooks init` uses. Only the selected profiles are copied in, and
accounts are recorded as variable names, never values. Hook and MCP commands use
the container interpreter. Every rendered setting except permission rules, every
MCP server field and every Codex hook export is scanned: a path outside the
attempt, the interpreter prefix or the agentihooks install fails bootstrap, as
does any `..`, `~` or `$` path, and a noexec home volume when Codex is
requested, because its hook wrapper must execute. Other relative paths are
admitted. Rerunning an accepted request is a no-op, an interrupted one is
rendered again from scratch, and a different request for an accepted attempt is
refused. The execution record names the digest of each selected profile.

`layout.json` (copied to `/opt/swarm-node/layout.json`) is the SV2-FSY-01 path
contract, read by `scripts.swarm_v2.filesystem`. Every attempt root holds the
same private folders: `homes` for writable CLI state, `run` for supervision and
Unix sockets, `checkouts`, `worktrees`, `spool`, `tmp` for scratch, and
`profiles` for profile seeds, which bootstrap seals read only. Each folder is
created mode 0700 under the attempt, so two Pods use the same internal names
without sharing a mutable file or socket. `filesystem.environment` gives a
process its `HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `XDG_RUNTIME_DIR` and
`TMPDIR` inside those folders. A path, profile link or archive member
that resolves outside its attempt root after symlink resolution is refused
before any write and counted in `execution_path_validation_failures`. The
execution record carries the layout as relative folder names only;
`filesystem.restore` recreates an attempt under a new base from that record and
refuses absolute paths or an unknown layout version, which stops new launches
while the readers for earlier versions stay in place.
Rollback selects the prior profile digest for new attempts; existing attempt
homes are kept for recovery. The supervisor pins both native config homes to
the selected private home, marks the admitted attempt trusted for Claude, and
passes the admitted attempt as Codex project trust. This image introduces no orchestration service or
embedded database. The existing ledger service Dockerfile remains separate.

`cache-policy.json` (copied to `/opt/swarm-node/cache-policy.json`) is the
SV2-FSY-03 cache contract, read by `scripts.swarm_v2.cache`. A cache key covers
the cache kind, toolchain, dependency lock digest, platform and trust scope, so
a different lock or another project's scope never reuses an entry. The store
is built with the attempt's granted scope, its verified project identity: an
attempt reads entries of that scope or of a policy `shared` scope and publishes
only into its own, so an untrusted project cannot write a `trusted` toolchain. `attach`
gives each attempt a writable layer under its own `tmp/cache` and, on a hit, the
path of a sealed read-only seed in the node store. `publish` copies a writable
layer into the store as a new seed: it refuses links, special files, the
excluded credential and session database names, and executables in kinds that
may not hold them; it never replaces an existing entry and refuses one whose
recorded files differ, and it evicts least
recently used entries only when that fits the byte budget and the free-space
reserve, otherwise nothing changes. An entry whose recorded key or file
manifest no longer matches is discarded on its next attach and counted in
`cache_corruption_total`; `cache_hit_rate` is the share of attaches that found a
seed. Setting `enabled` to false turns reuse off in the module: attaches return
no seed, publishes write nothing, and no workspace, home or archive is touched.
No worker launch path calls the module yet, and the granted scope is passed in
by the caller, so the toolchain rejection holds at this interface only. Seeds
are sealed by file mode only. Each attach and each publish over an existing
entry re-hashes its files and checks their execute bits, but a seed edited
after an attach runs unverified for that holder until the next attach discards
it. A store owned by another user or mounted read only into attempts is left
to deployment.

The SV2-IMG-03 launch JSON has schema version one, `authority`, `harness`,
`agent` and `exporter` argument vectors. Authority must exactly match a
controller supplied `registration.json` beside the bootstrap execution record;
its execution identifier must match the bootstrapped attempt. The registration
is a nonsecret, previously admitted controller record, not a grant token or a
worker self registration. Its delivery and read only mounting belong to the
execution adapter. The worker does not verify signing keys. Budgets
`startup_seconds`, `quiesce_seconds`, `checkpoint_seconds` and `kill_seconds`
are finite, positive and at most three hundred each. Defaults are ten, ten, ten
and two seconds. The local runtime adapter is unchanged.

Each launch has an exclusive attempt lock and a new random incarnation under
its run directory. `SWARM_SUPERVISION_DIR` points to that directory; its
`context.json` binds authority and incarnation. An exporter calls
`scripts.swarm_v2.supervision_protocol.acknowledge("exporter", status="ready")`
when usable. Only then does the main agent start. SIGTERM or SIGINT publishes
`drain.json`, stops the agent and herdr process trees including escaped
grandchildren, and publishes `quiesced.json`. The exporter remains alive to
flush. No task reconciliation, authority renewal or second swarm timer runs
in the supervisor.

The exporter acknowledges `exporter.checkpoint` with status complete, a
relative manifest path under the attempt checkpoints directory and its SHA256.
The persisted manifest must match the context, say complete and name a
checkpoint identifier. This is a local exporter handoff acknowledgement;
archive commit, restore qualification and lease fencing remain with their
later packages. Forced quiescence, missing acknowledgement, changed scope or
late acknowledgement reports incomplete. Results are per incarnation and never
overwrite a prior result or a latest checkpoint pointer. Exit zero means a
clean agent completion or requested drain with acknowledged material; seventy
means startup, agent, exporter or supervisor failure; seventy five means a
clean termination with incomplete material; sixty four is a refused launch.
`result.json` records exit reasons and `supervisor_child_exit_total`. If the
supervisor is killed, Tini exits and Linux destroys the container process
namespace. Such a death produces no fabricated checkpoint success.

The isolated acceptance fixture uses the real headless herdr binary, a
synthetic agent running a tool with two grandchildren, and a delayed exporter.
Build its Dockerfile from an immutable worker image and run
`tests/integration/swarm_node/supervision_proof.py` with the fixture image,
prior qualified image, tested commit and an output directory. It proves two
independent detachments, supervisor SIGKILL containment, bounded SIGTERM with
late or missing acknowledgement, forced descendants and selecting the prior
image for a new attempt. The retained prior worker image has no supervisor;
the first implementation rehearses its headless herdr compatibility path,
launching a distinct bootstrapped attempt while checking the current attempt's
process identity and homes and existing checkpoint files remain unchanged.
Historical supervisor image rollback must be requalified when such an image
exists. Production rollout remains with antoncore GitOps.

After committing inputs, `bash docker/swarm-node/smoke.sh OUTPUT_DIRECTORY`
builds an archived clean context, scans the built image for credentials with
`docker/swarm-node/scan.sh`, starts two independent containers with network
disabled, rejects three invalid locks, rebuilds the worker stage without cache and starts
the retained image again. The rebuild reuses the pip wheels and tool binaries of the
`downloads` stage, each checked against its locked sha256 before use. It
also bootstraps the fixture profiles in `fixtures/profiles` inside fresh
containers, runs `claude mcp list`, `codex mcp list`, `claude -p` and `codex
exec` offline so their SessionStart hooks register the session, proves
refusals, crash recovery and a profile rollback, and writes the SV2-IMG-02
evidence. The output contains the build manifest, software
inventory, profile hashes and package evidence. Production deployment and new
attempt image selection remain with antoncore GitOps. Rollback retains the
previously qualified image for new attempts; active attempts and durable task
state are unchanged.

`python /opt/swarm-node/health.py MODE --attempt ATTEMPT --harness claude|codex`
answers the execution adapter's probes with one JSON report. `liveness` reads
only the attempt's newest supervisor incarnation: its process is alive in this
process namespace and it has published no result. herdr and every external
dependency stay out of liveness, so an outage never restarts a running agent.
`startup` adds the self tests: herdr and the harness binary on `PATH`, the
private home readable and writable, the attempt `run` and `tmp` folders
writable, every program in every SessionStart hook command callable (a `cd`
must name an existing folder), and herdr answering
`workspace list` over its local socket for that incarnation. `readiness` also
requires the agent to be running. A brain named by `BRAIN_URL` that does not
answer `/health` reports `degraded` under `dependencies` with exit zero; local
failures report `not_ready` with exit one. `diagnose` prints the full report
for a failed bootstrap with exit zero and lists environment variable names,
never values. Every report names `worker_startup_failure_reason`, the first
failing local check. Probes leave nothing behind in the attempt.
`--herdr-timeout` and `--brain-timeout` (seconds, default 2) are the probe
thresholds; a wrong value is reverted in the probe arguments alone, with no new
image and no agent restart. Probe wiring and
thresholds live in the Pod template under antoncore GitOps and roll back
independently of the image. `tests/integration/swarm_node/run_health_proof.sh`
proves the probes against isolated containers with real headless herdr.

Image promotion runs in `.github/workflows/swarm-node-image.yml` on GitHub hosted
runners only, so a broken swarm can still build and publish its repair image: no
swarm runner, ledger or Redis is involved. A dev push (or a dispatch with
`publish`) builds the candidate from the checked out commit, then
`docker/swarm-node/qualify.sh` runs `python -m scripts.swarm_v2.image_probe`
inside it with the network disabled and no credentials. The probe starts a
private headless herdr server and reads its status and socket API schema, then
launches `claude -p` and `codex exec` from fixture profiles and counts the
sessions their SessionStart hooks registered. `scripts.swarm_v2.image_attestation`
qualifies each target against the image manifest's pinned version and the
accepted herdr contract of the local herdr 0.9.1 runtime path (protocol 22,
endpoint protocol generation one and the health check capability, and every
socket method the runtime calls). The contract leaves out `detached_server_daemon`: that flag
reports whether a client spawned the server as a background daemon, and the
supervisor runs `herdr server` in the foreground, where herdr 0.9.1 reports it
false. Viewers attach and detach over the server socket, which protocol 22 and
the socket methods cover. Qualification reports
`worker_image_qualified_targets`. Any refused target, or a manifest naming another
commit, leaves the image unpromotable and nothing is pushed. The same job builds
an incompatible herdr fixture and requires its refusal, and qualifies the
candidate twice in independent containers. Before the registry login it builds a
fixture carrying a build-time generated GitHub app token, requires
`docker/swarm-node/scan.sh` to refuse it, then scans the candidate. The scan runs
Trivy's secret scanner over every image layer and the image config, fails on any
finding and fails when the scanner cannot finish. The one exception is a
`jwt-token` match in an installed Python package's `dist-info/METADATA`, where
package descriptions quote example tokens; it is recorded in
`package-examples.txt`, and any other rule in that file still fails.

Only a qualified image is pushed, under the immutable tag `sha-<commit>`, after
the registry login, which holds the workflow token; build arguments carry only
the source revision. `docker/swarm-node/publish.sh` confirms the registry config
digest equals the tested image before recording the digest. An existing commit
tag is never pushed again: a rerun records it as a replay only when it holds the
tested image, and a registry that cannot say whether the tag exists stops the
run with nothing pushed. The
`swarm-worker-image-attestation` artifact holds the probe output and the
attestation with the registry digest, which is the immutable digest reference
`agentihooks-worker@sha256:...`, the version manifest, the protocol compatibility
manifest, the test report and provenance naming the commit and run. Publication
runs only from dev or a proof branch; a dispatch elsewhere qualifies without
pushing. `diffcheck/` proof branches publish only to the
`agentihooks-worker-proof` repository. Rollback points deployment at the last
accepted digest without rebuilding or retagging it; deployment selection belongs
to antoncore GitOps.
