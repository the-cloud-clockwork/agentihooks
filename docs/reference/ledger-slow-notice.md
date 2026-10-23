---
title: Ledger slow notice
parent: Reference
nav_order: 47
---

# Ledger slow notice

Each swarm timer pass times one ledger read and one ledger write. When two
passes in a row are slow or fail, the swarm raises the ledger slow alert. Its
notice names the passes that decided it, the ledger server's CPU and age, and
the newest change on `dev` with how many minutes ago it merged, so the master
can tell whether a recent merge is a likely cause.

The age of that newest change is taken from the pull request merge time, not
from the commit date. A merge queue squash commit carries the time the merge
group was built, which can be an hour or more before the pull request actually
merged. [PR #5105](https://github.com/the-cloud-clockwork/agentihooks/pull/5105)
moved the age to the pull request merge time, and
[PR #5122](https://github.com/the-cloud-clockwork/agentihooks/pull/5122)
replaced the commit date fallback with the time the head reached
`origin/dev` locally.

## When the alert raises and clears

| Step | Rule |
|---|---|
| Slow pass | The read or the write took over **5 seconds**, or either one timed out, gave no answer or answered with a server error |
| Raise | **2** slow passes in a row; the first notice says `The ledger server is slow`, or `The ledger probe failed` when a pass failed or the ledger watchdog could not restart the server |
| Clear | **2** passes in a row with read and write both under **4 seconds**; one such pass when the alert was raised by failures alone |

A refused request (a 4xx answer, a ledger that is gone) is recorded on the pass
but does not make it slow. The thresholds are module constants in
`scripts.swarm.ledger_probe` (`SLOW_S`, `CLEAR_S`, `PASSES`); there is no
environment setting for them.

While the alert holds, idle and stale claim checks and nudges pause. The
notice reaches three places:

- the ledger page notifications of the swarm whose pass raised it, cut to 700
  characters;
- the swarm master's inbox, at its seat address;
- a critical push alert.

## Notice wording

```text
The ledger server is slow: <slug> pass <n>: read <s> seconds and write <s> seconds, CPU <p> percent over pass, streaks before/after slow <a>/<b>, fast <c>/<d>. <second pass>. Server started <m> minutes ago. Newest on dev merged <age> minutes ago: <subject of the dev head>.
```

The clearing notice, `The ledger server answers fast again: …`, names the
passes only; it carries no `Newest on dev` part.

When the ledger watchdog could not restart the server, each pass also says
`restart failed` and the notice ends with the watchdog's own text.

When the swarm cannot tell, the parts read `CPU unknown over pass`,
`start time unknown` and `Newest on dev unknown`.

## How the newest dev merge is aged

`newest_on_dev` in `scripts.swarm.ledger_host` reads the checkout that holds
the running agentihooks code, normally the primary checkout under an editable
install:

1. **Head and subject.** It reads the newest reflog entry of
   `refs/remotes/origin/dev` with `git log -g -1 --date=unix
   --format=%H%x09%gd%x09%s`, which gives the head commit, the time that head
   reached local `origin/dev`, and its subject. Git has a 5 second limit.
2. **Pull request merge time.** It asks GitHub for the pull requests
   associated with that commit, `gh api
   repos/{owner}/{repo}/commits/<sha>/pulls`, and takes the first `merged_at`
   that is set. `gh` resolves the repository from the checkout's remote and
   has a 10 second limit.
3. **Reflog time.** When GitHub gives no merge time (no associated pull
   request, `gh` missing, not signed in, timed out, or an answer that is not a
   time), it uses the reflog entry time from step 1.
4. **Unknown.** When git fails, times out, or `origin/dev` has no reflog entry,
   the notice reads `Newest on dev unknown`.

The commit date is never used. The age is whole minutes, rounded down, at the
moment the alert raises.

### The case that led to the change

On 19 October at 21:36 UTC a ledger slow notice said the newest dev merge,
pull request 4864, had merged 68 minutes earlier. It had merged at 21:20:03
UTC, 16 minutes earlier; the dev head commit carried the merge queue build
time, 20:28:43 UTC.

## Inspect the value a notice would print

From the agentihooks checkout:

```bash
git log -g -1 --date=unix --format='%H%x09%gd%x09%s' refs/remotes/origin/dev
gh api "repos/{owner}/{repo}/commits/$(git rev-parse origin/dev)/pulls" \
  --jq 'map(.merged_at | values) | first // ""'
git log -1 --format=%cI origin/dev
~/dev/tcc-ecosystem/.venv/bin/python -c 'from scripts.swarm import ledger_host; print(ledger_host.newest_on_dev())'
```

The first line prints the head, `origin/dev@{<unix time>}` and the subject.
The second prints the pull request merge time, or an empty line when GitHub
has none. The third prints the commit date, which the notice ignores. On 23
October the dev head, pull request 6291, read:

```text
merged_at        2026-10-23T12:36:44Z
commit date      2026-10-23T12:20:09+00:00
newest_on_dev()  {'newest': 'Hive NATS members bind hub created queues and lose stream and durable create grants (#6291)', 'merged_minutes': 1}
```

The 16 minute gap between the two times is the merge queue build; the age
counts from the merge.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Newest on dev unknown` | The checkout has no `origin/dev` reflog entry, or git failed or took over 5 seconds | Run `git fetch origin dev` in the checkout; check `git log -g -1 refs/remotes/origin/dev` prints a line |
| The age is far larger than the merge's age on GitHub | The checkout has not fetched `dev` since that merge, so the notice names an older head | Fetch `dev`; the subject in the notice names the head it measured |
| The age counts from when the checkout fetched, not when the change merged | GitHub gave no merge time: the head was pushed without a pull request, or `gh` is missing, not signed in or timed out | Run the `gh api` command above from the checkout; sign `gh` in for the repository |
| The age matches the commit date of a merge queue squash | The running code predates PR 5105 | Pull `dev` into the checkout the swarm timer runs from |

## Proof

- PR 5105 red run
  [39369972237](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39369972237)
  reported 67 minutes for a merge queue commit merged 16 minutes earlier;
  green run
  [39370174626](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39370174626)
  reported 16.
- PR 5122 red run
  [39371724115](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39371724115)
  read 90 from a ninety minute old commit date; green run
  [39372042484](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39372042484)
  read 5 from the reflog entry.
- `tests/swarm/test_ledger_host.py` covers the merge time lookup, its
  command and limits, each GitHub failure falling back, and a merge queue
  commit aged from its merge.
