---
title: Hive enablement record
parent: Reference
nav_order: 77
---

# Hive enablement record
{: .no_toc }

The record of putting the first rig-grade-swarm engineer on the Anton worker hive, on the night of 19 to 20 October
2026: what was done, every step that cost time and why, and proposals ranked by the steps they remove. The record was
written for task t1265 and reviewed by a Standards and a Spec reader over four rounds; the final text is the second
comment on [issue 5111](https://github.com/the-cloud-clockwork/agentihooks/issues/5111). This page keeps that record
and adds, for each proposal, what current dev already does. "Hypothesis" marks anything the record did not prove.

To add a hive today, follow [Add a Hive](../getting-started/add-hive-quick-start.md) and the
[hives guide](../pillars/hives.md). This page explains why those paths look the way they do.

1. TOC
{:toc}

## Outcome

- The Anton worker hive joined rig-grade-swarm at 05:13 UTC on 20 October (live, role eng, max agents 1).
- The first engineer launched there at 10:53 UTC. The hive acknowledged the launch and the agent ran in the worker
  pod. Twenty eight seconds after joining the ledger it blocked itself: the worker had no GitHub login, no `gh` tool,
  and a ledger client refused the server certificate. Hive placement was turned off at 11:00.
- The launch path from the workstation to an agent process on Anton worked; an engineer there could not yet work.
- About twelve hours of wall time passed from the first live proof attempt (17:24 UTC) to the hive going live, and
  close to six more from the join to the first launch.
- Twelve agentihooks pull requests (4869, 4891, 4933, 4960, 5026, 5039, 5040, 5066, 5130, 5161, 5174 and 5198) and
  six antoncore pull requests (955, 957, 959, 965, 975 and 977) merged on the critical path. antoncore 960 was closed
  unmerged.

Most of the time went to two things:

1. **Ten defects that showed up only live.** Each surfaced on the next live attempt, after the fix for the one
   before it shipped, and each fix paid the full path again: merge queue, promotion, release and controller rollout.
2. **A wrong first route.** The controller was pointed at the swarm by name (antoncore 959). That cannot work while
   the swarm lives in the workstation Redis. The route that works is a hive member join over Tailscale.

## Defects found live

| # | Symptom | Cause | Fix |
|---|---|---|---|
| 1 | Controller logs "no live hive can run the master". | Approved account keys used dashes, hive slots underscores; the hive reported checkout paths where placement expected repository identities. | 4869, v2.17.91 |
| 2 | "Account reservation is full". | The reservation counted every workstation session against the fleet worker cap of 1. Raising the cap (antoncore 960) failed the resource budget gate. | 4891, v2.17.95 |
| 3 | The member hive could not acknowledge a launch. | Receipt and execution writes fell outside the member's Redis ACL (`NoPermissionError` on a write probe). | 4933, v2.17.99 |
| 4 | An expired, unacknowledged launch order blocked the proof swarm for good. | No code ended such an order. | Worked around with a fresh proof swarm (antoncore 965). |
| 5 | Four launches acknowledged as a bare "refused". | `--dir` carried the repository identity and init-agent refused it as a folder; the ack dropped the reason. | 5026; the ack now carries `<status>: <detail>`. |
| 6 | The first live lead failed its launch check on "joined". | The hive home had no ledger CA file. | Installed by hand. |
| 7 | Retiring that lead crashed the controller. | The handoff ask item lived in the hive's inbox world while `forget` read the controller's. | 5040; two red promotions (fixed by 5039 and 5066) held it until v2.17.104. |
| 8 | No engineer spawn for over thirty minutes. | Placement compared repositories as raw strings, so a local checkout path never matched the hive's GitHub origin. | 5174 (t1275) |
| 9 | Same spawn block. | A headless hive with room that refused on slot or repository returned a wait instead of falling back to the workstation. | 5174 (t1275) |
| 10 | Hive launches refused again. | init-agent received the workstation checkout path, the inverse of defect 5. | 5198 (t1286) |

## Timeline

All times UTC.

| When | What happened |
|---|---|
| 12 Oct | t518 written: the Anton controller joins this swarm as a hive over Tailscale. |
| 18 Oct 01:38 | A survey finds the join's missing pieces: no TLS on the ledger, no workstation Tailscale, ledger and Redis on loopback only, no ExternalSecret for a hive credential. Blocked on two operator calls. |
| 19 Oct 16:59 to 17:51 | Controller crash loop fixed (4832, Redis namespace), released in v2.17.87. antoncore 957 points the controller at a proof swarm. |
| 17:51 to 04:01 | Defects 1 to 7, each found on a live attempt and fixed through a full release. 4933 sat 89 minutes in the merge queue and was dequeued three times. |
| 03:47 | Operator order: the swarm moves to the hives first. |
| 03:50 to 04:02 | antoncore 959 and 955 merge. The controller logs that it serves the control API for a swarm "which does not exist yet". A hosted cutover is ruled out: one engineer first. |
| 04:05 to 04:26 | Tailscale. The stored automation VM auth key was rejected ("API key does not exist"; hypothesis: a one time key already used). The node join OAuth credential worked. |
| 04:12 to 04:17 | The task text still described the hosted route, so the engineer declined until the master rewrote it. |
| 04:30 | The OCI node reached the workstation at once; the on-prem worker timed out until the Tailscale container ran with accept routes. |
| 04:30 to 04:38 | A private CA and server certificate minted by hand; a compose overlay put tailnet listeners on the ledger (nginx TLS), `hive serve` and a Redis forwarder. |
| 04:38 | `hive join` by code. Credential, CA and ledger URL stored in OpenBao. |
| 04:44 to 05:44 | The deployment mode was machine wide, so a per swarm switch was built (5130). |
| 04:42 to 05:13 | Worker hive release (antoncore 975), narrowed to one account's token Secret (antoncore 977). The pod hit ImagePullBackOff once: node DNS could not resolve ghcr.io. |
| 05:13 | `agentihooks hive list` shows the hive live. |
| 05:45 to 06:40 | No spawn: the hive slot cap read 0 with no fresh quota reading. Defects 8 and 9 found; hive placement turned off. |
| 06:40 to 10:41 | 5174 and 5161 land; defect 10 found and fixed (5198). The worker pod ran a `:dev` image older than 5161 until a rollout restart at 10:26. Placement back on at 10:41. |
| 10:41 to 10:53 | No ready eng task; the master supplied one. |
| 10:53 to 11:00 | First Anton engineer launched, acknowledged, joined, blocked itself and stopped. Placement turned off. |

## Where the time went

| Cost | Time | Cause |
|---|---|---|
| Fix to release to rollout, five times | about 4 h 15 m from merge to release | Every defect was in controller or hive code, so every fix needed a promotion. Three of the five waited on red promotions caused by other changes. |
| Merge queue | about 3 h 25 m in total | Priority reshuffles dequeued 4933 three times; a conflict cost one more round. |
| One defect per live attempt | ten defects across about seventeen hours | Unit tests run on fakeredis without the member ACL, with no real init-agent and no CA file. The kind proof covered registration and ack, not a member launch through init-agent. |
| Bare "refused" ack | two replay rounds and one handoff | The launch consumer kept only the outcome status. |
| Wrong route through hosted cutover | antoncore 959 open about 9 h 20 m, then 25 m to find it did nothing | The control service waits for the named swarm in its own Redis and logs only "waits for it"; nothing told planners that a member join was the route. |
| Duplicate scope and overlap checks | two task lives | t518 was released as a duplicate, reopened, and re-checked overlap on its next life. |
| Tailscale | about 2 days blocked, then 21 m live | First framed as an operator sudo step; the documented key was dead; the working credential was recorded only in the node join. |
| Hand made hive state | about 7 restarts and manual fixes | Stale Redis host in the hive home, slot caps at 0 from a stale quota cache, herdr server down, missing ledger CA, a master login flag inherited from the parent session. |
| Seat churn | 3 lives on t1199, 7 on t518 | Long waits on releases pushed seats to the compact limit; each new life re-read its folders and re-checked scope. |
| Task text drifting from rulings | about 5 m and one declined action | Rulings arrived by inbox while the task text still said hosted ledger. |
| Pieces found only at join time | about 6 h 20 m from join to first launch | Machine wide deployment mode, a too broad token Secret, node DNS, no quota reading in the pod, raw string repository matching, no workstation fallback, the wrong checkout path, a stale `:dev` image, and no `gh` or GitHub login in the worker image. |

## The path that worked on 20 October

1. Put the swarm host on the tailnet: a Tailscale container with host networking and accept routes, using the node
   OAuth credential from OpenBao by file reference.
2. Mint a private CA and a server certificate for the tailnet address and name.
3. Expose the ledger, Redis and the join endpoint on the tailnet address with TLS; every one defaulted to loopback.
4. `agentihooks hive invite <name>` made a single use code.
5. Exchange the code with `hive join`, or hand it to the worker pod.
6. Store the credential in OpenBao and write an ExternalSecret for it and for the CA.
7. Set up the antoncore worker release: replicas 1, max agents, roles, join URL, CA secret, and the account logins.
8. On a machine hive, also by hand: `SWARM_URL`, `AGENTIHOOKS_DEPLOYMENT`, the ledger CA file, a quota balance run to
   warm the slot caps, repository checkouts, a running herdr server, and `hive set` for roles and max agents.

## Proposals ranked by steps removed

A step is one hand action taken that night or still needed by the path above. A live defect cycle counts as four
steps: fix, queue, promotion and rollout. The status column was read from current dev on 10 November 2026.

| Rank | Proposal | Steps removed | Status on current dev |
|---|---|---|---|
| 1 | **Member launch proof before merge.** Extend the worker hive kind proof to launch one stub agent through the real init-agent as a member ACL user, with a hive home holding only what join wrote, then ack, join the ledger, run one engineer step such as a `gh` read, and retire. Run it on pull requests that touch `scripts/hive`, `scripts/swarm_v2` or `deploy/worker`, or in the queue tier. | about 24 to 32 | Open. The Worker hive pod workflow still runs only on dev pushes and manual dispatch. |
| 2 | **Complete join grant, a worker that can work, and a hive that keeps its own quota fresh.** | 7 | Shipped. `hive join` writes `AGENTIHOOKS_SWARM_URL` and `AGENTIHOOKS_DEPLOYMENT` (default `distributed`) into `hive.env`; `hive refresh` and `hive add` write the ledger CA ([Ledger CA on a hive](../pillars/hives.md#ledger-ca-on-a-hive)); the worker image ships `gh`; `hive run` refreshes quota itself ([Automatic quota refresh](../pillars/hives.md#automatic-quota-refresh)). |
| 3 | **One command swarm host listen** that mints the CA and certificate and binds the ledger, Redis and `hive serve` on the tailnet address with TLS. | 4 | Partly. The compose stack ships the tailnet fronts and `deploy/compose/start-tailnet.sh` ([Compose quick start](../getting-started/compose-quick-start.md)); the certificate and key must already exist, and no command mints them. |
| 4 | **Invite straight to OpenBao and antoncore.** | 4 | Shipped. `hive invite` writes the invite into an OpenBao record (`--like` copies endpoint, bus and CA from another hive); the worker chart reads it from a Secret; `hive create` builds a Kubernetes hive from nothing through a GitOps pull request. |
| 5 | **`agentihooks hive doctor` and logged placement waits.** | 3 to 5 per hive start | Partly. Every placement refusal appears in tick output as `task <id> waits: <reason>` ([Placement waits](../pillars/hives.md#placement-waits)). No read only hive doctor command exists. |
| 6 | **Controller refuses an unreachable swarm loudly** and the overlay test fails. | 3 | Open. The control service still logs that the swarm "does not exist yet; waits for it" ([Deployment modes](deployment-modes.md)). |
| 7 | **Tailscale as a declared dependency**, with the one working credential location recorded and the dead key retired. | 3 | Partly. The tailnet fronts read `tailscale ip -4` and refuse to start without a tailnet address; Tailscale itself is not a compose service. |
| 8 | **Reap expired, unacknowledged launch orders.** | 2 | Partly. A launch reserves its slot row for at most 300 seconds, and an order a hive picks up after 300 seconds is acknowledged as failed with the reason `expired` ([Launches on a hive](../pillars/hives.md#launches-on-a-hive)). |
| 9 | **Several hives per release.** | 3 per extra hive | Shipped. `workers.replicas` stays at most 1; each `workers.hives` entry renders its own hive with its own credential ([Scaling worker hives](../pillars/hives.md#scaling-worker-hives)). |

Two process lessons need no code:

- **File the promotion fixer before anything else.** Until a red promotion is fixed, no controller fix reaches
  Anton; three of the five fixes that night waited on red promotions caused by other changes.
- **Rewrite the task text when a ruling changes the route.** An engineer is right to refuse to act against the task
  text, so the master rewrites the task as part of the ruling.

## Open at the time of the record

- A working engineer on Anton continued on tasks t518 and t1288 (`gh`, a GitHub login, the bundle and the ledger CA
  in the pod).
- The two hive proof (t1199) waited on t518; see [Two masters across two hives](../swarm-v2/two-hive-proof.md).
- The node DNS failure on the Kubernetes worker cleared after one pod delete; no cause was recorded.
