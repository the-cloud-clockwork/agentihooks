---
title: Codex code mode host in the worker image
parent: Reference
nav_order: 78
---

# Codex code mode host in the worker image

The swarm worker image installs `codex-code-mode-host` in `/usr/local/bin`
beside `codex`, taken from the same Codex release and pinned by its own sha256.
Codex 0.162 runs the shell tool of a `code_mode_only` model through that sibling
binary, as the
[Headless clamps](headless-clamp.md#codex-read-only-needs-user-namespaces)
canaries showed on a bare `codex` install. Without it every `exec` call fails,
the agent cannot run its profile canary, and the tick logs that the Codex agent
reported no canary within 90 seconds. The image smoke run proves the host works
by running one Codex shell command through it with the network disabled.

## Why the image needs it

The workstation's standalone Codex install ships the code mode host and its
other package resources next to `codex`. The worker image used to install only
the bare `codex` binary from the release archive, so hive pods carried `codex`
alone and every Codex launch on a hive failed its launch check. The swarm task
that asked for the fix recorded this on release 2.17.330: `/usr/local/bin` in
the hive worker image held only `codex`, agents reported a missing code mode
host, and the tick logged no canary within 90 seconds. Pull request
[#7131](https://github.com/the-cloud-clockwork/agentihooks/pull/7131) added the
host to the lock and the probe to the smoke run. The same gap on a bare binary
is described for the clamp probe host in
[Headless clamps](headless-clamp.md#codex-read-only-needs-user-namespaces).

## The lock entry

`docker/swarm-node/versions.lock` holds the host as one more entry under
`tools`, marked with `companion`:

```json
"codex": {
  "version": "0.162.0",
  "url": "https://github.com/openai/codex/releases/download/rust-v0.162.0/codex-x86_64-unknown-linux-musl.tar.gz",
  "sha256": "8daf67f6261161aa5939d8d42a516032d760406216779d7ce6040f242140ff73",
  "member": "codex-x86_64-unknown-linux-musl",
  "version_output": "codex-cli 0.162.0"
},
"codex-code-mode-host": {
  "version": "0.162.0",
  "url": "https://github.com/openai/codex/releases/download/rust-v0.162.0/codex-code-mode-host-x86_64-unknown-linux-musl.tar.gz",
  "sha256": "dee8bf3df637c68e1495b928d643b76d4ad44f94e4f0ed79fe8482fa79e23d97",
  "member": "codex-code-mode-host-x86_64-unknown-linux-musl",
  "companion": "codex"
}
```

| Field | Meaning |
|---|---|
| `version` | The Codex release, `MAJOR.MINOR.PATCH`. It must equal the version printed in the companion's `version_output`. |
| `url` | An `https` release asset whose path contains `version`. |
| `sha256` | The checksum of the downloaded archive, 64 lowercase hex characters. It differs from the `codex` archive checksum. |
| `member` | The file extracted from the `.tar.gz` archive and installed under the entry's name. |
| `companion` | The locked tool this binary belongs to. It must name `herdr`, `claude` or `codex`. |

A companion entry carries no `version_output`: the host prints no version, so
its version is read from the companion's `version_output` instead.

`worker_image.py` `load_lock` checks the three base tools first and then every
extra entry in name order. It refuses the lock with `invalid artifact lock:
codex-code-mode-host` when the entry:

- names a `version` other than the one in the `codex` `version_output`;
- names a `companion` that is not a base tool, or names itself;
- carries its own `version_output`;
- has an empty or malformed `sha256`, or a `url` that is not `https` or lacks
  the version;
- has no `companion` key, since an extra tool then has no version output to
  match.

`test_committed_lock_installs_the_code_mode_host_beside_codex` holds the
committed lock to the same release as `codex`: same version, the `codex` asset
URL and member with `codex-` replaced by `codex-code-mode-host-`, and a
different checksum. `load_lock` itself does not require the entry, so that test
is what fails if it is removed.

## Install and identity check

The `tool-downloads` build stage fetches every locked tool concurrently and
checks each archive against its `sha256` before caching it by checksum. The
worker stage runs `worker_image.py install`, which extracts each `member`,
writes it to `/usr/local/bin/<name>` with mode `0755`, and then checks every
installed binary:

- A base tool runs `<name> --version` and must print its `version_output`
  exactly, or the build fails with `binary version mismatch: <name>`.
- A companion runs `<name> --help`, must exit zero and must print output that
  starts with `Usage: <name> `. A wrong binary fails with
  `binary usage mismatch: codex-code-mode-host`; a binary that exits non-zero
  fails the build with the command's error.

The build manifest records the host under `observed.tools` as its name, the
locked version and the checksum of the installed file:

```text
codex-code-mode-host 0.162.0 sha256:<sha256 of /usr/local/bin/codex-code-mode-host>
```

`declared.tools` lists `codex-code-mode-host` with its version. The image's
default `report` command, which each startup container runs, recomputes both and
refuses an image whose installed host differs from its manifest with `installed
inventory differs from build manifest`.

## Smoke step

`docker/swarm-node/smoke.sh` runs the probe in the `startup` proof, and so in
`all`; the `rebuild` and `profiles` legs skip it. It is one more offline
container, run against the proof image built from an archived clean context and
written to the evidence folder:

```bash
docker run --rm --network none --read-only \
    --tmpfs /home/worker:uid=10001,gid=10001 --tmpfs /tmp \
    -v "$context/tests/swarm_node/code_mode_probe.py:/opt/probe/code_mode_probe.py:ro" \
    "$image" python /opt/probe/code_mode_probe.py /usr/local/bin/codex > "$output/codex-code-mode.json"
```

To run the probe against an image built by hand, as in the README build command,
replace `"$context"` with the repository root and `"$image"` with
`agentihooks-worker:dev`.

`tests/swarm_node/code_mode_probe.py`:

1. Starts a local stand in for the Responses API on `127.0.0.1` and a free port.
2. Runs `codex debug models` with a fresh `CODEX_HOME` and picks the first model
   whose `tool_mode` is `code_mode_only`. A catalog with none fails with
   `Codex model catalog offers no code mode model`.
3. Runs `codex exec --skip-git-repo-check --ephemeral --json -s danger-full-access -m <model>`
   with a `probe` model provider pointed at the local server, the key read from
   `CODEX_PROBE_KEY`, and a 90 second timeout.
4. The local model answers with a code mode `exec` program that calls
   `tools.exec_command` to run `echo code-mode-host-$((6*7))`, then ends the
   turn once Codex returns the tool output.

The shell arithmetic means the marker `code-mode-host-42` appears only when a
real shell ran the command through the host. The probe prints one JSON report,
which the smoke run saves as `codex-code-mode.json` in the evidence folder:

```json
{"model": "<code mode model slug>", "exit": 0, "code_mode_errors": [], "host_output": true}
```

| Field | Pass value |
|---|---|
| `model` | The `code_mode_only` model the catalog offered |
| `exit` | `0`, the `codex exec` exit code |
| `code_mode_errors` | Empty: no error event whose message mentions `Code Mode` |
| `host_output` | `true`: the marker came back in the tool output |

Any failing value prints the report first, then exits 1 with `Codex shell
command through the code mode host failed:` followed by the last 2000 characters
of Codex stderr. The smoke run stops, and the saved report shows which condition
failed.

## Reading the evidence in CI

The Swarm worker image smoke workflow runs `smoke.sh` once per proof leg and
uploads each leg's evidence folder. The probe report is in the
`swarm-worker-image-startup` artifact:

```bash
gh run download <run id> --name swarm-worker-image-startup --dir worker-evidence
jq . worker-evidence/codex-code-mode.json
```

The installed host's manifest line is under `manifest.observed.tools` in the
leg's `first.json` and `second.json` startup reports:

```bash
jq '.manifest.observed.tools["codex-code-mode-host"]' worker-evidence/first.json
```

## Moving to a new Codex release

Update both entries together; the lock refuses a host whose version differs from
`codex`.

```bash
v=0.163.0
base=https://github.com/openai/codex/releases/download/rust-v$v
for asset in codex codex-code-mode-host; do
  curl -fsSL --retry 5 --retry-connrefused "$base/$asset-x86_64-unknown-linux-musl.tar.gz" | sha256sum
done
```

Then in `versions.lock`:

1. In `codex`, set `version`, `url`, `sha256` and `version_output` to the new
   release (`codex-cli 0.163.0`). `member` keeps its name.
2. In `codex-code-mode-host`, set `version`, `url` and `sha256`. Leave `member`
   and `companion` unchanged.

The pull request's worker image smoke run then installs both, checks them and
runs the probe against the new release.

## Limitations

- Only Linux amd64 is locked, so only the `x86_64-unknown-linux-musl` assets
  are installed.
- `worker_image.py --tool` accepts only `herdr`, `claude` and `codex`. A
  partial `download` or `install` limited with `--tool codex` leaves out the host.
- The probe proves the host runs one shell command offline with
  `danger-full-access`. It does not prove the bubblewrap read only sandbox,
  which needs unprivileged user namespaces on the host.
- The image identity check reads `--help` output. It catches a missing,
  misnamed or failing host; the pinned archive checksum is what ties the bytes
  to the release.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `invalid artifact lock: codex-code-mode-host` | The entry breaks one of the lock rules above. Most often `codex` was bumped without the host: set both to the same release. |
| `artifact checksum mismatch: codex-code-mode-host` | The downloaded archive does not match `sha256`. Recompute it from the release asset; never copy the `codex` checksum. |
| `KeyError` from `tarfile` during `worker_image.py install` | `member` names a file the archive does not hold. List the archive with `tar -tzf` and copy the exact member name. |
| `binary usage mismatch: codex-code-mode-host` | The installed file is another binary: `member` or `url` points at the wrong asset. Check both against the release. |
| `Codex model catalog offers no code mode model` | The Codex release lists no `code_mode_only` model. The probe has nothing to exercise; check the release notes before bumping. |
| `Codex shell command through the code mode host failed` with `code_mode_errors` set | Codex reported a Code Mode error. Read the error text in the report and the stderr tail, then confirm `/usr/local/bin/codex-code-mode-host` exists in the image and `manifest.observed.tools` lists it. |
| Same failure with `host_output` false and no errors | The shell command did not run or its output did not return. Read the Codex stderr tail in the failure message. |
| A Codex agent on a hive reports a missing code mode host, or the tick logs no canary within 90s | The hive pod runs an image built before the host was locked. Roll the hive onto a release that carries it and check `codex-code-mode-host` in the pod's `/usr/local/bin`. |
