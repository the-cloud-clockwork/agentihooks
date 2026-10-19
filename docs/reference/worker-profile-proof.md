---
title: Worker profile proof
parent: Reference
nav_order: 20
---

# Worker profile proof

The worker image smoke workflow checks that rendered fixture profiles work in
isolated Linux amd64 containers. Its `profiles` leg bootstraps private homes,
checks the Claude and Codex MCP lists, runs Claude offline and replays a fixture
SessionStart event through every rendered Codex SessionStart hook. Each harness
must register exactly one session in the proof working directory. Codex
registration uses the rendered hook commands without an authenticated model
request.

The Codex replay also rejects hook children that remain alive after the hooks
finish. Separately, the smoke script measures from registration to Docker's
container exit timestamp. A container destroying an abandoned child cannot turn
that leak into a passing proof.

For image construction, profile bootstrap and production image selection, see
the [worker image guide](../../docker/swarm-node/README.md). This proof uses
fixture profiles with networking disabled; it does not qualify private bundles,
model authentication or production deployment.

## Registration and child lifetime

`codex_session_start` reads the rendered `hooks.json` and runs its SessionStart
commands in order, feeding each the fixture event. The session identifier is
`worker-profile-codex`; `HOME` and `CODEX_HOME` point to the private Codex home.
An empty command list fails with `the rendered Codex profile has no SessionStart
hook`. A failed command or a command exceeding sixty seconds fails the replay.

While a command runs, the probe polls the active session registry every ten
milliseconds. `first_registration` reads the registry modification time before
and after parsing its JSON and accepts an observation only when both times
match and the fixture session is present. Missing, unreadable or changing data
is retried on a later observation. The first accepted timestamp is retained
across later polls and hook commands; a later rewrite cannot reset the clock.
This is the first **observed** matching write, not a filesystem event history:
multiple writes between polls can precede the first accepted observation.

Before launching hooks, the probe records existing descendants and becomes a
Linux child subreaper. Orphaned hook descendants, including children detached
into another session, are adopted by the probe. After hook execution it waits
up to one second for new, non-zombie descendants to exit. Remaining children
receive SIGKILL and are reported by PID and command in an assertion:

```text
Codex hook children outlived registration: ['12345 sleep 30']
```

The probe attempts to reap adopted children for up to another second. A
survivor fails the replay even if its hook returned zero; if the hook also
failed, its error remains the assertion's cause. Existing descendants are
excluded by PID and process start time. A child that finishes within the grace
period passes this check.

## Timing reports and fixed limits

The positive case runs twice in independent containers. Each report includes
these fields under `codex_probe`:

| Field | Meaning |
|-------|---------|
| `registered_at` | Registry modification time in epoch seconds from the first accepted matching observation |
| `registration_to_exit_seconds` | Time from that timestamp to the replay function's return measurement, including child cleanup |
| `elapsed_seconds` | Monotonic duration of hook replay and cleanup |
| `registration_to_container_exit_seconds` | Docker `State.FinishedAt` minus `registered_at`, added by the smoke script after the positive container exits |

Both positive reports must satisfy
`0 <= registration_to_container_exit_seconds < 2`. Exactly two seconds fails.
The container measurement includes the remainder of the probe and container
shutdown; `registration_to_exit_seconds` alone does not prove that budget.

These thresholds are source constants, not environment settings:

| Control | Current value |
|---------|---------------|
| Registry polling (`POLL_HOOK_SECONDS`) | 0.01 seconds |
| Hook execution timeout | 60 seconds per command |
| Descendant exit grace (`HOOK_CHILD_SECONDS`) | 1 second |
| Adopted child reaping deadline | 1 additional second |
| Registration to container exit | Less than 2 seconds, nonnegative |

Changing these limits requires a code change and CI proof. Increasing the
container budget does not address a surviving child failure.

## Run and configure CI proof

Commit and push the revision to test, then dispatch the worker smoke workflow:

```bash
gh workflow run swarm-node-smoke.yml \
  --ref <pushed-branch>
```

It accepts no dispatch inputs. The workflow runs `startup`, `rebuild` and
`profiles` as separate matrix legs, then compares their inventories. To invoke
the same workflow through the shared proof dispatcher:

```bash
gh workflow run proofs.yml \
  --ref <pushed-branch> \
  -f proof=worker-image
```

For a focused regression check of registry rewrites and surviving children:

```bash
gh workflow run proofs.yml \
  --ref <pushed-branch> \
  -f proof=tests \
  -f tests=tests/test_worker_profile_offline.py
```

The focused tests complement the real image proof; they do not measure Docker
exit. Run tests and image proofs in CI, following the repository's local test
restriction.

The image workflow calls the following command on its runner:

```bash
bash docker/swarm-node/smoke.sh "$RUNNER_TEMP/worker-image-evidence" profiles
```

The first argument selects the evidence directory. The optional second
argument is `all` (default), `startup`, `rebuild`, `profiles` or `compare`.
`compare` consumes downloaded leg evidence and requires the tested commit as
its third argument. `profiles` still builds and scans the image; it runs the
positive, repeated positive, rejection, recovery, rollback and noexec cases.
The script archives committed `HEAD`, so uncommitted fixture edits do not enter
the proof.

The workflow sets `WORKER_SCANNER_CACHE` for scanner reuse and may pass
`WORKER_LAYERS` for image layer reuse. `WORKER_KEEP_IMAGE` retains a tagged image
for a later proof step. None of these controls changes registry polling, child
detection or the exit budget. No account credential is needed for the offline
Codex replay.

## Read evidence and diagnose failures

Open the selected run and download the profiles artifact into a task evidence
directory:

```bash
gh run view <run-id>
gh run download <run-id> \
  --name swarm-worker-image-profiles \
  --dir <task-evidence-directory>/profiles
```

Inspect `profile-positive.json` and `profile-positive-second.json` for
`codex_probe` timings. Their corresponding `.log` files retain container stderr;
`build.log` and `credential-scan/` cover image preparation. Successful profile
aggregation writes `SV2-IMG-02/result.json`, and a successful leg writes
`mode-result.json`. The final comparison artifact is
`swarm-worker-image-evidence`. A missing success report is not a pass: read the
failed step and partial evidence first.

| Failure | Meaning and response |
|---------|----------------------|
| `Codex hook children outlived registration` | A new hook descendant remained alive beyond the grace period. Use its PID and command to identify the owner; make the hook wait for or terminate its children before returning. |
| `Codex registration to container exit took ... seconds` | Docker exit was negative relative to registration or missed the two second limit. Inspect both positive reports, hook duration, cleanup and shutdown; do not substitute the registry's final modification time. |
| Hook command error or timeout | Check the rendered fixture command and CI stderr. A survivor assertion can wrap this error as its cause. |
| Session registry assertion | The final registry did not contain exactly the fixture session in the expected working directory. Check the fixture event and the rendered hook registration behavior. |
| `PR_SET_CHILD_SUBREAPER failed` | The Linux subreaper setup failed. The proof cannot establish descendant cleanup and must fail. |

Preserve the failed run and fix the hook or proof in source before rerunning CI.
Do not treat container teardown as evidence that the hook cleaned up its own
children.

## Delivered regression evidence

[Pull request 3913](https://github.com/the-cloud-clockwork/agentihooks/pull/3913)
introduced two planted cases: a hook rewrites its registry after a half second,
and a hook leaves a detached `sleep 30`. The old probe used the later write and
accepted the abandoned child. Additional cases cover a failing hook with a
surviving child and a child that finishes with its hook.

Historical delivery runs:

- [Old probe with new tests: red](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39010351044).
- [Fixed branch tests: green](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39010310855).
- [Worker image proof: green](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39010309026).

These runs establish the delivered regression; qualify later source revisions
with their own CI evidence.
