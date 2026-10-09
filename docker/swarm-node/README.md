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
The launch supervisor belongs to a subsequent IMG package.

Before an attempt starts, `python -m scripts.swarm_v2.worker_home bootstrap`
renders its Claude and Codex profiles into a private attempt home under
`/home/worker/attempts/<attempt>`, one home per target, through the same target
adapters `agentihooks init` uses. Only the selected profiles are copied in, and
accounts are recorded as variable names, never values. Hook and MCP commands use
the container interpreter; a profile pointing at any path outside the attempt,
the interpreter prefix or the agentihooks install fails bootstrap, as does a
noexec home volume when Codex is requested, because its hook wrapper must
execute. Rerunning an accepted request is a no-op, an interrupted one is
rendered again from scratch, and a different request for an accepted attempt is
refused. Rollback selects the prior profile templates for new attempts; existing
attempt homes are kept for recovery. This image introduces no orchestration service or embedded database.
The existing ledger service Dockerfile remains separate.

After committing inputs, `bash docker/swarm-node/smoke.sh OUTPUT_DIRECTORY`
builds an archived clean context, starts two independent containers with network
disabled, rejects three invalid locks, rebuilds without cache and starts the
retained image again. It also bootstraps the fixture profiles in
`fixtures/profiles` inside fresh containers, runs `claude mcp list`, `codex mcp
list` and each SessionStart hook offline, and writes the SV2-IMG-02 evidence. The output contains the build manifest, software inventory,
profile hashes and package evidence. Production deployment and new attempt image
selection remain with antoncore GitOps. Rollback retains the previously qualified
image for new attempts; active attempts and durable task state are unchanged.
