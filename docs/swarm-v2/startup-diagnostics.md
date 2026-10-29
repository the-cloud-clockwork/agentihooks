# Refused worker startup diagnostics

A worker Pod refused at startup can fail within seconds and be removed before anyone reads its logs. The deployed controller reads a failed worker Pod's startup log and exit reason, and stores them in the swarm's Redis before it deletes the Pod. The record stays readable after the Pod is gone.

Two paths delete worker Pods, and both capture first:

| Path | When it runs | Where the capture goes |
|---|---|---|
| Launch removal (`PodGrants`) | The hand to a freshly created Pod is refused | `attempt-startup-diagnostics`, one entry per execution |
| Final attempt cleanup (`Cleanup`) | The attempt is final, its outcome is recorded and its transcript is archived | The Pod's entry in `cleanup-journal`, then `removed.pods` in `attempt-retained` |

## What counts as refused

`startup()` in `scripts.swarm_v2.kubernetes.diagnostics` captures a Pod when either is true:

- the Pod phase is `Failed`;
- an init container or a main container has terminated with a nonzero exit code.

A Pod that is still running, or whose terminated containers all exited 0, captures nothing and is deleted without a record.

For a refused Pod, the capture holds the Pod phase and reason, and one entry per terminated container: its name, its full Kubernetes `terminated` state (`exitCode`, `reason`, `message`, `startedAt`, `finishedAt` and the other fields the API server reports) and its log.

The log comes from `PodClient.pod_logs`, which asks the API server for the container's last 200 lines, capped at 64 KiB, with timestamps. Bytes that are not valid UTF-8 are replaced. These limits are fixed in code.

## Launch removal

The controller removes a Pod at launch when the hand fails after the Pod exists. The hand reasons that remove the Pod are `project_unpermitted`, `config_map_exists`, `config_map_refused`, `config_map_unavailable` and a refusal from the terminal objects. Before it deletes the Pod, launch removal:

1. Reads the Pod again. A missing Pod counts as removed.
2. Keeps the Pod if its UID changed, its owner, execution or generation labels no longer match, or it is already being deleted.
3. Captures the startup diagnostics. When the Pod was refused and the runtime has no recorder, the Pod is kept. The deployed controller always has a recorder.
4. Writes the capture to `attempt-startup-diagnostics` with `HSETNX`, keyed by execution id, so the first capture for an execution is never overwritten.
5. Deletes the Pod by its pinned UID.

Launch removal runs once. When the Pod read, the log read, the Redis write or the delete fails, the hand reports `removed: false` and the Pod stays for the recovery and cleanup pass.

A stored record:

```json
{
  "pod": "swarm-worker-…",
  "uid": "6f1c…",
  "labels": {
    "swarm.agentihooks.io/controller-owner": "…",
    "swarm.agentihooks.io/execution-id": "…",
    "swarm.agentihooks.io/generation": "1"
  },
  "startup": {
    "phase": "Failed",
    "reason": "",
    "containers": [
      {
        "name": "worker",
        "terminated": {"exitCode": 78, "reason": "Error", "startedAt": "…", "finishedAt": "…"},
        "log": "2026-10-19T08:00:01.123Z worker startup refused: planted kind refusal\n"
      }
    ]
  }
}
```

## Final attempt cleanup

`Cleanup` pins every Pod and Service of the final attempt in `cleanup-journal`. For each pinned Pod that still exists with the same UID and labels, it captures startup diagnostics once, saves them on the pinned entry as `startup`, then sends the delete. A cleanup pass that restarts reads the saved capture from the journal instead of reading the log again.

When the log read fails with `ApiRefused`, `ConnectionError` or `TimeoutError`, cleanup counts the failure on the pinned entry as `log_reads`:

| `log_reads` | Behaviour |
|---|---|
| 1 or 2 | The pass fails and the Pod stays. The next controller pass retries the read. |
| 3 (`LOG_READS`) | The Pod is removed. Its capture keeps each container's exit reason and has no `log` field. |

An API server answer of 429 or 5xx raises `ConnectionError`; any other refusal, such as 403, raises `ApiRefused`. A Pod whose logs read on the first attempt keeps the full capture and records no `log_reads`.

When every step finishes, cleanup moves the journal's `removed` map into `attempt-retained` with the execution, its final record and the archive watermark, then clears the journal entry. Each entry in `removed.pods` carries the Pod name, UID, outcome (`deleted`, `absent`, `absent_after_send`, `replaced` or `unselected`) and, for a refused Pod, its `startup` capture and any `log_reads`.

## Configuration

The controller's service account needs `get` on `pods/log` in the worker namespace. The Anton controller Role grants it in the antoncore swarm RBAC manifest.

Without that permission every log read is refused with 403:

- launch removal keeps the refused Pod;
- final attempt cleanup removes it on the third pass with exit reasons only.

`LOG_READS` and the log limits have no environment or Helm setting.

## Read the records

Run these read only commands against the deployed controller. Replace `RELEASE`, `SLUG` and `EXECUTION`.

Launch removal capture:

```bash
kubectl exec deployment/RELEASE-controller -c controller -- python -c '
import sys
from scripts.swarm.store import connect
store = connect()
print(store.redis.hget(store.key(sys.argv[1], "attempt-startup-diagnostics"), sys.argv[2]))
' SLUG EXECUTION
```

Retained cleanup evidence, with each removed Pod's capture under `removed.pods[].startup`:

```bash
kubectl exec deployment/RELEASE-controller -c controller -- python -c '
import json, sys
from scripts.swarm.store import connect
store = connect()
kept = json.loads(store.redis.hget(store.key(sys.argv[1], "attempt-retained"), sys.argv[2]))
print(json.dumps(kept["removed"]["pods"], indent=2))
' SLUG EXECUTION
```

Cleanup still in progress, with `startup` and `log_reads` on each pinned Pod. The journal entry is deleted once cleanup finishes:

```bash
kubectl exec deployment/RELEASE-controller -c controller -- python -c '
import json, sys
from scripts.swarm.store import connect
store = connect()
journal = json.loads(store.redis.hget(store.key(sys.argv[1], "cleanup-journal"), sys.argv[2]))
print(json.dumps(journal["pinned"]["pods"], indent=2))
' SLUG EXECUTION
```

## Proof

The Helm kind smoke runs `deploy/helm/agentihooks-swarm/ci/startup-refusal-proof.sh`. It grants `pods/log` in the proof namespace, plants a worker that prints `worker startup refused: planted kind refusal` and exits 78, and hands it a grant for a project outside the registration so the hand is refused with `project_unpermitted`. It then checks that the Pod is gone and that `attempt-startup-diagnostics` holds phase `Failed`, exit code 78, reason `Error` and the planted log line.

Delivered by [#4876](https://github.com/the-cloud-clockwork/agentihooks/pull/4876); unit coverage lives in the Kubernetes diagnostics and log retry tests.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| A failed worker Pod stays after a refused hand | Launch removal could not read the Pod or its log, write Redis or delete; it does not retry | Launch removal logs nothing; the hand result shows `removed: false`. Wait for the attempt's final cleanup, which retries |
| Cleanup of a final attempt keeps failing | The log read is failing; the pass retries until `log_reads` reaches 3 | Read `cleanup-journal` for `log_reads`; check the controller Role for `pods/log` |
| A retained capture has `terminated` but no `log` | Three log reads failed, so cleanup recorded exit reasons only | Fix the log permission or API server error; the log of a deleted Pod cannot be recovered |
| No `attempt-startup-diagnostics` entry for a removed Pod | The Pod was not refused, cleanup removed it instead, or it was gone before launch removal read it | Read `attempt-retained` for the execution |
| A second launch of an execution did not update its record | The first capture wins (`HSETNX`) | Read the existing record; it is the first refusal |
