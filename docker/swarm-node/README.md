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
Rollback selects the prior profile digest for new attempts; existing attempt
homes are kept for recovery. The supervisor pins both native config homes to
the selected private home, marks the admitted attempt trusted for Claude, and
passes the admitted attempt as Codex project trust. This image introduces no orchestration service or
embedded database. The existing ledger service Dockerfile remains separate.

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
builds an archived clean context, starts two independent containers with network
disabled, rejects three invalid locks, rebuilds without layer cache and starts the
retained image again. The rebuild reuses pip wheels and tool binaries from a
BuildKit cache mount, each checked against its locked sha256 before use. It also bootstraps the fixture profiles in
`fixtures/profiles` inside fresh containers, runs `claude mcp list`, `codex mcp
list`, `claude -p` and `codex exec` offline so their SessionStart hooks register
the session, proves refusals, crash recovery and a profile rollback, and writes
the SV2-IMG-02 evidence. The output contains the build manifest, software
inventory, profile hashes and package evidence. Production deployment and new
attempt image selection remain with antoncore GitOps. Rollback retains the
previously qualified image for new attempts; active attempts and durable task
state are unchanged.
