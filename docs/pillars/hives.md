---
title: "Hives"
parent: The Four Pillars
nav_order: 9
permalink: /docs/pillars/hives/
---

# Hives
{: .no_toc }

A hive is one machine that runs swarm agents: a workstation, a VM or a Kubernetes pod. Each hive joins the swarm
once, beats every fifteen seconds, and takes every agent type its `excluded` setting allows.

For a first hive, follow the [single machine quick start](../getting-started/single-machine-quick-start.md) or the
[Kubernetes quick start](../getting-started/kubernetes-quick-start.md), then [add a hive](../getting-started/add-hive-quick-start.md)
with one invite.

1. TOC
{:toc}

---

## Adding a machine to a hive

A machine joins a hive only through the UI hive: a UI hive adds the machine and delivers its credentials to it
over SSH. A headless hive cannot add one.

```bash
# On the UI hive
agentihooks hive add ssh://worker@lab-vm:22 \
  --url https://swarm.example.internal:8770/hive/refresh

# On the new machine, once hive add has delivered its hive.env
agentihooks hive install          # user service that runs agentihooks hive run
```

- `hive add <ssh target> --url <url> [--redis-url URL]` names the hive after its machine, the target's host: the
  first add of a host gives it the lowest free name `swarm-hive-001`, `swarm-hive-002` and so on, kept in the Redis
  hash `agentihooks-hive:machines`, and every later add of that host reuses the name. A typed label is refused. It
  refuses unless the local hive record has `ui=yes`, with
  `hive add refused: hive <id> is headless; only a UI hive adds machines`. One machine runs one hive: a herdr machine
  already saved on the same host under another label refuses the add with
  `<host> already runs hive <label>; one machine runs one hive, so grow it with more max agents`. When a hive member
  already carries the label, the add keeps that member, saves the herdr entry again if it is gone, and mints and
  delivers nothing: `--url` and `--redis-url` are ignored, and its `added <target> to herdr as <label>` line means
  the herdr entry is saved, not that a credential was delivered. `hive remove` is the only way a machine's member is
  retired, so changing the delivered URLs or repairing a lost `hive.env` is `hive remove <id>` (it drains first), then
  `hive add`. Otherwise
  it runs `herdr machine add --label <name> --remote-session default <target>` (300 second timeout) unless
  `herdr machine list --json` already holds that target or label; it mints a hive member
  named after the label, and delivers it: `ssh -o BatchMode=yes <target>` (60 second timeout) writes the member's
  `hive.env` (mode 0600) into `${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/hive.env` on the remote machine. If delivery
  fails, the minted member is revoked and the command exits 1. On success it prints
  `added <target> to herdr as <label>; its hive member is <id>`.
- `--url` is this hive's refresh endpoint as the new machine reaches it, saved into the delivered `hive.env`.
  A refusal prints `hive add refused: <reason>`, such as `herdr exited 1: <stderr>`, `ssh exited 255: <stderr>`
  or `herdr failed (<error>)`.
- The delivered `hive.env` carries the hive id, the hive URL, a ledger credential and a Redis URL for the hive's
  own Redis user: `AGENTIHOOKS_HIVE_ID`, `AGENTIHOOKS_HIVE_URL`, `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL` and
  `AGENTIHOOKS_HIVE_REDIS_URL`. `agentihooks hive refresh` posts to the URL saved there. The hive URL must be https
  unless it is loopback.
- When the hive serves a ledger CA, `hive add` delivers it beside `hive.env` and `hive refresh` rewrites it, so
  agents the new machine launches trust a ledger on a private certificate authority with no manual step; see
  [Ledger CA on a hive](#ledger-ca-on-a-hive).
- The hive endpoint is `agentihooks hive serve`, or the Compose `hive` service started with `--profile join`.
  The Compose service always serves TLS from `SWARM_TLS_DIR`; bare `hive serve` serves TLS only with `--tls-cert`
  and `--tls-key`, and refuses to serve on a non-loopback address without them. It serves only
  `POST /hive/refresh`; the endpoint's request, handler and Redis time limits, and the 503 it answers when Redis
  does not reply, are under [Hive endpoint limits](#hive-endpoint-limits).
- Each member gets the Redis user `hive-<id>`: it reads the swarm keyspace and writes only its own hive keys;
  [Member keys](#member-keys) lists what lives there.
  A launch order's receipt and acknowledgement live under those keys. The controller reads the acknowledgement and
  records the execution as working, with the hive's process target, pane, model and effort.
  Credentials sit outside what a member can read.
- `hive install` writes and enables the `agentihooks-hive` user service, which runs `hive run` and restarts five
  seconds after any exit. `hive run` publishes sessions, quota, slots and repos every fifteen seconds. After each
  beat with no order worker or resume order process still running, it reads the git revision of the checkout it
  loaded its code from;
  when that revision moved, as after a `dev` pull, it prints
  `the hive <pid> restarts on the new code: the checkout moved from <old> to <new>` and exits, so the service starts it
  again on the new code. Agents run in herdr panes outside the service's cgroup and keep running. A revision it cannot
  read, as in an image with no git checkout, keeps it running. A revision it could not read at start and reads later
  restarts it once, since the code it loaded is then unknown. The hive keeps every resume order process it starts, so
  a resume in flight finishes on the old code before the restart. An order worker or resume order process that never
  exits holds the restart until it exits.
- `agentihooks hive refresh` mints a new Redis credential; `agentihooks hive revoke <id>` deletes a member's
  ledger credential and Redis user.
- `agentihooks hive remove <name>` reverses `hive create` in one run; the name may also be the member id. It runs
  its steps in order and prints each one: drain sets `draining=yes`, so placement puts no new agent or master there,
  sends every live agent on the hive one inbox order to hand off and reports the agents that did; member revokes the
  member and its Redis user; herdr removes the machine saved under the hive's label and its host key from
  `~/.ssh/known_hosts`; registry deletes the hive record and every key below it, releases the hive name of its node and
  deletes the `hive create` state that names it; manifests opens a pull request into antoncore `dev` (`--repo`,
  `--base`) deleting the hive's Argo application, manifests folder and values overlay, and asks GitHub to merge it
  once its checks pass: the release is the label itself for a `swarm-hive-` name and `agentihooks-hive-<label>`
  otherwise; openbao deletes the OpenBao record (`k8s/agentihooks-hive-<label>`, or `--path`). `--dry-run` prints
  every step and writes nothing. The label defaults to the member's name (`--label` overrides; a rerun by member id
  after the member is revoked needs it, a rerun by name does not). After `--wait` seconds (default 3600) with agents
  still on the hive it stops with their names and leaves the hive draining; run it again to keep waiting. Every step
  skips a piece that is already gone, so a rerun finishes an interrupted remove and writes nothing after a completed
  one. `OPENBAO_URL` and `OPENBAO_TOKEN` must be set.
- `agentihooks hive list` prints one line per hive: id, `name=<name>` when the hive carries a readable name, `live` or `stale`, ui, max agents, excluded (`none` when
  empty) and its [headroom](#headroom) reading.
  `agentihooks hive show <id>` prints the full record as JSON.

A Kubernetes worker pod joins the same way, waiting for `hive add` to deliver its credential; see
[Worker hives in Kubernetes](#worker-hives-in-kubernetes). A pod can instead mount a credential that
`agentihooks hive invite <label>` writes into OpenBao, which brings a new hive up from an empty namespace; see
[Reproducible hive](../reference/reproducible-hive.md).

### Checking an added machine

`hive add` is done when herdr lists the machine, a pane on it runs a command, and its `hive.env` holds the member
the add printed. On the UI hive, with the label from the `added <target> to herdr as <label>` line:

```bash
herdr machine list                                    # swarm-hive-001 is listed and enabled
herdr --machine swarm-hive-001 pane list              # panes on the machine; pick one pane id
herdr --machine swarm-hive-001 pane run <pane id> hostname
ssh <target> 'stat -c %a "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/hive.env"'   # 600
ssh <target> 'grep -c "^AGENTIHOOKS_HIVE_" "${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/hive.env"'   # 4
agentihooks hive list                                 # the hive shows live once hive run beats
```

- `herdr machine add` prepares the remote herdr server over SSH before it saves the machine, so a listed machine
  has been reached once. herdr has no `machine status` command; `machine list` and a `pane run` that answers are
  the check.
- A pane on a worker pod runs as the worker user, so `pane run` prints the pod hostname.
- The `hive.env` that `hive add` delivers holds exactly the four `AGENTIHOOKS_HIVE_` keys and no `AGENTIHOOKS_BUS_`
  lines; bus credentials arrive only through a later `hive refresh` against a hub that signs them. The
  `AGENTIHOOKS_HIVE_ID` value is the member id `hive add` printed.
- The hive appears in `hive list` only after `agentihooks hive run` (or the worker pod's start script) beats with
  the delivered credential.

### Retired join path

The join by code ceremony is gone. Adding a machine never asks the operator to copy a code or run a command on the
new machine to fetch its credential; the UI hive mints the member and writes it over SSH.

| Retired | Replaced by |
|---|---|
| A join code printed on the UI hive and typed on the new machine | `agentihooks hive add <ssh target> --url <url>` on the UI hive |
| `POST /hive/join` on `agentihooks hive serve` | `hive serve` answers only `POST /hive/refresh`; any other path is 404 |
| The antoncore hive join service, a separate join path in Anton | `hive add`; the hive endpoint, wherever it runs, answers only refresh |
| The worker hive chart's join code mode | `workers.credentialSecret`, or a `hive.env` that `hive add` delivers to the pod |

The data plane stays: a hive endpoint answers `POST /hive/refresh`, each member keeps its ledger credential and Redis
user, and `hive refresh` mints a new Redis credential through it. The endpoint is `hive serve` on a UI hive or the
hosted endpoint a worker pod names with the chart's `workers.hiveUrl`; neither mints a member.

`agentihooks hive invite` and `agentihooks hive join` today are a different path that came later: `hive invite`
writes a member credential into an OpenBao record for a hive brought up from an empty namespace, and `hive join`
reads that invite file and posts to `/hive/refresh`. Neither restores a join endpoint. See
[Reproducible hive](../reference/reproducible-hive.md) and the
[add a hive quick start](../getting-started/add-hive-quick-start.md).

### Hive add troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `hive add refused: hive <id> is headless; only a UI hive adds machines` | `agentihooks hive show <id>`, the `ui` field | Run `hive add` on the UI hive. |
| `<host> already runs hive <label>; one machine runs one hive` | `herdr machine list --json` for another label on the same host | Raise that hive's max agents with `agentihooks hive set`, or `hive remove` it first. |
| `herdr exited 1: <stderr>` while `ssh <target>` works | Whether `HOME` differs from the passwd home | Put the host in the passwd home's `~/.ssh/config`; herdr's health check calls ssh without `-F`. |
| `ssh exited 255: <stderr>` | `ssh -o BatchMode=yes <target> true` | Fix the key or host key so ssh logs in with no prompt. The minted member was revoked; rerun `hive add`. |
| `added ... to herdr` but the machine has no `hive.env` | Whether a member already carried the label | The add kept that member and delivered nothing; `hive remove <id>`, then `hive add`. |
| A worker pod exits with `no member credential was delivered within <n>s` | The pod log for sshd start, and that `hive add` ran against its ssh address | Run `hive add` within `HIVE_DELIVERY_WAIT_S`, or mount `workers.credentialSecret`. |

## Ledger CA on a hive

A hosted ledger signed by a private certificate authority is reachable only by clients that trust that CA. A hive
installs the CA into its own home when it joins and on every refresh, so a master or engineer the hive launches
passes its ledger join and launch check without anyone copying the certificate by hand.

### Where the CA comes from

The UI hive and the hive endpoint hand out the CA they trust themselves:

1. `LEDGER_CA_PEM`, when set and not blank.
2. Otherwise the file `LEDGER_CA_FILE` names, else `ledger-ca.pem` in their agentihooks home. `agentihooks init`
   writes that file from OpenBao `k8s/agentihooks-swarm-ledger-ca`, field `ca.crt`, when `OPENBAO_URL` and
   `OPENBAO_TOKEN` are set.

The value is served only when it parses as a PEM certificate and carries no `PRIVATE KEY` block. Anything else
counts as no CA, and nothing is delivered. The endpoint reads its CA once, when `hive serve` starts: after changing
the CA source, restart `hive serve`, or roll the `hive-join` pod on Helm. While `hive.join.serveLedgerCa` is `true`
(the default), the Helm `hive-join` deployment sets `LEDGER_CA_PEM` from the `ca.crt` key of
`ledger.tls.secretName`. Set it to `false` for hives that trust another ledger's CA.

```yaml
# values.yaml: the hive endpoint serves the ledger CA from this secret
ledger:
  tls:
    secretName: agentihooks-swarm-ledger-tls   # must hold ca.crt
hive:
  join:
    serveLedgerCa: true
```

```bash
# A workstation UI hive serving its own CA file
LEDGER_CA_FILE=~/.agentihooks/ledger-ca.pem agentihooks hive serve \
  --tls-cert swarm.crt --tls-key swarm.key
```

### Where the hive writes it

| Step | What it writes | Target file |
|---|---|---|
| `hive add` | The UI hive's served CA, over the same `ssh -o BatchMode=yes` session family that delivers `hive.env`, after `hive.env` lands | `$LEDGER_CA_FILE` in the remote shell when set, else `${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/ledger-ca.pem` |
| `hive refresh` | The `ledger_ca` field of the endpoint's answer | `LEDGER_CA_FILE` when set, else `ledger-ca.pem` in the hive home (`AGENTIHOOKS_HOME`, else `~/.agentihooks`) |

- Both writes stage a new file beside the target and rename it into place, so a reader never sees half a
  certificate. Missing parent folders are created.
- A refresh answer without a valid `ledger_ca` leaves the existing file as it is: an endpoint that stops serving the
  CA never removes one a hive already holds.
- `hive add` for a label that already has a member delivers nothing, the CA included. Repair a missing CA with
  `agentihooks hive refresh` on that machine.
- A failed CA delivery is a failed `hive add`: the minted member is revoked and the command exits 1 with
  `hive add refused: ssh exited <code>: <stderr>`. The `hive.env` written just before stays on the machine with the
  revoked credential until the next `hive add` replaces it.

### Which CA the refresh request trusts

`hive refresh` and the hive join post to the endpoint with a trust each picks for that request from the hive's own
environment:

| Hive environment | The request trusts |
|---|---|
| `LEDGER_CA_PEM` set and not blank | Only that CA |
| `LEDGER_CA_FILE` set and the file exists | The system roots plus that file |
| `LEDGER_CA_FILE` unset and `ledger-ca.pem` exists in the hive home | The system roots plus that file |
| None of these | The system roots alone |

A named `LEDGER_CA_FILE` that does not exist yet counts as no CA here, even when `ledger-ca.pem` exists, unlike for
agents, where a missing named file is an error. With the system roots alone, a first refresh succeeds only when the
endpoint's certificate chains to a system root or the endpoint is plain http on loopback; an endpoint signed by a private CA needs `LEDGER_CA_PEM` for that
first command. A whitespace only `LEDGER_CA_PEM` falls through to the file rows.

Both failures print one message and exit 1, `hive <refresh|join> refused: the hive at <url> is unreachable (<reason>)`:

| Reason | Cause |
|---|---|
| `<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>` | The chosen trust cannot verify the endpoint's certificate |
| Any other reason | The endpoint cannot be reached or did not answer in time |

```bash
# A first refresh against an endpoint signed by a private CA: trust it for this one command
LEDGER_CA_PEM="$(cat swarm-ca.crt)" agentihooks hive refresh

# Later refreshes trust the file the first one wrote, when the endpoint serves its CA
agentihooks hive refresh
```

The file a refresh writes is `LEDGER_CA_FILE` when set, else `ledger-ca.pem` in the hive home, and only when the
endpoint serves a CA (see [Where the CA comes from](#where-the-ca-comes-from)).

Agents the hive launches inherit the `hive run` environment and resolve the CA file from it: `LEDGER_CA_FILE`, else
`ledger-ca.pem` in `AGENTIHOOKS_HOME`, else in `~/.agentihooks`. That must name the path `hive add` (the remote login
shell) and `hive refresh` wrote to; with neither variable set anywhere, all three agree on the default. Every ledger
client trusts the system roots plus that file, or only `LEDGER_CA_PEM` when that is set; see
[Hosted ledger authentication](../reference/hosted-ledger-auth.md). A hive whose environment sets `LEDGER_CA_PEM`
ignores the delivered file. Neither variable is written into `hive.env`.

### How refresh trusts the endpoint

`agentihooks hive refresh` posts to the hive URL saved in `hive.env` over https. The same request runs on its own
when the swarm Redis client finds the hive Redis credential refused: it refreshes the member and retries with the new
password. Both verify the endpoint's certificate the way every ledger client does:

| On the hive | The refresh trusts |
|---|---|
| `LEDGER_CA_PEM` set and not blank | Only that certificate |
| `LEDGER_CA_FILE` set and the file exists | The system roots plus that file |
| `LEDGER_CA_FILE` set and the file is missing | The system roots alone, as in the last row |
| Neither variable set, `ledger-ca.pem` in the hive home | The system roots plus that file |
| Neither variable set, no `ledger-ca.pem` yet | The system roots alone |

The last row covers a hive's first refresh, which is the one that delivers the CA. An endpoint on a private
certificate authority therefore needs the CA on the hive before that refresh: a delivered `ledger-ca.pem`, or
`LEDGER_CA_PEM` set for the command. A Kubernetes worker pod places its mounted `LEDGER_CA_PEM` beside the system
roots as `ledger-ca.pem` before it joins, so its own refresh trusts the hive endpoint with no manual step.

```bash
# On a hive whose endpoint uses a private CA that has not delivered ledger-ca.pem yet
LEDGER_CA_PEM="$(cat ca.crt)" agentihooks hive refresh
```

### Checking a hive's CA

```bash
# On the hive: the file agents trust, and the certificate it holds
ca="${LEDGER_CA_FILE:-${AGENTIHOOKS_HOME:-$HOME/.agentihooks}/ledger-ca.pem}"
openssl x509 -in "$ca" -noout -subject -enddate

# Rewrite it from the hive endpoint
agentihooks hive refresh

# The ledger's certificate verifies against it
curl -sS --cacert "$ca" -o /dev/null -w '%{http_code}\n' https://swarm.example.internal/healthz
```

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A master or engineer launched on the hive fails its ledger join with `certificate verify failed` | No `ledger-ca.pem` in the hive home: the hive joined before its UI hive or endpoint served a CA | Serve the CA there (see [Where the CA comes from](#where-the-ca-comes-from)), then run `agentihooks hive refresh` on the hive |
| `hive refresh` succeeds but the file stays absent or stale | The endpoint serves no valid CA: `LEDGER_CA_PEM` and its CA file are both unset, empty, not a certificate or carry a private key; or the endpoint started before its CA source changed; or `hive.join.serveLedgerCa` is `false` | Fix the endpoint's CA source and restart `hive serve` or roll the `hive-join` pod; on Helm, check `ledger.tls.secretName` names a secret with `ca.crt` |
| Agents still fail after the file is correct | The `hive run` environment sets `LEDGER_CA_PEM` to another CA, or `LEDGER_CA_FILE` to another path than the hive wrote | Unset the stray variable, or set it to the same value where `hive refresh` and `hive run` both read it, then restart the hive service |
| `LEDGER_CA_FILE names no file: <path>` | `LEDGER_CA_FILE` is set and the file is missing | Run `agentihooks hive refresh` with the same `LEDGER_CA_FILE`, or unset it to use `ledger-ca.pem` in the home |
| `the ledger CA file <path> holds no valid certificate: <reason>` | The file exists but holds no certificate the client can load. `hive refresh` stops on the same error before it writes | Remove the file, or set `LEDGER_CA_PEM` for that one command, then run `agentihooks hive refresh` |
| `hive add refused: ssh exited 1` after the `hive.env` write | The remote shell could not create the CA folder or file | Fix the permissions on the remote home or the `LEDGER_CA_FILE` folder, then `hive add` again |
| `hive refresh refused: the hive at <url> is unreachable (<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>)` | The hive endpoint's certificate is signed by a CA the refresh does not trust: no `LEDGER_CA_PEM` and no CA file, either because `ledger-ca.pem` is absent from the hive home or because `LEDGER_CA_FILE` names a missing file; or a release from before the refresh trusted the ledger CA, which used the system roots alone | Upgrade the hive to a current release, place the CA (see [How refresh trusts the endpoint](#how-refresh-trusts-the-endpoint)), then run `agentihooks hive refresh` |

## Repos and checkouts

Each heartbeat reports the repos the hive can work in. `hive run` scans one folder, `HIVE_REPO_ROOT`, default
`~/dev`: every direct child that is a git checkout, and every checkout one level below a child that is not one.
Worktrees count; their origin is read from the shared git directory.

For every checkout the hive reports two names, so a task matches whichever form its swarm uses:

- the absolute path, such as `/home/me/dev/agentihooks`;
- the origin identity, the `origin` remote as lowercase `host/owner/name`, such as
  `github.com/the-cloud-clockwork/agentihooks`. A checkout with no `origin` remote reports only its path.

```bash
# Workers keep their clones under /srv/repos instead of ~/dev
HIVE_REPO_ROOT=/srv/repos agentihooks hive run

# The repos the swarm sees for one hive
agentihooks hive show lab-vm        # the "repos" list
```

### Matching a repo across machines

A swarm may name its repo by a local checkout path even though a worker keeps its clone elsewhere. Launch
selection reads the local checkout's `origin` and matches that identity against the hive's advertised `repos`.
This applies to worker and master launch selection. The two machines do not need matching home directories.

For example, these records describe the same repository:

| Machine | Checkout or advertised repo |
|---|---|
| Workstation, swarm repo | `/home/me/dev/agentihooks` |
| Headless worker, checkout | `/home/worker/dev/agentihooks` |
| Headless worker, advertised origin | `github.com/the-cloud-clockwork/agentihooks` |

If the workstation checkout's origin is `https://github.com/the-cloud-clockwork/agentihooks.git`, the worker
qualifies through `github.com/the-cloud-clockwork/agentihooks`. Selection sends the matched origin with the launch,
so the worker resolves its own checkout. When several checkouts share that origin, it uses the first by path.
A directly matched path needs no origin translation.

```bash
git -C /home/me/dev/agentihooks remote get-url origin
agentihooks hive list
agentihooks hive show <worker-hive-id>
```

Compare the normalized origin with the worker's `repos` list. An origin match needs a readable local checkout
and a worker heartbeat advertising that identity. A missing or unreadable origin cannot establish that match;
check the advertised path as well. A different owner or host is a different origin, even when the repo folder
has the same name. Placement refuses when no eligible hive lists a matching repo, with
`no hive for <role> lists the repo <repo>`. Set `HIVE_REPO_ROOT` to the parent of the worker checkouts before
starting its daemon, and allow its next heartbeat to publish them; listing a repo does not clone it.

`swarm prepare` and `swarm publish` search the same `HIVE_REPO_ROOT` checkouts by repository name when a task
repository entry is not a directory on this host, such as a path recorded on another host; see
[Repositories recorded on another host](swarm.md#repositories-recorded-on-another-host).

### Launch directory on a hive chosen by origin

A hive chosen by origin launches the agent in its own checkout with that origin, never in the swarm's
workstation path. Worker and master launches follow the same rule:

1. Selection marks a hive that qualifies only through the origin identity and carries that identity with the
   launch as the `hive_origin` field of the launch order.
2. The hive's order consumer launches with `hive_origin` as the repo.
3. The hive resolves the repo to the first checkout under `HIVE_REPO_ROOT`, by path, whose `origin` matches,
   and passes it to `init-agent --dir`.

A hive that lists the swarm path itself gets no `hive_origin` field and launches in that path unchanged.

With the records in [Matching a repo across machines](#matching-a-repo-across-machines), an engineer placed on
the headless worker launches with `--dir /home/worker/dev/agentihooks`, never `/home/me/dev/agentihooks`. No
setting selects the launch directory; it follows from `HIVE_REPO_ROOT` and the checkouts' `origin` remotes on
the hive.

```bash
# On the hive: each direct child checkout and its origin remote
for d in "${HIVE_REPO_ROOT:-$HOME/dev}"/*/; do
  [[ -e "$d/.git" ]] || continue
  printf '%s %s\n' "$d" "$(git -C "$d" remote get-url origin 2>/dev/null)"
done
```

The loop lists the direct children of `HIVE_REPO_ROOT`. The hive also scans one level into each child that is
not a checkout, and compares each remote after normalizing it to lowercase `host/owner/name`, so
`https://github.com/the-cloud-clockwork/agentihooks.git` matches `github.com/the-cloud-clockwork/agentihooks`.

When the hive has no checkout with the carried origin, it passes the origin identity itself to
`init-agent --dir`, which refuses the launch with `directory does not exist: <path>`. `<path>` is the identity
joined to the `init-agent` base directory (`RUN_CLAUDE_BASE_DIR`, else `~/dev` when it exists, else `$HOME`),
such as `/home/worker/dev/github.com/the-cloud-clockwork/agentihooks`.

### Swarm commands use the launch checkout

Every swarm launch, on a hive or on the workstation, sets `AGENTIHOOKS_SWARM_REPO` in the agent's environment to
the directory it passed to `init-agent --dir`, next to `AGENTIHOOKS_SWARM`, the swarm slug. The launch resolves
that directory from the swarm repo:

| Swarm repo | `--dir` and `AGENTIHOOKS_SWARM_REPO` |
|---|---|
| An origin identity, such as `github.com/the-cloud-clockwork/agentihooks` | The first checkout by path under `HIVE_REPO_ROOT` (default `~/dev`, one level of children of a folder that is not a checkout included; see [Repos and checkouts](#repos-and-checkouts)) whose `origin` matches, else the identity unchanged |
| A local path, such as `/home/me/dev/agentihooks` | The path unchanged |

These swarm commands read their repository from that variable instead of the swarm repo:

| Command | What it reads from the checkout |
|---|---|
| `swarm <slug> prepare` | The repository it creates the task worktree from |
| `swarm <slug> publish` | The repository it commits, gates and pushes from |
| `swarm <slug> block "<why>"` | The pushed task branch it records on the task before blocking |
| `swarm <slug> block --dev-red "<why>"` | The GitHub repository whose latest finished dev Tests run must have failed |

A command uses `AGENTIHOOKS_SWARM_REPO` only when the variable is set and not empty and `AGENTIHOOKS_SWARM`
names the slug the command was given. Otherwise it uses the swarm repo from the swarm config, as before. An agent
of one swarm that runs a command for another swarm therefore reads that swarm's repo, never its own checkout. On
the workstation, a swarm repo given as a local path resolves to that same path, so those commands read
what they read before.

With the records in [Matching a repo across machines](#matching-a-repo-across-machines), an engineer on the
headless worker runs with:

```bash
# In the agent's pane on the hive
printf '%s %s\n' "$AGENTIHOOKS_SWARM" "$AGENTIHOOKS_SWARM_REPO"
# rig-grade-swarm /home/worker/dev/agentihooks

# Both read /home/worker/dev/agentihooks, never the workstation path the swarm config names
agentihooks swarm rig-grade-swarm block "waiting on a token"
agentihooks swarm rig-grade-swarm publish --message "Fix the parser" -- scripts/parser.py
```

The variable belongs to the launch. Set it by hand only in a shell that has no swarm launch, such as a recovered
pane, and only to a checkout of the swarm's repository; a wrong path sends the commands to the wrong repository.

| Symptom | Check | Fix |
|---|---|---|
| `swarm block` on a hive records no pushed branch on the task and reports no error | `echo "$AGENTIHOOKS_SWARM_REPO"` in the agent's pane | The agent started before this behavior shipped, or outside a swarm launch, so the command read the workstation path, which does not exist on the hive. Release the task so the swarm relaunches it, or export the hive checkout path and the swarm slug in that pane |
| `swarm block --dev-red` on a hive fails with `cannot read the dev Tests runs` naming a missing directory | `echo "$AGENTIHOOKS_SWARM_REPO"` in the agent's pane | Same cause and fix as the row above |
| A command reads the swarm config repo although the variable is set | `echo "$AGENTIHOOKS_SWARM"` against the slug in the command | Run the command with the agent's own swarm slug |
| `AGENTIHOOKS_SWARM_REPO` is an origin identity such as `github.com/owner/name` | The hive's checkouts under `HIVE_REPO_ROOT` and their `origin` remotes | No checkout matched at launch, so `init-agent` refused it with `directory does not exist`. See [Launch directory on a hive chosen by origin](#launch-directory-on-a-hive-chosen-by-origin) |

## Hive endpoint limits

`agentihooks hive serve` answers `POST /hive/refresh` (a ledger credential for a new Redis password) on one thread
per request; any other path answers 404, like any unknown path. Every limit in this table is a constant in the
service; no flag or environment variable changes one.

| Limit | Value | Effect |
|---|---|---|
| Request body | 4096 bytes | A missing, oversize or non JSON body answers 400. |
| Request socket timeout | 10 seconds | Bounds the TLS handshake and each read of the request. |
| Concurrent handlers | 32 | A further connection waits until a handler gives its slot back. |
| Request deadline | 30 seconds | The service shuts down the client socket of a request older than this. |
| Redis socket timeout | 5 seconds | Bounds each socket operation of every Redis call the service makes, not a whole request: a request that makes several calls can hold its slot longer, and a Redis that keeps sending within 5 seconds is not cut off. A failed call is never retried. |

The request deadline does not interrupt a handler waiting on Redis, which holds a separate connection, so the
Redis client's own timeout is what frees a handler while Redis stalls. The swarm Redis client reads with a
ten-second timeout; the hive endpoint lowers it to five seconds. A stalled call then raises after five seconds, the
handler answers 503 and its slot is free for the next request.

The service applies the bound when `agentihooks hive serve` builds the server. It sets the socket timeout on the
Redis client it was given, sets the retry count to zero and drops every connection opened before, so no pooled
connection keeps the earlier timeout. The swarm Redis client's three-second connect timeout is unchanged. It applies
to each address tried, not to name lookup, and a `socket_connect_timeout` query argument in the Redis URL overrides
it. A Redis that refuses the connection answers 503 at once; one that accepts it and stays silent answers after five
seconds.

### Answers

| Status | Body `error` | Cause |
|---|---|---|
| 200 | none | The credential was accepted; the body carries the grant. |
| 400 | `the body must be a JSON object with a string ledger_credential` | The body is missing, over 4096 bytes, not JSON, or lacks that string field. |
| 403 | the refusal reason, such as `the ledger credential belongs to no hive member` | The credential is not valid, or Redis returned an error other than a timeout, a lost connection or a refused login while the service refreshed the member. |
| 404 | `not found` | The path is not `/hive/refresh`. |
| 503 | `the hive Redis did not answer; try again` | A Redis call timed out, the connection to Redis was refused or dropped, or Redis refused the service's own login (`WRONGPASS`, `NOAUTH`). |

The 503 text is fixed and carries no Redis detail. The service writes no request log, so for an answered request the
client's reply is the only record.

### What a client sees

```text
$ agentihooks hive refresh
hive refresh refused: the hive Redis did not answer; try again
```

The command exits 1. The bound is per Redis call. The client may report the timeout instead of the 503:

```text
hive refresh refused: the hive at https://swarm.example.internal:8770 is unreachable (timed out)
```

A hive whose own Redis credential was refused refreshes it through this service. When the service answers 503, the
swarm Redis client raises `AuthenticationError` with `the hive Redis credential was refused and the hive refused a
refresh (the hive Redis did not answer; try again)`. The `hive` commands print a `refused` line only for their own
refusals. This error comes from the swarm's `redis_client()` helper, so commands that open the swarm Redis, such as
`hive list`, `hive set`, `hive show` and `hive run`, end with a traceback whose last line carries this text.

### Check the bound against a live service

A made-up credential reaches Redis through the service without touching a real member. Add `--cacert` for a
private certificate authority.

```bash
curl -sS -m 15 -w '\nstatus %{http_code} in %{time_total}s\n' \
  -d '{"ledger_credential": "not-a-real-credential"}' https://swarm.example.internal:8770/hive/refresh
```

| Redis state | Output |
|---|---|
| Answering | `{"error": "the ledger credential belongs to no hive member"}` and `status 403` at once |
| Accepting the connection, never replying | `{"error": "the hive Redis did not answer; try again"}` and `status 503` after about 5 seconds |

The CI test `test_a_request_stalled_inside_redis_answers_and_frees_its_slot_within_the_bound` runs this route
against a listener that accepts connections and never replies, with one handler slot and a Redis timeout of one
second: it answers 503 and the slot is free within twice that timeout.

### Hive endpoint troubleshooting

| Symptom | Reading | Action |
|---|---|---|
| `hive refresh` answers 503 | The refresh did not complete, or completed without the reply reaching the client. | Run `agentihooks hive refresh` again. Each success replaces the previous Redis password, and the command takes a per-host lock so two callers do not retire each other's password. |
| Every refresh answers 503 | Redis is stalled, unreachable or refusing the service's connection. | From the swarm host, run `agentihooks hive list`. It opens its own Redis connection with a 3-second connect and 10-second read timeout, so an error or a pause there points at Redis and not at the hive endpoint. Fix Redis, then repeat the request. |
| 403 text `Redis refused the refreshed member (...)` | Redis returned an error other than a timeout, a lost connection or a refused login; see Answers. | Read the detail in the message. |

## Member keys

A member's Redis user `hive-<id>` reads every key under the swarm root. It writes the member's own hive key and the
keys below it, its own hive and scheduler state records, and the fixed list of swarm and seat keys a worker engineer
writes, described in [Worker keys](#worker-keys). With the default root `agentihooks` (set by
`AGENTIHOOKS_SWARM_KEY_PREFIX`), the member `lab-vm` gets these key patterns:

```text
%R~agentihooks:*                                  read every swarm key
~agentihooks:hive:lab-vm                          write the hive record
~agentihooks:hive:lab-vm:*                        write everything below it
~agentihooks:swarm:state:hive-lab-vm:*            write the member's hive state record
~agentihooks:swarm:state:scheduler-lab-vm:*       write the member's scheduler state record
~agentihooks:swarm:*:claim:*                      write task claims, the first worker key
~agentihooks:swarm:*:findings                     write finding verdicts, another worker key
```

The worker key list holds one such pattern per key; [Worker keys](#worker-keys) lists them all.

Every other write, including another member's keys, the controller's inbox and the launch slot rows, is refused
by Redis with `NOPERM`. Apart from the worker keys, every piece of state a member process writes lives below its
own hive key:

| State | Where a member keeps it | Who reads it there |
|---|---|---|
| Launch order receipt and acknowledgement | `<root>:hive:<id>:...` | The controller, which records the execution as working. |
| Inbox items, sequence counters, delivery owners, seen marks and the master pool | `<root>:hive:<id>:inbox:...` | The member's own agents; the controller routes sends to the member's agents there and relays mail out of it. |
| Session readings: heartbeat, prompt, session model, progress, wait, waited, woke, wait history and stop blocks | `<root>:hive:<id>:swarm:...` | The controller's idle and wait checks, and the progress and stop gates, for agents placed on that member. |
| Sessions report, including the executions it runs | `<root>:hive:<id>:sessions` | The controller's launch slot refresh, and every launch reservation's account count; see [Sessions report and the account cap](#sessions-report-and-the-account-cap). |

### Which keys a process uses

A process counts as the member only while it talks Redis as that member: `AGENTIHOOKS_HIVE_ID` is set and the
Redis URL it resolves is `AGENTIHOOKS_HIVE_REDIS_URL`. `hive add` delivers both in `hive.env`; a process resolves the
hive Redis URL only when `AGENTIHOOKS_DEPLOYMENT` is `compose` or `distributed`. Any other process, the
controller included, uses the controller keys under the plain root.

The controller finds an agent's member from the agent's execution record: the hive it was placed on, when that hive
is in the hive index and holds a member record. An agent on any other host, and every agent in local mode, keeps
its inbox and session readings in the controller keys under the plain root. Those keys are the same as before
member scoping, so a local install and a single host install see no change.

### Member id after a refused credential

When a hive process replaces its Redis credential it takes the member id from the same place, so every key it
builds carries the id of the member its Redis user `hive-<id>` was made for. Outside ledger local mode, where a
process uses `AGENTIHOOKS_SWARM_REDIS_URL` and none of this applies, a process connects in this order:

1. It reads `hive.env` in the hive home (`AGENTIHOOKS_HOME`, else `~/.agentihooks`). When that file names a Redis
   user other than the one in the process environment, as after `hive create` or a rejoin replaced the member, the
   process takes `AGENTIHOOKS_HIVE_ID` and `AGENTIHOOKS_HIVE_REDIS_URL` from the file. `hive add` writes both into the
   new host's file, and every refresh writes both into the refreshing host's file.
2. It logs in with `AGENTIHOOKS_HIVE_REDIS_URL`.
3. When Redis refuses that credential, it tries the URL saved in `hive.env`. If that one logs in, the process takes
   the member id saved beside it.
4. Otherwise it runs `hive refresh` under the per-host refresh lock. The refresh answer names the member the new
   credential belongs to; the process writes both values into `hive.env` and into its own environment.

The hive daemon (`hive run`) reads the member id it keys under after it connects. Its heartbeat, headroom
publisher and scheduler keys therefore use the recovered member on the first start. A daemon on a hive recreated
by `hive create` writes its scheduler under its own member:

```text
<root>:hive:<id>:scheduler:<machine>:<suffix>      for example agentihooks:hive:lab-vm:scheduler:pod-0:entries
```

`<machine>` is the host name. A process without `AGENTIHOOKS_HIVE_ID` writes the same keys under
`<root>:scheduler:<machine>`; in ledger local mode the scheduler keeps its local key.
The member ACL is not widened: a member still writes no other member's hive keys.

Check which member a hive daemon keys under:

```bash
# On the swarm host: the hive's member id
agentihooks hive list

# As an admin user: that member's scheduler keys exist, and no other member's id carries this machine
redis-cli --user <admin> --askpass --scan --pattern 'agentihooks:hive:<id>:scheduler:*'
redis-cli --user <admin> --askpass --scan --pattern 'agentihooks:hive:*:scheduler:<machine>:*'

# The hive service has not restarted since it started
systemctl --user show agentihooks-hive -p NRestarts
```

### Worker keys

An engineer on a joined hive runs its swarm commands as the member's Redis user. Those commands write swarm and seat
keys outside the member's hive key, so the member user also writes the patterns listed in
`scripts.hive.auth.WORKER_KEYS`, each below the swarm root and for every swarm slug. The list holds only keys a traced
worker command writes: running `swarm status`, `wait`, `ready`, `park` and `done` as a member user on Redis 7 and
granting each key Redis refused until all five passed gave the list.

| Command | Keys it writes under `<root>:` |
|---|---|
| `agentihooks swarm <slug> done` | The task claim `swarm:<slug>:claim:<task>`, released when the task closes. |
| `done`, `ready`, and `park` when it retires the agent | Agent and execution records `swarm:<slug>:agents`, `executions` and `hosts`, the agent's `desired-routes` and `state-since`, and the seat exit mark `seat-of:<agent>:exit`. |
| `ready` and `park` when it hands off | `swarm:<slug>:ready`, `restart-causes`, `transfers`, the handoff text, seat and envelope `handoff:*`, `handoff-seat:*` and `handoff-envelope:*`, and the seat recaps `seat:<seat>:recaps`. |
| `agentihooks swarm <slug> status` | Finding verdicts `swarm:<slug>:findings`, check state `checks:*`, merge stall state `merge-stall` and `merge-stall-notice`, and `hive-placement-waits`. |
| `agentihooks swarm <slug> wait --on ...` | The prerequisite wait record `swarm:<slug>:prerequisite-waits`, which it clears and writes. A wait without `--on` writes only below the member's own hive key. |
| `agentihooks swarm <slug> block` | The same claim release, agent records and seat exit mark as `done`, plus `swarm:<slug>:dev-red` and, for a stacked prerequisite, `prerequisite-waits`. |
| `agentihooks swarm <slug> learned` | The seat's learned notes `seat:<seat>:learned`. |
| A refused execution record write | The identity conflict counter, kept in `swarm:state:counters:*`. |

The rest of the list covers records the same commands write through the execution registry, fencing and gate code:
`lease-cache`, `swarm:state:fencing:*`, `swarm:*:fence:*`, `execution-publishing`, the
Codex `codex-transport`, `codex-endpoints`, `codex-openings` and `codex-viewers`,
`stage-covers`, and the task gate lifts `gate-lifts:tasks:*` and `gate-lift-drops`.

The list also keeps `swarm:*:identity-conflicts`, the key the identity conflict counter used before it moved into
`swarm:state:counters:*`. Code only reads it, as a fallback when the counters record holds no entry.

The member user stays refused on every other swarm key. Writes that fail with `NOPERM` include:

```text
agentihooks:swarm:<slug>:config                 swarm settings
agentihooks:swarm:<slug>:claim-journal:<task>   claim journals
agentihooks:swarm:<slug>:task-authority:<task>  task authority
agentihooks:swarm:<slug>:tick-lock              the tick lock
agentihooks:swarm:<slug>:launches               launch rows
agentihooks:swarm:<slug>:gate-lifts:agents:*    agent gate lifts
agentihooks:swarm:<slug>:dev-red-runs           red run history
agentihooks:swarm:index                         the swarm index
agentihooks:seat:<seat>:culture                 seat culture
agentihooks:names:<agent>                       agent names
agentihooks:inbox:*                             the shared inbox
agentihooks:hive:<other-id>:*                   another member's keys
```

#### Applying the grant

No setting turns the worker keys on. Redis applies them to a member user when the user is written:

- `agentihooks hive add` and `agentihooks hive join` write the user with the current list.
- `agentihooks hive refresh`, run on the member, writes it again with a new password.
- `agentihooks hive serve`, the hive endpoint, rewrites every indexed member's user with the current list at start
  and keeps each password, then saves the ACL file.

After an upgrade that changes `WORKER_KEYS`, restart the hive endpoint, or run `hive refresh` on each member. Read the
patterns a member holds, as an admin user on the swarm Redis:

```bash
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" ACL GETUSER hive-lab-vm
```

The `keys` field lists the member's own patterns and one `~agentihooks:<pattern>` entry per worker key, such as
`~agentihooks:swarm:*:claim:*` and `~agentihooks:swarm:*:findings`. Check a write from the member:

```bash
. ~/.agentihooks/hive.env
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" HSET agentihooks:swarm:<slug>:findings probe '{}'   # (integer) 1
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" HDEL agentihooks:swarm:<slug>:findings probe         # (integer) 1
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" HSET agentihooks:swarm:<slug>:config probe 1        # (error) NOPERM ...
```

#### Limits of the worker keys

- Redis ACLs scope by key pattern, never by swarm, seat or agent. A member user can write the worker keys of every
  swarm and every agent, not only the agents placed on it.
- The ledger issues a hive agent token when the agent's record in `swarm:<slug>:agents` names the member as its
  hive, and records the token from the member's holder record in the hive credential store under `<root>-hive`,
  which a member user can neither read nor write. Agent records are a worker key, so the hive field check alone no
  longer stops a member from naming itself on another agent's record.
- A worker command that writes a key outside the list still fails with `NOPERM`. The list grows only by a change to
  `WORKER_KEYS` that names the command writing the key.

### Mail across members

- **Into a member.** A controller send to an address that a member runs, by name or by the seat it occupies, is
  written into that member's inbox keys. The member's agents read and answer it there.
- **Out of a member.** A member writes mail to any other address, such as `master@<slug>` on the swarm host, in its
  own inbox keys, where it waits. Every swarm tick relays each waiting item to its address's inbox with the same item
  id and withdraws the member's copy with the reason `relayed to its address's inbox`. A relay retry after a lost
  response finds the id already sent and only withdraws the copy.
- **Refused sender.** The relay withdraws an item whose sender the member does not run, with the reason
  `refused: the hive member that sent it does not run its sender`. A member cannot send in another agent's name.
- **Master pool.** A member pools mail across live masters only while every live master's inbox sits in the same
  keys as the sender's. When their inboxes span the controller and a member, or two members, the item goes to its
  address unpooled.

The flow for an engineer that a member runs and the master on the swarm host:

```bash
# On the member: the engineer reports to the master; the item waits in the member's inbox keys
agentihooks msg send master@rig "the pull request is green and queued"

# On the swarm host, after the next tick relays it
agentihooks msg inbox              # run as the master: the item shows with the same id
agentihooks msg reply <id> "thanks, take the next task"
# The reply is written into the member's inbox keys, where the engineer reads it
```

### Seat mail follows its occupant

A send to a seat writes into the inbox of the agent occupying it at send time. A send to an empty seat, such as
`dispatcher@<slug>` between two dispatchers, lands in the controller keys. When the next occupant starts on a
member, its mail would sit in keys it never reads.

Every swarm tick's exit sweep moves that mail. For each live agent that is the current occupant of a lane seat
(`eng-N@<slug>`, `ci-N@<slug>`, `plan-N@<slug>`) or the dispatcher seat (`dispatcher@<slug>`), the sweep moves every
open item addressed to the seat from any other reachable inbox into the inbox that holds the agent: the member keys
of the hive it runs on, or the controller keys when it runs on the swarm host. A moved item:

- keeps its id, sender, text, history and ledger entry;
- turns pending at the same seat address, so the occupant's next inbox read delivers it;
- gains a history entry by `swarm` with the reason `moved to hive <id>, where <agent> runs <seat>`, or
  `moved to the controller, where <agent> runs <seat>`.

Master seats never move: their mail stays with the master pool of its inbox. An inbox whose Redis times out or
drops is skipped for that pass, and the sweep moves its mail on a later tick.

To see a moved item, run on the hive where the occupant runs, as that occupant:

```bash
agentihooks msg inbox            # the item shows pending, with its original id
agentihooks msg read <id>        # its history ends with the move and its reason
```

### Launch slot rows

Launch slot rows count how many executions hold each account slot. Only the controller writes them; a member never
touches them. Reserve and acknowledge run in the controller, and every tick the controller refreshes the rows of
member executions from what each member reports:

1. A launch reserves a row for at most 300 seconds while the launch order is unacknowledged.
2. The controller reads the member's acknowledgement and marks the row live, kept for 45 seconds.
3. Every swarm tick reads the sessions report of each live hive, a heartbeat under ninety seconds old, and keeps the
   live rows of the executions it reports alive for two ticks, 120 seconds at the default tick. Only executions
   whose agent record places them on that same hive count, so a hive cannot hold a slot for an execution it does not
   run.
4. A row whose hive stops reporting the execution, or stops beating, is no longer refreshed. It lapses within two
   ticks and the slot is free for the next launch.

`AGENTIHOOKS_CONTROLLER_TICK_SECONDS` sets the tick, and with it the refresh lifetime.

### Sessions report and the account cap

Every launch that takes an account slot reserves it against the account's cap in one Redis script. The script
counts the account's sessions as:

1. the sessions every live hive reports for the account in its sessions report, `<root>:hive:<id>:sessions`, where
   live means a heartbeat no older than ninety seconds;
2. plus every launch slot row for the account that is not exited and whose execution no hive report lists;
3. minus every exited launch slot row whose execution a hive report still lists.

When the count reaches the cap the launch is not started, and the launch reports

```text
account reservation is full: account=<harness>:<account> sessions=<count> cap=<cap> five_left=<pct> week_left=<pct>
```

The hive's beat writes the sessions report every 15 seconds with a 45 second expiry. Between beats the headroom
publisher, every 10 seconds, moves the hive's `heartbeat_at` forward and extends the sessions report's expiry to the
90 second live window. While the headroom publisher runs, a sessions report therefore counts for as long as its
hive reads live: while the beat stalls, for example while it waits inside a launch, the hive keeps its last report
and its sessions still count against the cap. Once both the beat and the headroom publisher stop, the heartbeat
goes stale ninety seconds after the last write, and the report expires 45 seconds after that write if the beat made
it, or ninety seconds after it if the headroom publisher made it, so the hive's sessions stop counting and free the slots.

A beat stalled during a launch therefore holds the cap at the last reported count until the next beat refreshes it,
which can count a session the hive has since lost. Before this, the report expired 45 seconds after the last beat
while the headroom heartbeat kept the hive live, so the reservation counted none of that hive's sessions and
admitted launches past the cap: Doctor finding `failed-spawn/t2015` recorded a Codex account at eight live sessions
against a cap of six.

Read a hive's report and its remaining lifetime on the swarm Redis:

```bash
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET agentihooks:hive:lab-vm:sessions
# {"codex:one": 6, "_executions": {"codex:one": ["...", "..."]}}
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" PTTL agentihooks:hive:lab-vm:sessions
# up to 90000 while the hive is live
```

The ledger monitor reads the report only for live hives. The Hives panel reads it for every hive seen in the last 24
hours whether or not the hive is live, so it shows a dead hive's counts for up to ninety seconds after its last
heartbeat.

### Checking a member's scope

Read the Redis user's key patterns on the swarm Redis, as an admin user:

```bash
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" ACL GETUSER hive-lab-vm
```

The `keys` field lists the member patterns above and one entry per [worker key](#worker-keys). On the member, a
write outside them fails with `NOPERM`; this
probe tries to write a key in the controller's inbox, which the member must not reach:

```bash
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" SET agentihooks:inbox:probe 1
# (error) NOPERM ...
```

The packaged smoke tests check the same scope end to end: `scripts/packaging/hive-join-smoke.sh` adds a member on
Redis 7 and asserts that the shared inbox, shared session readings and another member's keys are refused, that the
member's own inbox and readings are allowed, and that the relay refuses a sender the member does not run.
`scripts/packaging/compose-hive-smoke.sh` relays a member agent's mail to the master and reads the host's reply from
the member's own keys.

### Limitations

- The controller's wake and escalation pass, exit sweep, swarm status, handoff mailbox and swarm removal read every
  joined member's inbox as well as the controller's. Mail sent to a seat while it was empty still waits in the
  controller keys until the next tick's [seat mail move](#seat-mail-follows-its-occupant), and master seat mail
  never moves.
- A member-hosted agent writes outside its hive key only through the [worker keys](#worker-keys): `swarm done`,
  `ready`, `park`, `wait`, `status`, `block` and `learned` pass, and any other command that writes a swarm key
  outside that list is refused.
- A member's master pool, filled while every live master sat on that member, drains only on the next send there.
- The launch slot lifetime of two ticks assumes a tick finishes within one tick interval.
- The pane cleanup's alias prompt scan reads only the controller's prompt keys.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Mail a member agent sent never reaches the master | No swarm tick has run since the send; the relay runs once per tick. | Check the swarm is started and its controller lease is held: `agentihooks swarm <slug> status`. |
| A member item closes as `refused: the hive member that sent it does not run its sender` | The sender named on the item is not an agent placed on that member. | Send from the agent's own session on the member; the sender is the session, never an argument. |
| A member process fails a write with `NOPERM` | The process writes a key outside its hive key and outside the [worker keys](#worker-keys), such as a launch slot row or swarm settings. | Expected for controller keys. A worker command refused this way is a bug to file with the command and the key named. |
| `swarm done` fails with `NoPermissionError` deleting the task claim, or `swarm status` fails with `NoPermissionError` writing a finding verdict | The member's Redis user was written before its hive endpoint ran a release with the worker keys, so `ACL GETUSER hive-<id>` lists no `~<root>:swarm:*:claim:*` or `~<root>:swarm:*:findings` pattern. | Restart the hive endpoint (`agentihooks hive serve`) on a current release, or run `agentihooks hive refresh` on the member; see [Applying the grant](#applying-the-grant). |
| A new hive's service restarts at startup and Redis refuses `<root>:hive:<old id>:scheduler:<machine>:entries` to user `hive-<new id>` | The daemon keyed its scheduler under a member id that did not match its Redis user. | Upgrade the hive and restart its service. The daemon now takes the id with the credential; see [Member id after a refused credential](#member-id-after-a-refused-credential). |
| The controller shows a member agent idle though it works | The agent's execution record names a hive with no member record, so the controller reads its readings from the controller keys. | Check `agentihooks hive show <id>` lists the hive and that it was added with `hive add`. |
| A hive-run dispatcher or lane agent never sees mail sent to its seat while the seat was empty | The item waits in the controller keys until the next tick's exit sweep moves it. | Wait one tick, then run `agentihooks msg inbox` as the occupant on its hive. Mail to a master seat does not move. |
| A slot stays taken after a member lost an agent | The member still reports the execution. | The row lapses within two ticks once the execution leaves the member's sessions report. |
| An account runs more live sessions than its cap | A live hive's sessions report is missing: `redis-cli GET <root>:hive:<id>:sessions` returns nothing while `agentihooks hive list` shows the hive live. | Upgrade the hive to a release whose headroom heartbeat extends the report, then restart its hive service; see [Sessions report and the account cap](#sessions-report-and-the-account-cap). |
| Launches report `account reservation is full` though the account runs fewer sessions | A hive's beat is stalled and its last sessions report still counts. | The count corrects on the hive's next beat, or within ninety seconds after both its beat and its headroom publisher stop. |

## Settings

Set them with `agentihooks hive set <id> key=value ...`, or on the ledger page's
[Hives panel](#ledger-hives-panel), which writes through `PATCH /api/v1/hives/<id>` and needs the operator.

| Setting | Values | Default | Meaning |
|---|---|---|---|
| `name` | text | the hive id | Display name. A hive with `ui=yes`, where the masters run, is always named `swarm-master`. |
| `ui` | `yes`, `no` | `no` | The machine has a screen and a person at it. Agents on a `ui=yes` hive are also graded on kubectl and a global git identity; see [Headed hive checks](#headed-hive-checks-kubectl-and-git-identity). |
| `ephemeral` | `yes`, `no` | `no` | The machine can disappear, such as a pod. Placement prefers lasting hives on a tie. |
| `pinned` | `yes`, `no` | `no` | The hive stays on one release. The hub release floor skips it, so the hub keeps admitting it after the hub moves to a newer release. A worker hive's chart sets it from `workers.pinned` on every start. |
| `excluded` | comma list of lane roles or profiles, such as `master,frontend,qa` | `master` on `ui=no`, none on `ui=yes` | Work the hive never takes. An empty value excludes nothing. |
| `master-swarms` | comma list of swarm slugs | none | Swarms whose masters the hive takes although it excludes `master`. Every other swarm still sees masters excluded. |
| `prefer` | comma list of `role:rank` | none | Rank 1 is chosen first for that role; unranked hives come after ranked ones. |
| `max-agents` | whole number, 0 or more, or `auto` | `auto` | A cap on the hive's agent ceiling. With no cap the ceiling is computed each tick from the hive's own heartbeat reading: the agents it runs plus the room the workstation host room rule gives its free memory less swap in use and its one minute load. A cap holds the ceiling lower; `0` scales the hive fully down, so the tick places no new agent there; `auto` clears it. Placement admits no hive past the ceiling, and the headroom admission still applies beside it. With no cap and no fresh reading the ceiling reads `unknown`. Masters do not count against it. A stored value is kept. |
| `capabilities` | comma list of names, such as `headed` | none | What the machine offers beyond the defaults. A task with `needs` runs only on a hive that declares every capability it needs. |

Every hive takes every agent type (master, eng, ci, plan, dispatch and any profile) unless the agent's lane role
or profile is in its `excluded` list, and never past its max agents. `prefer` refuses a role the hive excludes. An
unset `excluded` follows the hive's current `ui`. A hive still on a legacy record with the old `roles` setting drops
it, with its stored `max-agents`, on its next heartbeat, join or `hive set`, and reads under the defaults until then.

```bash
agentihooks hive set pod-a excluded=master,frontend,qa max-agents=12
agentihooks hive show pod-a        # "excluded": ["master", "frontend", "qa"], "max_agents": 12
agentihooks hive set pod-a excluded=
agentihooks hive show pod-a        # "excluded": [], every agent type may land here
agentihooks hive set pod-a max-agents=auto
```

With the defaults, a `ui=no` hive takes `ci`, `plan` and every other worker role or profile and refuses `master`;
a `ui=yes` hive takes every agent type, masters included. A hive still on a legacy `roles` record reads under these
defaults until its next heartbeat, join or `hive set`; `agentihooks hive set <id> excluded=<list>` writes its own list at once.

### Capabilities and task needs

A hive declares what it offers with `capabilities`, and a task names what it requires with `needs`:

```bash
agentihooks hive set desk capabilities=headed
agentihooks api --slug <slug> --as <name> task add browse "Check the logged in page" --lane eng --needs headed
agentihooks api --slug <slug> --as <name> task set browse needs=headed
```

Placement keeps only the live hives whose capabilities cover every name in the task's `needs`. When none does, the
task waits with `the task needs a hive with headed, and no hive declares it`, shown with the other placement waits
in `swarm status`. Overflow to the local machine and a local swarm take such a task only when the local hive record
declares the capabilities too, and runtime placement keeps it off a backend that cannot declare them. `swarm status`
prints each agent's hive in its Hive column, and the ledger page agents table names it under the agent.

### Excluded and prefer: the operator's layout

The operator runs a PC, a VM and a set of pods:

```bash
agentihooks hive set pc  ui=yes excluded=qa,frontend,eng,ci,plan,dispatch prefer=master:1
agentihooks hive set vm  ui=yes excluded=eng,ci,plan,dispatch prefer=master:2,qa:1,frontend:1
agentihooks hive set pod-1 ui=no ephemeral=yes max-agents=20
```

- The master runs on the PC while it is live, and on the VM when the PC is gone; the pods exclude it by default.
- QA and frontend work runs on the VM, then on a pod when the VM is gone.
- Engineer, CI, planner and dispatcher work runs on the pods, and on the PC when every pod is full once the PC no
  longer excludes that role.

### The dispatcher seat on hives

The dispatcher is one seat per swarm, `dispatcher@<slug>`, in the `dispatch` lane. The tick's dispatch seat step
starts it while the swarm runs at `autonomy=full` and has an open dispatcher trigger: a Priorities item, an
uncovered bottleneck share, a red dev run that holds work, or an inbox item for the seat. It ends the seat when the
triggers close. It spawns through hive placement like the `eng`, `ci` and `plan` lanes: the
`dispatcher` profile places as the `dispatch` worker role, so it lands on any live hive that does not exclude
`dispatch` or `dispatcher`, ordered as in [Placement](#placement).

Placement does not try headless hives first for the dispatcher: it pools every live hive that takes `dispatch`,
UI and headless alike, and orders them only by `prefer` rank, room, lasting over ephemeral and hive id. Keeping
the seat off the workstation takes an exclusion or a rank, as below.

A worker hive pod excludes only `master` by default (`HIVE_EXCLUDED`, chart value `excluded: [master]`), so every
pod takes the dispatcher. A UI hive excludes nothing by default, so the workstation takes the dispatcher on equal
terms with the pods. To run the dispatcher only on headless hives, exclude `dispatch` on every UI hive:

```bash
agentihooks hive set desk ui=yes excluded=eng,ci,plan,dispatch prefer=master:1
agentihooks hive show desk          # "excluded": ["eng", "ci", "plan", "dispatch"]
```

To let the workstation take the dispatcher only when no pod can, leave `dispatch` out of its `excluded` list and
rank the pods instead:

```bash
agentihooks hive set anton-1 prefer=dispatch:1
agentihooks hive set anton-2 prefer=dispatch:2
```

An unranked hive comes after every ranked one, so the workstation takes the seat only when every ranked pod lacks
the account slot, room, the repo or the login, or is not live. The agent record's profile decision then carries
`placement_spill` with `preferred`, the first ranked hive, and `refusal`, why it did not take the seat, and the hive
launcher prints the same fields on stderr as one `hive_placement_spill` JSON line.

The seat starts only when nothing holds it. The tick checks, in order, the controller's placement refusal for the
`dispatch` lane, a free account slot and the host room guard, and logs the first refusal as the tick's result for
the seat. When every hive that takes `dispatch` is full the seat waits with
`the dispatcher waits: no hive for dispatch has room: <hive> <refusal>` and the tick tries again on its next pass.
A paused swarm starts no dispatcher.

Check where the dispatcher runs:

```bash
agentihooks swarm <slug> status     # the dispatcher row's Hive column names its hive
agentihooks hive list               # each hive's excluded list, prefer ranks and agents
```

| Symptom | Cause | Fix |
|---|---|---|
| The dispatcher runs on the workstation while pods have room | The workstation does not exclude `dispatch`, and no pod ranks first in `prefer`. | `agentihooks hive set <workstation> excluded=...,dispatch`, or rank the pods with `prefer=dispatch:1`. |
| The tick reports `the dispatcher waits: no hive for dispatch has room` | Every hive that takes `dispatch` is at its agent ceiling or measured headroom. | Wait for a pod to free a slot, raise `max-agents` on a pod, or add a pod. |
| A launch waits with `no live hive takes dispatch` | Every live hive excludes `dispatch`, or no hive has a fresh heartbeat. | Remove `dispatch` from a hive's `excluded` list, or restart the stale hive's `agentihooks-hive` service, or roll its pod. |

## Comparing hive configuration

Every hive runs agents with the workstation's configuration. Each beat keeps the hive's configuration published,
named without values or host paths: account token names, agentihooks env setting names, the linked bundle and
profiles, installed Claude rules and skills, managed MCP servers, satisfied bundle dependencies and repo identities.
The snapshot lives fifteen minutes and is rebuilt once under a minute is left, so dependency checks run rarely.

```bash
agentihooks hive compare               # this machine's hive is the base
agentihooks hive compare --base root
```

The first line is the base with a count per section; every other live hive follows as `equal`, `differs` with one
`<section> missing|extra <name>` line per difference, or `no configuration published`. The command exits 1 when
any hive differs or published nothing.

## Placement

Every launch places its agent on one hive. For a worker role, placement keeps the live hives that are not draining
and do not exclude its lane, role or profile. It checks the selected account slot, admission room, the
[repository match](#matching-a-repo-across-machines), and any required interactive or master login.
Each check that empties the candidate list names its reason, and the launch waits with it.

Among eligible worker hives, placement picks the lowest `prefer` rank for the role, then the lowest fraction of
agent capacity in use, then a lasting hive over an ephemeral one, then the hive id.

This order is the swarm's default placement mode, `anton-first`. A swarm can switch its worker launches to
[round robin placement](#round-robin-placement-for-load-runs) for load runs.

### Round robin placement for load runs

Round robin placement spreads a swarm's worker agents evenly over at most a set number of hives, local or cloud,
so a large volume of work can be loaded onto a known set of machines to prove the swarm holds. Two swarm settings
select it:

| Setting | Values | Default | Meaning |
|---|---|---|---|
| `placement-mode` | `anton-first`, `round-robin` | `anton-first` | `anton-first` places by `prefer` rank, then the share of capacity in use, as in [Placement](#placement). `round-robin` spreads worker launches over the kept hives. |
| `placement-nodes` | whole number, 0 or more | `0` | How many hives round robin keeps. `0` keeps every eligible hive. A number above the eligible count keeps them all. `anton-first` ignores it. |

```bash
agentihooks swarm <slug> set placement-mode=round-robin placement-nodes=3
# {"swarm": "<slug>", "applies": "next tick", ..., "placement_mode": "round-robin", "placement_nodes": 3, ...}
agentihooks swarm <slug> set placement-nodes=0         # spread over every eligible hive
agentihooks swarm <slug> set placement-mode=anton-first
```

Both settings can be set while the swarm runs. The tick reads them on its next pass. A swarm saved before these
settings existed reads `anton-first` with no node limit. `swarm set` refuses any other mode with
`placement mode must be one of anton-first, round-robin`, and a node limit that is not a whole number with
`placement-nodes takes a whole number of hives, 0 for no limit`. A refused value leaves the stored settings
unchanged.

#### How round robin picks a hive

For each `eng`, `ci`, `plan` or `dispatch` launch, and any other worker profile, placement:

1. Keeps the live hives that are not draining, do not exclude the launch's lane, role or profile, and host its
   profile, as under `anton-first`.
2. Keeps the hives among those that [list the swarm's repo](#matching-a-repo-across-machines), sorts them by hive
   id and keeps the first `placement-nodes`. A hive that excludes the role, lacks the repo or has gone stale
   takes no place in the set, so the next hive by id joins instead.
3. Orders the kept hives by the agents each runs plus the launches already placed on it in the same tick pass,
   fewest first. Ties go to the lower share of capacity in use, then a lasting hive over an ephemeral one, then the
   hive id. `prefer` ranks play no part.
4. Runs the usual account slot, room, login and repo checks on that order and takes the first hive that passes.

The kept set is computed again on every launch, so a hive that stops beating leaves the set and a new hive with a
lower id enters it. When every kept hive refuses, the launch waits with the refusal reasons of the kept hives only
and never uses a hive outside the set. A `hybrid` controller, or a `local` machine with `hive-placement=on`, still
runs such a launch in the machine's own herdr panes while the machine is under `local-max-agents`, as described
under [Placement by role](#placement-by-role); set `local-max-agents=0` on the swarm to keep the load run on the
kept hives alone.

Example: three headless hives list the swarm's repo:

| Hive | Agents running | `max-agents` | `prefer` |
|---|---|---|---|
| `anton-1` | 3 | 28 | `eng:1` |
| `anton-2` | 2 | 4 | none |
| `anton-3` | 0 | 4 | none |

With `anton-first`, every `eng` launch goes to `anton-1` while it has room, because it ranks first for `eng`.
With `placement-mode=round-robin placement-nodes=2`, placement keeps `anton-1` and `anton-2`, the first two by id,
and never uses `anton-3`. The first launch goes to `anton-2`, which runs fewer agents. Both then run three, and the
second launch goes to `anton-1`, whose share in use, 3 of 28, is lower than `anton-2`'s 2 of 4. Later launches
keep alternating by count until `anton-2` has no room left, after which every launch goes to `anton-1`.

#### What keeps anton-first

- Masters, and the `qa` and `frontend` roles on any lane, place by `anton-first` on their UI hives; see
  [Placement by role](#placement-by-role) and [Master placement](#master-placement).
- A task whose `hive` field pins it to one hive, or a task marked `workstation=yes`, places only on that hive, even
  when it lies outside the node limit.

#### Checking a load run

```bash
agentihooks hive list                    # each hive's workers=N of M: the kept hives should differ by at most one
agentihooks swarm <slug> status          # the Hive column names each agent's hive; placement waits list refused tasks
agentihooks swarm <slug> set placement-mode=anton-first   # back to the default when the run ends
```

A spread can differ by more than one when a kept hive reached its ceiling, its headroom or its account slots, or
when agents placed before the switch still run where `anton-first` put them. Round robin moves no running agent.

| Symptom | Cause | Fix |
|---|---|---|
| Agents still gather on the `prefer` ranked hive | The swarm is on `anton-first`, or the launches are masters, `qa` or `frontend`. | `agentihooks swarm <slug> set placement-mode=round-robin`, and check the task profiles. |
| A hive meant for the run takes no agents | It sorts past the first `placement-nodes` ids, does not list the repo, excludes the role, is draining or has no fresh heartbeat. | Raise `placement-nodes`, or check `agentihooks hive show <id>` for `repos`, `excluded` and `draining`. |
| Launches wait while hives outside the set have room | Every kept hive is at its ceiling, headroom or account slots. | Raise `placement-nodes`, raise `max-agents` on a kept hive, or wait for a kept hive to free a slot. |
| Load run agents land on the local machine | A `hybrid` controller overflowed launches the kept hives refused. | `agentihooks swarm <slug> set local-max-agents=0` for the run. |
| `swarm set` answers `placement-nodes takes a whole number of hives, 0 for no limit` | The value is negative or not a whole number. | Pass `placement-nodes=0` or a positive whole number. |

### Workstation fallback for workers

A headless hive refusing an `eng`, `ci`, `plan` or `dispatch` launch does not prevent an eligible UI hive,
such as the workstation, from taking it. This covers a missing account slot, a repo mismatch, a missing login,
no admission room, or no live headless hive. The workstation must pass the same account, repo, role and room
checks. A fallback never bypasses a task's hive pin or workstation requirement.

Worker selection now ranks eligible headed and headless hives together using `prefer` and utilization. Set a
lower `prefer` rank for a worker to favor it; being headless alone does not force that worker to win. The engineer,
cicd, planner and dispatcher profiles place as `eng`, `ci`, `plan` and `dispatch`; frontend and qa profiles keep
their UI role on any lane.

For a workstation reporting five accounts and a headless worker reporting one, a launch can use the worker's
account there even though an earlier account candidate could run only on the workstation. The account loop
prefers a choice without a fallback reason over an earlier choice carrying one. If the worker cannot accept the
selected account or repo, an eligible workstation remains available.

```bash
agentihooks hive list
agentihooks hive set <workstation-hive-id> ui=yes max-agents=5
agentihooks hive set <worker-hive-id> ui=no prefer=eng:1,ci:1,plan:1,dispatch:1
agentihooks hive show <worker-hive-id>
agentihooks hive show <workstation-hive-id>
```

These commands configure role preference and the workstation ceiling; account slots and repos come from each
hive's heartbeat. An account count is not an agent ceiling: quota caps and measured headroom still apply.
For one swarm in local mode, also enable [hive placement](#hive-placement-for-one-swarm-on-a-local-machine).

Where selection records a fallback reason, the agent's profile decision carries it as `placement_fallback`;
a task pinned to a UI hive records the pin as its reason. Ranked worker spill also records the preferred hive's
refusal; see [First ranked hive room and spill records](#first-ranked-hive-room-and-spill-records). Masters keep their separate [placement chain](#master-placement).

### Placement waits

A ready task refused during placement appears in tick output as `task <id> waits: <reason>`. For example:

```text
task example waits: no hive for eng advertises the claude slot worker-account
task example waits: no hive for eng lists the repo /home/me/dev/agentihooks
```

The tick reports the tasks it evaluates while filling free launch slots. A task not reached because those slots
are already filled has no new placement decision in that pass.

| Wait or symptom | Check | Resolution |
|---|---|---|
| `lists the repo` despite matching repository names | Local `origin` and the target hive's advertised `repos` | Confirm the host and owner match, the checkout lies under `HIVE_REPO_ROOT`, and the hive has published a new heartbeat. |
| Launch on a hive refused with `directory does not exist` | The hive's checkouts under `HIVE_REPO_ROOT` and their `origin` remotes | Clone the repo under `HIVE_REPO_ROOT` with an `origin` remote matching the advertised identity, or restart `hive run` with `HIVE_REPO_ROOT` at the clones' parent. See [Launch directory on a hive chosen by origin](#launch-directory-on-a-hive-chosen-by-origin). |
| `advertises the ... slot` | `slots` in each hive's `hive show` record | The chosen harness and account must be advertised by an eligible hive, including the workstation. |
| `holds the ... login` | The selected account's login on the candidate hive | Use a hive holding that login; subscription availability alone does not supply an interactive login. |
| `has room`, possibly followed by CPU or RAM detail | `hive list` headroom, `max-agents`, and the workstation's local cap | Free capacity or adjust the relevant cap only when the host has room. |
| Headless worker refuses and workstation still receives no launch | Workstation heartbeat, role exclusions, repo, account, headroom and task pin | A fallback needs an eligible workstation; a pin does not spill to another hive. |

### Placement by role


The host kind an agent lands on follows from its placement role. A hive's `ui` setting declares its kind: `ui=yes`
is a UI hive, such as the workstation that runs the herdr server the operator watches; `ui=no` is a headless hive,
such as an Anton worker pod.

The role comes from the profile the launch resolves for the task, so a task's `profile` decides it before its lane
does:

| Profile | Placement role | Host kind |
|---|---|---|
| `master` (every master seat) | `master` | UI hive; see [Master placement](#master-placement) and [Workstation fallback](#workstation-fallback) |
| `frontend`, on any lane | `frontend` | UI hive only |
| `qa`, on any lane | `qa` | UI hive only |
| `engineer` | `eng` | Any hive that does not exclude it |
| `cicd` | `ci` | Any hive that does not exclude it |
| `planner` | `plan` | Any hive that does not exclude it |
| `dispatcher` | `dispatch` | Any hive that does not exclude it |

Any other profile places under its own name as a worker role, on any hive that does not exclude it. Placement drops a
hive whose `excluded` list names the lane, the role or the profile.

UI roles, `master`, `qa` and `frontend`:

- While a live UI hive takes the role, placement chooses only among UI hives, even when a headless hive also takes
  it.
- When headless hives take a `qa` or `frontend` role and no live UI hive does, the launch waits with
  `no live swarm-master takes qa` (or `frontend`). It never runs on the headless hive.
- These roles never overflow to the local machine, and round robin placement leaves them under `anton-first`.

Worker roles, `eng`, `ci`, `plan` and `dispatch`, place on every live hive that does not exclude them, UI or
headless, by the order above. A UI hive's `excluded` default is empty, so the workstation takes worker work on
equal terms with the pods unless it excludes the worker roles or the pods rank first in `prefer`. With the
defaults, worker roles are not held to headless hives. Two layouts control when worker roles reach the
workstation:

```bash
# Worker work never runs on the workstation; it waits for a pod with room
agentihooks hive set anton-1 ui=no ephemeral=yes                 # excluded defaults to master
agentihooks hive set desk ui=yes excluded=eng,ci,plan,dispatch prefer=master:1

# Worker work runs on the workstation only when no ranked pod takes it, with the reason logged
agentihooks hive set anton-1 prefer=eng:1,ci:1,plan:1,dispatch:1
agentihooks hive set anton-2 prefer=eng:2,ci:2,plan:2,dispatch:2
agentihooks hive set desk ui=yes excluded=
agentihooks hive show desk                                       # "excluded": [], "prefer": {}
```

With the first layout a worker launch that finds no pod with room waits with the pods' reason, such as
`no hive for eng has room: anton-1 ...`. The local machine overflow below also reads the local hive's `excluded`
list, so it never runs the launch on that machine either.

The second layout needs every headless hive ranked for every worker role. The workstation stays unranked, and an
unranked hive comes after every ranked one, so it takes the launch only once every ranked pod lacks the account slot,
room, the repo or the login, or is not live, draining or excluding the role. An unranked pod is ordered with the
workstation by the share of its ceiling in use, and can lose to it while it still has room; with no `prefer` set on
any hive, nothing records why the workstation took the launch. When a ranked pod is skipped, the agent's profile
decision carries `placement_spill` with `preferred`, the first ranked hive, and `refusal`, why it did not take the
launch. The hive launcher prints the same fields on stderr as one JSON line with `event` `hive_placement_spill`,
`slug`, `task`, `name`, `hive` (the hive that took it), `preferred` and `refusal`.

When no hive at all takes a worker launch, a `hybrid` controller, or a `local` machine with `hive-placement=on`,
runs it in the machine's own herdr panes while the machine is under `local-max-agents` and its local hive record,
if any, does not exclude the role, and only while the machine's own herdr server answers; see
[Hive placement for one swarm on a local machine](#hive-placement-for-one-swarm-on-a-local-machine). A full machine
waits with `<hive reason>, and the local machine runs <n> of its <max> agents: <room reason>: <detail>`. While
tasks marked `workstation=yes` wait for the machine, the agent count reads
`<n> of its <max> agents with <r> kept for waiting workstation tasks`, where `<r>` is the number of seats kept for
them.

Pinned and gated tasks narrow the hives before the role rule runs:

- A task with `hive` set places only on that hive.
- A task with `workstation=yes` places only on the `root` hive, or on the controller's own hive when no `root` hive
  is registered, never on another UI hive, and waits with
  `the task needs the workstation, so no remote hive takes it` otherwise.
- A task with `needs` places only on hives that declare every capability; see
  [Capabilities and task needs](#capabilities-and-task-needs).

The fallback reasons an agent record carries in its profile decision apply to worker roles and to a master launch
whose task sets `route_kind`, the account route the launch must use, such as `interactive`. A default master launch
goes through [Master placement](#master-placement) and records `master_placement` with `hive` and `headless`
instead of either field. `headless` is always `false`, even for a master on a headless hive; read the hive's `ui`
setting with `agentihooks hive show <hive>` to tell its kind.

| Field | Written when |
|---|---|
| `placement_fallback` | A master whose task sets `route_kind` runs on a headless hive that does not exclude `master`, because no UI hive fits; for a master pinned to a hive it reads `the task is pinned to <hive>`. |
| `placement_spill` | The hive ranked first for the role in `prefer` refused, or is not live, draining or excluding the role, and another hive took the launch. |

When no live hive of any kind takes the role, every role waits with `no live hive takes <role>`; the master then
follows [Workstation fallback](#workstation-fallback).

| Symptom | Cause | Fix |
|---|---|---|
| A frontend or qa task waits with `no live swarm-master takes frontend` | No live UI hive takes the role: none is live, or each live one is draining or excludes it, while a headless hive takes it. | Start the UI hive, end its drain, or remove the role from its `excluded` list; `agentihooks hive list` shows heartbeat and exclusions. |
| An engineer or planner runs on the workstation while pods have room | The workstation does not exclude the role and has the smaller share of its ceiling in use. | `agentihooks hive set <workstation> excluded=eng,ci,plan,dispatch`, or rank the pods first in `prefer`. |
| A worker task waits with `no live hive takes eng` | No hive is live, or every live hive excludes `eng` or is draining. | Check `agentihooks hive list` for heartbeats, remove `eng` from a hive's `excluded` list, or wait for the drain to end. |
| A worker launch waits on the hives' reason although the local machine is under `local-max-agents` | The machine's herdr server does not answer, its hive record excludes the role, the task is pinned to a hive, the local hive does not declare every capability in the task's `needs`, or the controller is not `hybrid` and the machine is not `local` with `hive-placement=on` set on the swarm. | Start herdr on the machine, or check `agentihooks hive show <machine>` and the swarm's `hive-placement` setting. |
| A master runs on a pod | That pod's `excluded` list does not name `master`, or `master-swarms` lists the swarm, and no UI hive was live with the repo and a slot or login for the harness. | `agentihooks hive set <pod> excluded=master master-swarms=`; the next master launch returns to the UI hive. |

A hive counts as live while its last heartbeat is under ninety seconds old. Codex launches are not placed on hives.

### Headroom

A hive's room is the smaller of its `max-agents` left and what its measured headroom fits. `hive run` publishes the
headroom reading to `<root>:hive:<id>:headroom`, and placement, the launch reservation and `hive list` read it from
there.

A reading holds:

| Field | Source |
|---|---|
| `cpus`, `load5` | CPU count and the five minute load average from `/proc/loadavg`. |
| `available_mb`, `total_mb` | Available and total memory from `/proc/meminfo`. |
| `pod_total_mb`, `pod_available_mb` | The cgroup v2 `memory.max` limit and that limit minus current use less inactive file pages, only when the limit is set. |
| `pod_cpus`, `pod_cpu_usec`, `pod_load` | The cgroup v2 `cpu.max` quota and usage; `pod_load` needs a previous reading under 30 seconds old, so the pod CPU limit does not apply to the first reading after a start or a gap. |
| `pending` | Launch orders waiting in the hive's order stream, shown by `hive list`. Room counts the hive's pending launch reservations instead. |
| `measured_at` | Milliseconds when the reading was taken. |

The reading expires after 30 seconds, and a reader treats one older than that as absent. With no fresh reading the
hive's room is 0, the refusal reads `missing telemetry: no fresh headroom reading`, and `hive list` shows
`headroom=missing`. Otherwise each limit fits whole agents at 0.5 CPU and 850 MB each under 80 percent of the host,
and of the pod when the cgroup sets a limit; launches reserved on the hive in the last fifteen minutes count against
each limit, and the smallest limit wins.

`hive run` publishes headroom on two paths:

- **Every beat,** right after the heartbeat is written and before launch orders, herdr reports and execution reports,
  stamped with the heartbeat time. A beat that fails after the heartbeat still leaves a fresh reading.
- **Every 10 seconds on its own thread.** A launch inside a beat can run `init-agent` for minutes; the thread keeps
  the reading fresh while it does, and also renews the daemon's order consumer marker (45 seconds), so a launch it
  still holds is not taken for an orphan. The thread paces with the scheduler process timer, like the beat. A Redis
  error, or a reading that cannot be parsed, prints `hive headroom publish failed (<error>: <detail>)` to stderr and
  the thread keeps going. A lost or timed out timer wait prints `hive headroom wait failed (<error>: <detail>)` and
  ends the thread; any other error ends it with a traceback.

The daemon checks its helper threads, the headroom publisher and the herdr stats publisher, at the start of every
beat and starts again each one that has ended, so a helper is back within one beat of ending without waiting for the
beat loop's own Redis connection to drop. Each check starts at most one thread per helper, so a Redis that stays down
cannot make the daemon spin. A reconnect starts both helpers fresh on the new connection. The herdr stats thread logs
a lost wait the same way, as `hive herdr stats wait failed (<error>: <detail>)`.

Between a helper ending and the next beat, only the beat path publishes. A beat that holds a launch longer than 30
seconds, the reading's expiry, therefore still lapses the reading once, until that beat ends and the next one
restarts the thread.

`hive list` appends the reading to each hive's line, its fields separated by tabs (shown aligned here):

```bash
agentihooks hive list
# vm  live  ui=no  workers=3 of 20 (auto)  excluded=none  cpus=8  load5=1.42  available-mb=21034  pending=0
# anton-2  live  ui=no  workers=2 of 8 (cap 8)  excluded=master  cpus=16  load5=4.73  available-mb=2576  pod-limit-mb=4200  pending=0
```

A hive in a pod with a cgroup memory limit shows `available-mb` as the tighter of the node and pod figures, and
`pod-limit-mb` names the pod limit; admission counts the pod memory room as one more limit and launches by the
tightest of them.

`hive list` picks the memory figure from the stored reading alone:

| Reading carries | `available-mb` | `pod-limit-mb` |
|---|---|---|
| `pod_total_mb` and `pod_available_mb` | The smaller of `available_mb` (node) and `pod_available_mb` | `pod_total_mb` |
| Anything else; the publisher writes both fields or neither | `available_mb` (node) | Not printed |

The pod fields appear only when `/sys/fs/cgroup/memory.max` exists and holds a number. A bare host, a cgroup v1
mount, or a pod with no memory limit (`memory.max` reads `max`) publishes the node figure only. A value that is
neither a number nor `max` publishes no reading at all, so `hive list` shows `headroom=missing`. The workstation
formats each line when `hive list` runs, so the pod figure needs no hive image change: an older hive that already
publishes the pod fields shows its pod figure once the workstation runs a release with this formatter. Admission
uses `pod_available_mb` whenever the reading carries it, whatever `hive list` prints.

Compare the printed line with the stored reading and the pod's own cgroup:

```bash
agentihooks hive list | grep anton-2
# anton-2  live  ...  available-mb=2576  pod-limit-mb=4200  pending=0
redis-cli GET <root>:hive:anton-2:headroom
# {"available_mb": 10356, "pod_total_mb": 4200, "pod_available_mb": 2576, ...}
kubectl exec -n <namespace> deploy/<hive deployment> -- cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory.current
# 4404019200
# 2300563456
```

`memory.max` in bytes divided by 1048576 is `pod_total_mb`; `pod_available_mb` is that limit minus `memory.current`
plus the `inactive_file` line of `memory.stat`, in MiB.

Hive workers load code at start. A hive still on code from before the 10 second publisher writes headroom only
after the beat's launch poll, so its reading lapses during a long launch; restart its `agentihooks-hive` service,
or roll its pod, to pick up the publisher.

| Symptom | Check | Fix |
|---|---|---|
| `hive list` shows `headroom=missing` on a `live` hive | `journalctl --user -u agentihooks-hive`, or the pod log, for `hive headroom publish failed` | Fix the error it names; a Redis error clears when the connection returns. |
| A launch waits with `missing telemetry` while another launch runs on that hive | The hive's agentihooks version | Upgrade and restart the hive service so the 10 second publisher runs. |
| `headroom=missing` comes and goes on a `live` hive, with readings only as often as its beats | The log for `hive headroom wait failed`, and the hive's agentihooks version: code from before the helper restart loses its headroom thread on the first failed wait until the beat loop reconnects | Upgrade and restart the hive service, or roll its pod. |
| Room reads 0 with a fresh reading | The refusal detail: `cpu`, `ram` or `max agents` | The named limit is spent; wait for agents to finish or raise `max-agents` when the host has room. |
| A pod hive shows `available-mb` above the pod's `memory.max` in MiB, with no `pod-limit-mb` | `redis-cli GET <root>:hive:<id>:headroom` for `pod_total_mb` and `pod_available_mb`; the workstation's agentihooks version | Both fields present: upgrade the workstation, whose older `hive list` printed the node figure only. Fields missing: read `/sys/fs/cgroup/memory.max` in the pod. Absent or `max` means no limit or a cgroup v1 node: the chart sets the container memory limit to `maxAgents` times `memoryPerAgentMi.limit` (see [Worker hives in Kubernetes](#worker-hives-in-kubernetes)), so check the pod's own spec. A number means the hive runs a publisher from before the pod fields: upgrade and restart its hive service, or roll its pod. |

### First ranked hive room and spill records

A hive that sets `prefer` for a role is admitted for that role by its own room rule, so the hive ranked first keeps
taking launches until its memory is nearly full rather than stopping at the first limit the general rule reaches.
Every other hive, and every hive under `placement-mode=round-robin`, keeps the general room above.

**Which hive is first ranked.** Among the live hives that are not draining and take the role, the first ranked hive
is the one with the lowest `prefer` rank for the role, then the lowest share of its agent ceiling in use, then a lasting
hive over an ephemeral one, then the hive id. Hives without a rank for the role are never first ranked. When that hive
takes the launch but a hive listing the task's repo has a lower rank and is not live, is draining or excludes the role,
the spill record below names that hive instead.

**Its room.** For a hive ranked for the role, the tick computes a second room alongside the general one and placement
reads that second room for the role:

| Limit | Rule |
|---|---|
| Host memory | Refused at or above 80 percent used (`available_mb` at or under 20 percent of `total_mb`). Below that line it fits whole agents at 850 MB each up to the line, counted as at least one before pending launches are subtracted. |
| Pod memory | The same rule on `pod_available_mb` and `pod_total_mb` when the cgroup sets a memory limit. |
| Pending launches | Launches reserved on the hive in the last fifteen minutes are subtracted from each memory limit, so they can hold the last agent of room under the line. |
| Host and pod CPU | The same CPU limits as the general room. |
| Disk floor, `max-agents` and the local machine cap | Still refuse the hive as in the general room. |
| Missing reading | No fresh reading gives room 0 with `missing telemetry: no fresh headroom reading`, as in the general room; a fresh reading with no memory fields gives room 0 with `missing telemetry: no memory reading`. |

The smallest limit wins, as in the general room. The launch reservation reads the same rule, so a hive placement
admits is not refused again at reservation by a general limit it was spared.

Within one tick pass each launch placed on the first ranked hive spends one agent of that room. Once a pass has spent
it, the hive's refusal for the rest of the pass reads
`placement: <n> launches placed on it this pass took its room`, and the next launch goes to the next hive in the order.

Refusals the first ranked room produces, with the measured values in the reading:

```text
ram: 2000 of 10000 MB available, 80 percent used, at or above the 80 percent line
ram: pod 3900 of 19600 MB available, 81 percent used, at or above the 80 percent line
ram: 2100 of 10000 MB available, 79 percent used, 850 MB per agent with 2 pending launches, fits 0 more under the 80 percent line
cpu: 15.20 of 16 CPUs used, five minute load 28.70 on 16 CPUs with CPU used at or above the 40 percent swarm share, fits 0 more
cpu: 15.20 of 16 CPUs used, five minute load 28.70 on 16 CPUs with pod load 6.80 at or above the 40 percent swarm share, fits 0 more
max agents: 6 of 6 agents on the hive, fits 0 more
missing telemetry: no memory reading
```

The first room was introduced by [#8595](https://github.com/the-cloud-clockwork/agentihooks/pull/8595) with memory as
its only measured limit. [#9984](https://github.com/the-cloud-clockwork/agentihooks/pull/9984) added the host and pod
CPU limits back after a preferred hive with a five minute load of 28.7 on 16 CPUs read room six and took every
`eng` and `ci` launch.

**The spill record.** When a worker launch for a role some hive ranks lands on another hive, the launch records why
the first ranked hive did not take it. The agent's profile decision carries `placement_spill`:

```json
{"placement_spill": {"preferred": "worker-1", "refusal": "ram: 2000 of 10000 MB available, 80 percent used, at or above the 80 percent line"}}
```

The hive launcher prints the same fields to stderr, which the tick journal keeps, as one line:

```json
{"event": "hive_placement_spill", "slug": "my-swarm", "task": "t42", "name": "engineer@my-swarm-0007", "hive": "worker-2", "preferred": "worker-1", "refusal": "ram: 2000 of 10000 MB available, 80 percent used, at or above the 80 percent line"}
```

`refusal` is the first check the first ranked hive failed:

| Refusal | Meaning |
|---|---|
| `<reason>: <detail>`, such as `ram: ...`, `cpu: ...`, `disk: ...`, `max agents: ...`, `placement: ...` or `missing telemetry: ...` | Its first ranked room was 0; the detail is the binding limit and its measured value, as listed above. |
| `no hive for <role> has room: <hive> <reason>: <detail>` | Placement chose it, and the launch reservation then found its room spent by launches already reserved on it. |
| `no hive for <role> advertises the <harness> slot <slot>` | It does not advertise the account slot chosen for the launch. |
| `no hive for <role> lists the repo <repo>` | It does not list the task's repository. |
| `no hive for <role> holds the <harness> login for <slot>`, or `no hive for <role> holds the <harness> master login for <slot>` | It lacks the interactive or master login the launch route needs. |
| `its heartbeat is older than 90 seconds` | It is not live. |
| `it is draining` | Its drain is on. |
| `it excludes <role>` | Its `excluded` list names the lane, the role or the profile. |

No spill is recorded when no hive ranks the role, when the first ranked hive takes the launch, when a reservation
retry lands back on it, or under round robin placement.

Read the spills of the last hour from the tick journal, and the records the live agents carry, with the Python
environment agentihooks is installed in, from its checkout:

```bash
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "-1 hour" --until now --no-pager | grep hive_placement_spill
python -c '
from scripts.swarm.store import connect
s, slug = connect(), "my-swarm"
for a in s.agents(slug):
    if "placement_spill" in a.profile_decision:
        print(a.name, a.hive, a.profile_decision["placement_spill"])'
```

To confirm the first ranked hive takes launches until its memory line, set the rank, let the swarm fill, then read
both its reading and the spills of the last ten minutes:

```bash
agentihooks hive set worker-1 prefer=eng:1,ci:1,plan:1,dispatch:1
agentihooks hive set worker-2 prefer=eng:2,ci:2,plan:2,dispatch:2
agentihooks hive list          # worker-1 available-mb falls as it takes agents
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "-10 min" --until now --no-pager | grep --line-buffered hive_placement_spill
```

Each spill line while `worker-1` is under its memory line names the other limit that refused it.

| Symptom | Check | Fix |
|---|---|---|
| A second hive takes agents while the first ranked hive sits well under 80 percent memory | The spill `refusal` in the tick journal | Act on the named limit: `cpu` means the hive is CPU saturated, `max agents` means raise its `max-agents`, a slot, repo or login refusal means the hive lacks what the launch needs, and `placement` means the pass already filled its room. |
| A second hive takes agents and no spill line appears | `agentihooks hive show <hive>` for `prefer` on each hive | No hive ranks the role, or the swarm runs `placement-mode=round-robin`; rank the hive for every worker role. |
| The first ranked hive is refused with `missing telemetry: no memory reading` | `hive list` for `available-mb` on the hive | The hive daemon publishes no memory fields; upgrade and restart its hive service. |
| The first ranked hive is refused at reservation with `no hive for <role> has room` | The pending launches held on the hive | Expected while other launches hold its last room; the refusal clears as those launches are acknowledged. |

### Account caps on every hive

Quota belongs to the account, so every hive reports the same cap for it. See
[automatic quota refresh](#automatic-quota-refresh) for how a new hive obtains its own reading before its first
heartbeat. A subscription slot whose own reading is
missing or older takes the cap and reading of the newest one another live hive published for that account. A hive
that holds a token but has not obtained its own reading still shows the account's real cap; interactive and
api slots keep their own.

A hive's subscription cap scales with quota left: the ceiling `AGENTIHOOKS_HIVE_SESSION_CEILING` (default 12) times
the smaller of the five hour and week percentage left, at least the band in
[Claude account load balancing](load-balancing.md) and at most the ceiling, which wins when set below the band. An
account at 96% of both windows left reports 11; one with under 10% of its five hour window left keeps the band's 2;
under 5% of either window it reports 0. Worker admission under a hive or cluster spawn backend reads every Claude
row this way, interactive slots included. Launches routed by one machine's own processes keep the band.

#### Closed and draining accounts get no hive agent

The deployed swarm controller builds its account rows from the approved account keys in its Secret and places hive
agents only on rows with a free seat. For each key it reads the newer of the published fleet observation and the
newest reading any live hive publishes for that account. A hive names a slot by its `AH_CC_TOKEN_<slug>` slug, for
example `claude:lab_one`, and the controller names the same account by its Secret key, `lab-one`; the two are matched
with `_` and `-` treated as the same character, so a hive's reading reaches the controller's row.

An observed account closes for new hive agents, with state `CLOSED` and cap `0`, when it is draining:

- its week is at or under 10% left, for Claude and Codex alike;
- for Claude only, its five hour window is at or under 5% left. Codex rows ignore the five hour window and keep their
  week band.

Both thresholds are fixed in code; there is no setting for them. An observed row also closes when its latest reading
is `rejected` by the provider, when it is the primary Claude login (rank zero of `AGENTIHOOKS_MASTER_LOGIN_ORDER`)
with its week at or under `AGENTIHOOKS_PRIMARY_LOGIN_RESERVE_PCT` left (default 2), or when it is the Claude account
a live master holds and its week has reached the workers' ceiling (see [reserved
rows](load-balancing.md#reserved-rows-in-agentihooks-balance)). Any other observed row takes its quota band alone; the
controller's launch cap does not bound it. A row whose reading is missing, older than fifteen minutes, reported as a
provider error or without a week value counts as unobserved: it stays `OPEN` at the launch cap with no quota left
shown, as before.

A closed row holds no free seat, so hive placement skips it. When a task's planned account is closed, the launch takes
another open Claude account; when every Claude account is closed or full, the task waits with the reason `no Claude
account has room after hive sessions: <account> <n> sessions of <cap>, ...`; an unobserved account shows its launch
cap there, and a task pinned to one account ends with `; pinned to <account>`. A quota successor is chosen from the
same rows, so it never lands on a closed account either. The account opens again on the first reading above both
thresholds, typically after its week or five hour window resets.

Worked example. Two approved keys, `lab-one` and `lab-two`. A hive publishes `claude:lab_one` with 97% of the week
used and no five hour reading; `lab-two` has no reading yet:

| Account | Five hour left | Week left | Row | Placement |
|---|---|---|---|---|
| `lab-one` | - | 3% | `CLOSED`, cap 0 | None, even when the task was planned there |
| `lab-two` | - | - | `OPEN`, launch cap | Takes the agent |

With the same hive reading at 89% of the week used, `lab-one` has 11% left and stays `OPEN` at its quota band, cap 6
with no five hour reading, whatever the launch cap.

Checks:

```bash
# The hive's own reading for each slot: 7D LEFT at or under 10% means hive placement skips that account
ssh <hive> agentihooks balance

# Every hive's slots and caps as the swarm sees them
agentihooks hive show <hive-id>
```

| Symptom | Check | Action |
|---|---|---|
| A task waits with `no Claude account has room after hive sessions` | Each account in the reason with `sessions of 0` | The account is closed: draining, rejected, or reserved for the primary login or a live master. Wait for its window to reset, or add another account to the controller Secret and to a hive. |
| A hive agent starts on an account that `balance` shows at 3% of its week | Whether the controller runs a release that carries the draining rule, and whether the hive is `live` in `agentihooks hive list` | Roll the controller onto the current release. A reading from a hive that is not live is ignored, so restore its heartbeat. |
| An account with quota left stays closed | Its five hour left for Claude, and the reading's age | A Claude account at or under 5% of its five hour window is closed until that window resets. A stale reading counts as unobserved and keeps the launch cap rather than closing the row. |

The regression test `test_a_closed_account_a_hive_advertises_gets_no_placement` plants a hive reading with a 97% week
and no five hour window and checks that the row is `CLOSED` with cap 0, that launch keeps only the open account and
that a task planned on the closed account is routed to the open one.
`test_a_hive_reading_with_room_in_its_week_takes_the_week_band` checks the 89% case.

### Automatic quota refresh

`agentihooks hive run` reads Claude subscription quota itself. A newly joined worker with no cached observation
does not need a manual `agentihooks balance` command to make its slots usable. Before the first heartbeat, the
daemon synchronously probes each configured subscription whose reading is missing or due for refresh. When the
probe returns usable quota and the account has room, that first heartbeat reports a nonzero subscription `cap`.
The startup probe must finish before the heartbeat can be published; this is not a fifteen second probe timeout.

After startup, each beat checks whether a refresh is due. The newest cached observation for each account decides
its age; fresh accounts are left alone. Refresh becomes due once an observation is more than ten minutes old,
five minutes before the fifteen minute freshness limit. Later probes run on one background thread, so they do
not hold up the heartbeat. A running refresh is reused, and attempts are spaced at least five minutes apart,
including after a failed attempt. These timings are fixed; there is no hive setting for the refresh interval.

#### Configure and start

The hive must already be joined and have its normal Redis configuration. Supply subscription credentials to
the daemon as nonempty `AH_CC_TOKEN_<slug>` environment variables; `<slug>` is the account name shown in its slots.
Keep its `AGENTIHOOKS_HOME` writable so the quota cache survives between beats. The installed Claude CLI must be
available to the daemon. With the worker token already supplied securely as `CLAUDE_WORKER_TOKEN`, a foreground
daemon can be started as follows:

```bash
AH_CC_TOKEN_lab="$CLAUDE_WORKER_TOKEN" \
  AGENTIHOOKS_HIVE_SESSION_CEILING=6 \
  agentihooks hive run
```

The ceiling limits the cap computed from quota; it does not change the refresh schedule or create quota. For a
managed daemon, make the same variables available to its service or container, then use `agentihooks hive install`
on a machine with user services. Shell variables set only in an interactive terminal do not configure an already
running service. A worker pod runs the same daemon and refreshes its own readings on startup.

From another joined machine, inspect the hive after its first heartbeat:

```bash
agentihooks hive list
agentihooks hive show lab-vm
```

In the JSON record, find the slot with `"harness": "claude"`, `"account": "lab"` and
`"kind": "subscription"`, and read its `cap`. For example, an account with 96% left in both windows and the
ceiling above reports `"cap": 6`. This is an account session limit, not a count of free places: existing
reservations, the hive's `max-agents`, headroom and repo eligibility still govern placement.

#### Limits and troubleshooting

Automatic refresh covers the Claude subscription credentials available to the daemon. With no such credentials,
there is no Claude subscription to probe. A missing, stale, refused or incomplete quota reading can still leave
a slot at cap zero, as can an account below the quota floor or a configured ceiling of zero. A successful startup
does not guarantee that the provider supplies usable quota.

| Symptom | Check | Action |
|---|---|---|
| A new hive has no Claude subscription slot | Whether the daemon received a nonempty `AH_CC_TOKEN_<slug>` variable | Supply the account through the service or container environment, then restart the daemon. Never print the token. |
| A new slot stays at cap zero | The daemon's quota probe errors and whether its account can return quota | Restore account access, Claude CLI availability or provider connectivity. Allow the five minute retry spacing and a following heartbeat for the next result. |
| A slot becomes zero after previously working | The reading's freshness, quota left and configured ceiling | Restore failed probes when the reading is stale; when quota is spent, wait for its window to reset. Raising the ceiling does not bypass the quota floor. |
| The first heartbeat has not arrived | The daemon log and its initial quota probe, then Redis connectivity | Resolve the reported startup failure. The initial probe runs before the first Redis connection and heartbeat. |

The regression test `test_a_hive_planted_without_a_cache_reports_a_nonzero_cap_after_start` plants a hive with no
cache, observes a zero cap before startup, supplies a successful quota probe and verifies a nonzero cap on the
first beat. It tests daemon startup without requiring a manual balance read or a live provider.

### Master placement

Every master seat, the lead and seats two and up, follows the same host rule as role placement: it runs only on a
live hive that is not draining, does not exclude `master` and lists the repo. A headless hive's `excluded` list
defaults to `master`, so with the default settings a master never lands on a headless hive. When no UI hive fits, the
master goes to the workstation instead; see [Workstation fallback](#workstation-fallback).

A headless hive takes masters only when the operator opts it in, in one of two ways:

| Opt-in | Scope | Undo |
|---|---|---|
| `agentihooks hive set <pod> excluded=` (or any list without `master`) | Masters of every swarm | `agentihooks hive set <pod> excluded=master` |
| `agentihooks hive set <pod> master-swarms=<slug>` | Masters of the named swarms only; every other swarm still sees `master` excluded | `agentihooks hive set <pod> master-swarms=` |

An opted-in headless hive still comes after every UI hive that fits: placement sorts UI hives before headless ones,
then within each kind uses two tiers, each tried in `prefer` order:

1. A hive that holds the native interactive login for the master's harness. Only the front door seat,
   `master@<id>` or a master with no seat, considers this tier; see [Backend master seats](#backend-master-seats).
2. A hive with a subscription or api slot for that harness whose account has a free seat and is not closed. A forced
   start may take any Claude account's slot when no account has a free seat.

A hive names a subscription slot by its `AH_CC_TOKEN_<slug>` slug; a hosted controller names the same account by
its Secret key. The two may differ only in `_` against `-`, and master placement treats them as one account. A
master on a hive is held to the session cap that hive reports for the account, not the hosted controller's cloud
worker cap. The hive computes that cap from its own quota reading of the account, with the bands in
[Claude account load balancing](load-balancing.md), and reports 0 while it has no fresh reading. When the
reservations on the account already reach the cap, the launch is not started and reports "account reservation is
full".

#### Backend master seats

A hive's native interactive login is the account a person signed in with `claude` on that machine. The hive reports
it as the `unrouted` account, with a slot of kind `interactive`. It carries no routed quota token, a master placed
on it reserves it with a cap of 1, and the front door master holds it. Backend master seats, `master-2@<id>` and up,
never take it: master placement offers them only the hive's subscription and api slots, so they route on
`AH_CC_TOKEN_<slug>` accounts like workers. The same seat rule already kept backend seats off a hive's [master
claude ai login](../reference/account-login.md#master-claude-ai-login).

The controller decides the front door from the launch task's seat alone. A master whose seat is `master@<id>`, or
that has no seat, is the front door; every other master seat is a backend seat, whatever its hive or route.

For example, the workstation `desk` is a UI hive where the operator ran `claude` once and also gave the daemon one
`AH_CC_TOKEN_ops` token. An excerpt of `agentihooks hive show desk`:

```json
{
  "id": "desk",
  "ui": "yes",
  "interactive": {"claude": "unrouted", "codex": ""},
  "slots": [
    {"harness": "claude", "account": "ops", "kind": "subscription", "cap": 4},
    {"harness": "claude", "account": "unrouted", "kind": "interactive", "cap": 6}
  ],
  "sessions": {"claude:ops": 1, "claude:unrouted": 0}
}
```

With `masters-step` asking for three masters (see [Several master seats](swarm.md#several-master-seats)), the
seats place as:

| Seat | Hive | Account | Route |
|---|---|---|---|
| `master@<id>` | `desk` | `unrouted` | `interactive` |
| `master-2@<id>` | `desk` | `ops` | `subscription` |
| `master-3@<id>` | `desk` | `ops` | `subscription` |

`master-2` and `master-3` each reserve one of the four `ops` sessions, beside the worker already on that account.
When the front door's own launch is refused because the native login's single reservation is still held, it moves to
the next account with a free seat and never retries the refused login in that launch; see [Retry after a full account
reservation](#retry-after-a-full-account-reservation).

Releases without pull request 11407 offer the native login to every master seat and rank it above the slots. There,
each backend seat chooses the login the front door already holds, hive admission refuses it on every tick, and a
swarm that scales to three masters keeps one. The tick's actions read, on every pass:

```text
master spawn failed in seat master-2@rig-grade-swarm: account reservation is full: account=claude:unrouted sessions=1 cap=1 five_left=None week_left=None
```

The regression tests `test_a_secondary_master_ignores_a_hive_interactive_login` and
`test_a_secondary_master_on_a_native_login_hive_is_ordered_on_a_subscription_slot` hold the rule, under the feature
map entry `master-interactive-login-without-front-gate`. The Semgrep incident rule of the same name flags a native
login candidate in `place_master` that does not check `front`.

This rule covers placement on hives. A master the controller leaves to the workstation's own herdr server follows
[Auth fallback and move back](swarm.md#auth-fallback-and-move-back) instead.

| Symptom | Cause | Fix |
|---|---|---|
| Every tick pass reports `master spawn failed in seat master-2@<id>: account reservation is full: account=claude:<login> sessions=1 cap=1 ...` in its actions, and `swarm status` shows one master while `masters-step` wants more | The controller runs a release from before the front door rule, so backend seats pick the native login the front door holds. | Roll the controller onto a release that carries pull request 11407. |
| A backend seat waits with `no hive for master advertises a claude slot` while the front door runs | No live hive that takes `master` and lists the repo has a Claude subscription or api slot whose account has a free seat and is not closed. The native login does not count for a backend seat. | Give a UI hive an `AH_CC_TOKEN_<slug>` with quota left, then read `slots` and each `cap` in `agentihooks hive show <hive>`. |

#### Retry after a full account reservation

Before the controller sends a master's launch order to a hive, `HiveLaunch.send_order` reserves one session on the
chosen account. The native login reserves with a cap of 1; a subscription or api slot uses the cap the hive reports
for the account, or the account's own cap when the hive reports none. When the reservations already reach the cap, no
order is sent. The new execution record is ended, its reason is written to the swarm's `launch-endings` hash, and the
launch is refused with:

```text
account reservation is full: account=<harness>:<account> sessions=<count> cap=<cap> five_left=<pct> week_left=<pct>
```

`HiveLaunch._placed` then retries inside the same launch:

1. It releases the hive room the refused try held and adds the account's `harness:account` key, such as
   `claude:unrouted`, to the launch's refused set.
2. It selects again with that set. Selection drops the refused accounts' quota rows, and `_master_selection` adds
   each refused key to the closed accounts it passes to `placement.place_master`. A closed account is offered neither
   as the native interactive login nor as a subscription or api slot.
3. When placement finds another account, the launch orders it there. Another full reservation repeats from step 1
   with one more refused account.
4. When no account is left, selection returns a wait and the launch ends with the last `account reservation is full`
   refusal. The tick's actions then read `master spawn failed in seat <seat>: account reservation is full: ...`;
   see [Master scaling troubleshooting](swarm.md#master-scaling-troubleshooting).

Each account is tried at most once per launch, so a launch makes no more tries than the master has candidate
accounts, and every try adds one execution record to the seat. A forced start is not refused at reservation, and a
[master claude ai login](../reference/account-login.md#master-claude-ai-login) launch takes no reservation, so neither
retries. The same retry runs for a worker launch on a hive; only the master lane passes the refused set into
`_master_selection`, and a worker lane drops the refused accounts' quota rows alone.

With the `desk` hive above, suppose the front door relaunches while the previous lead's reservation on the native
login has not yet been released:

| Try | Refused set | Placement | Result |
|---|---|---|---|
| 1 | empty | `desk`, `unrouted`, `interactive` | `account reservation is full: account=claude:unrouted sessions=1 cap=1 five_left=None week_left=None`; the record ends |
| 2 | `claude:unrouted` | `desk`, `ops`, `subscription` | The order is sent; the record is `launching` |

Read the reasons of the ended tries from the endings hash, keyed by execution id:

```bash
redis-cli HGETALL agentihooks:swarm:<slug>:launch-endings
```

```json
{"reason": "account reservation is full: account=claude:unrouted sessions=1 cap=1 five_left=None week_left=None", "generation": 41}
```

Releases without pull request 10731 left the refused set out of `_master_selection`. Dropping the quota row kept a
full subscription slot off the next try, but `place_master` builds the native login candidate from the hive's
`interactive` field, and releases before pull request 11407 offered it to every master seat, ranked ahead of that
hive's slots within the same UI or headless tier. Each retry therefore chose the same login and got the same refusal.
The loop ran until the pass heartbeat found the tick lock stale and raised `SwarmError` with `the tick lock is stale:
pass heartbeat expected token ...`. The `master scale` step holds the host placement lock (`tick.PLACING`) while it
launches, so every other step that takes the lock waited on it, the `spawn` step of those releases among them. In the
evidence for pull request 10731, one `master-2` launch ran `_reselect` 78 times, the seat held 35,141 executions, its
newest 400 all ended with the same `claude:unrouted` refusal, and 30 of 30 `master scale` passes in the tick history
ended with that `SwarmError`. In three passes of the 10 November scale proof
([#10553](https://github.com/the-cloud-clockwork/agentihooks/issues/10553)), `master scale` held the placement lock 37
to 48 seconds while its failing launch used 27 to 35 seconds of CPU, and `spawn` ran past the 33 second budget it had
then, which the issue records. That loop predates the [front door rule](#backend-master-seats), which now keeps
backend seats off the native login; the retry rule covers the front door and any account whose reservation is full.

Measured over the `tick:steps` stream (`own_cpu_s` of `master scale`) and the `spawn` records ended `abandoned`,
before and after the fix:

| Window | `master scale` runs | CPU seconds, sum | CPU seconds, max | Runs at 5 s or more | Abandoned `spawn` runs |
|---|---|---|---|---|---|
| Before | 24 | 392.7 | 45.0 | 11 | 1 |
| After | 43 | 55.3 | 4.8 | 0 | 0 |

The before window was read at 14:35 UTC and the after window runs from 15:20 to 16:46 UTC, on the day pull request
10731 merged. Pull request 11141 later removed the `spawn` step. The after window also ran with `master-per` off from
its start, so not every drop in the master scale runs comes from the retry fix alone.

The cause was found with the [tick step profiler](../reference/tick-engine-steps.md#profile-a-step). The regression
test `test_a_front_door_master_retry_leaves_a_full_native_login_for_a_subscription_slot` fails when a launch calls
`_reselect` more than twice, or when its one retry does not end the native try with the full reservation reason
and order the subscription slot, under the feature map entry `hive-master-refused-account-retry`. The Semgrep incident
rule `master-retry-drops-refused-accounts` flags a `_master_selection` call inside `_select` that does not pass the
refused accounts.

| Symptom | Check | Fix |
|---|---|---|
| `master scale` runs tens of seconds of CPU every pass, the steps that take the placement lock run late (`spawn` is abandoned at its budget on releases that have it), and one master seat gains thousands of executions ended `account reservation is full` for the same login | `HGETALL agentihooks:swarm:<slug>:launch-endings`: the reasons name one account again and again. Then check that the controller's release carries pull request 10731 | Roll the controller onto a release that carries pull request 10731. |
| A pass fails with `the tick lock is stale: pass heartbeat expected token ...` while `master scale` runs long | The `master scale` wall in `agentihooks swarm <slug> tick-report --passes 10` | The step outran the tick lock. With pull request 10731, profile the step as in [Profile a step](../reference/tick-engine-steps.md#profile-a-step) to find the cost. |
| A master launch ends `account reservation is full` after more than one try | `agentihooks hive show <hive>`: `sessions` against each slot's `cap`, for every hive that takes `master` | Every candidate account was full. End a session on one of them, give the hive another `AH_CC_TOKEN_<slug>`, or, when `sessions` counts more than run on the account, wait for the hive's next beat to correct it. |

### Who writes placement

The controller and the ledger server place agents; add, refresh and revoke keep the hive index. A member's Redis user
also writes the agent and execution records, claims and seat records its own engineers change when they run
`swarm done`, `ready`, `park`, `block` and `learned`, and the finding and placement wait state `swarm status` writes; see
[Worker keys](#worker-keys).

| Keys under the swarm root | Member Redis user | Written by |
|---|---|---|
| Agent records: `swarm:<slug>:agents`, `swarm:<slug>:executions`, `swarm:<slug>:state-since` | read and write | The controller and the ledger server; a member's engineers on `done`, `ready`, `park` and `block`. |
| Task claims `swarm:<slug>:claim:*` and placement waits `swarm:<slug>:hive-placement-waits` | read and write | The controller and the ledger server; a member's engineers on `done`, `block` and `status`. |
| Seat records: the exit mark `seat-of:<agent>:exit`, recaps `seat:<seat>:recaps` and learned notes `seat:<seat>:learned` | read and write | The controller and the ledger server; a member's engineers on `done`, `ready`, `park`, `block` and `learned`. |
| Claim journals, task authority, swarm settings, seat culture and names | read only | The controller and the ledger server. |
| Launch slot rows `launch-slot:*` | read only | The controller, which reserves, acknowledges and refreshes them. |
| Hive index `hives` | read only | `hive add` and `hive refresh` add the member; `hive revoke` and a failed add remove it. |
| The member's own hive record and every key below it | read and write | The member's `hive run` beat and the agents it runs. |

The ledger issues an agent token to a member only for an agent whose record names that member as its hive, recorded
from the member's holder record in the hive credential store, which no member reads or writes.

What a member process does differently:

- **Beat.** `hive run` writes the hive record, sessions, quota and execution report under its own hive key. It adds
  itself to the hive index and refreshes launch slots only when it is not a member. A non-member host, such as the
  swarm host itself, still does both.
- **Hive id.** A process uses `AGENTIHOOKS_HIVE_ID` as its hive id while its Redis URL is
  `AGENTIHOOKS_HIVE_REDIS_URL`, so its heartbeat lands under the member's own keys and matches the id the ledger
  checks. Otherwise the hive id is `SWARM_HIVE_ID`, then the seeded hive id, then the host name.
- **Launch orders.** When Redis refuses a write while the order consumer applies a launch order, the consumer writes
  `failed` with the reason `refused` to the member's own acknowledgement hash and drops the order. The `hive run`
  service keeps running and takes the next order.

Check a member's scope from the member, with the credentials `hive add` delivered:

```bash
. ~/.agentihooks/hive.env
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" HGETALL agentihooks:swarm:<slug>:agents             # allowed
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" HSET agentihooks:swarm:<slug>:config max_eng 99     # (error) NOPERM ...
redis-cli -u "$AGENTIHOOKS_HIVE_REDIS_URL" SADD agentihooks:hives forged                         # (error) NOPERM ...
```

`scripts/packaging/hive-join-smoke.sh` runs the same check on Redis 7 after an add. The member's writes to swarm
settings, tasks, another member's keys, the shared inbox, another agent's heartbeat, the hive index and a launch
slot row are refused. A write to `swarm:<slug>:executions`, its beat, an agent heartbeat and an inbox send succeed,
and the member stays in the hive index. `scripts/packaging/compose-hive-smoke.sh` also checks that a member stays
indexed after a Redis restart and a refresh.

Limitations:

- Code on a member host that writes a controller key outside the [worker keys](#worker-keys), such as a launch slot
  row or swarm settings, is refused until it routes through the controller or the ledger.
- Redis ACLs scope by key pattern, never by seat or agent name. A member that can write its own agents' keys can
  also write keys it names for another agent, below its own hive key and in the worker keys, agent records included.

| Symptom | Cause | Fix |
|---|---|---|
| A launch order on a member acknowledges `failed` with `refused` | The launch wrote a key outside the member's keys and the worker keys, such as a launch slot row. | Read the refused key from the member's `hive run` log; a key the launch needs is a bug to file with the key named. |
| A member is missing from `agentihooks hive list` | The hive index no longer holds it, and a member's beat never adds itself back. | Run `agentihooks hive refresh` on the member; refresh adds it to the index again. |
| A member is refused an agent token | The agent's record names another hive, or no hive. | Read the agent's `hive` field with `HGET agentihooks:swarm:<slug>:agents <name>`; only the controller places an agent. |

## Pulling work from the hub

A swarm on the `events` engine (see [Event driven engine](swarm.md#event-driven-engine)) places no agent on a
hive. Hives pull offers from the hub over NATS JetStream instead, through one spoke per swarm and lane (see
[Running a spoke](#running-a-spoke)). On every pass a spoke:

1. Recovers its own unfinished claims.
2. Checks the hive's admission room.
3. Takes one offer from the lane work queue, has it admitted and launches the agent.

A master spoke rechecks its offer against the ledger itself. An eng, ci or plan spoke asks the hub to admit it,
because a member's Redis user cannot claim a task, name an agent or take a seat, and a remote hive makes no service
ledger reads. The hub answers `admit` on the same `slots.<swarm>.<hive>` request subject as account slots, which a
member's credential may publish only for its own hive; a request naming another hive than its subject is refused. It rechecks the offer against the ledger, claims the task,
starts the execution on that hive, occupies a seat, marks the task claimed (or back in `pr`) and returns the primed
spawn task. A repeat from the same hive returns the same admission, so a retried launch keeps its agent; any other
hive gets none. After a failed admit ask or launch, the spoke sends `reopen`, which returns an admitted task to
`open` (see [When a launch fails](#when-a-launch-fails)). The hub answers these requests on a pool of eight threads
and drops one that waited there as long as the hive waits for its answer, so an admission the hive gave up on never
claims the task; see
[How the hub serves requests](../reference/swarm-bus-scope.md#how-the-hub-serves-requests).
Unlike a tick launch, a hub admission scaffolds no task work folder on the hub. It resolves the task's continuation,
stack base, overlaps and group on the claimed row and attaches the handoff transfer the predecessor left (see
[Handoff continuity on admission](#handoff-continuity-on-admission)); the spawn task carries the seat's handoff,
recaps and learned notes.

The hub creates the hive buckets; members are not allowed to create them:

| Bucket | Holds |
|---|---|
| `spoke-work` | One claim per task and generation, keyed by a hash of swarm, task and generation. On the main path its state goes `claimed`, `launching`, `started`, then `exiting` and `exited`. A claim can also be `ready`, `withdrawn`, `dead_pending` or `dead`. |
| `spoke-pending` | The launch slot a spoke reserves before it starts an agent. |
| `hive-presence` | Each hive's heartbeat. |
| `hive-executions` | What each hive reports about its running executions. |
| `execution-starts` | Every `execution.started`. The tick and the reconciler use it to tell hub launched agents from tick launched ones. |
| `inbox` | Message send records and delivery claims for each swarm seat inbox. |
| `account-slots` | Per harness and account, the executions holding one of its sessions and each hive's cap for it. A spoke reserves a slot here before launching, so hives sharing an account stay under its cap. |
| `hive-slot-accounts` | Per hive, the harness and account pairs it has reported, so its beat renews every slot it holds. |

A hive beats `hive-presence` every 15 seconds. A beat expires after 45 seconds.

### Offer order

On each readiness pass the hub reads one snapshot of every `events` swarm and offers each task a lane may claim to
that lane's work queue. `scripts.swarm.readiness.offers` builds the list in one order for the whole swarm, and
`readiness.publish` adds the offers to the lane queues in that order.

A task is offered when all of these hold:

- It names a lane.
- It is not a workstation task, or it is pinned to no hive or to the root hive.
- It is `open`, in scope and not merged into another task, and no claim hold keeps it: an open dependency, its
  phase, a freeze or a focus elsewhere, or its post merge proof window (see
  [How a freeze hold is named](swarm.md#how-a-freeze-hold-is-named)).
- No running agent and no recorded claimant holds it.
- The swarm is `running`, and no release wait, apply wait, fleet red hold or lab wait holds the task.

The offers are sorted by three keys, in this order:

1. **Focused first.** A task one focus record covers goes ahead of every task none covers. A record covers a task
   when its target is a node in the task's ancestry, or a `lane:` or `kind:` selector whose value equals the task's
   lane or kind; see [Focus ranks in offer order](../reference/tick-adapters.md#focus-ranks-in-offer-order) for the
   full rules.
2. **Class, then ledger rank band.** A focused or `urgent` task comes first, by rank band. A task under an operator
   order comes next, oldest order first and by rank band inside one order time: its order time is the earlier of
   its own mark and its phase's, and the work it waits on carries the same time (see
   [Operator orders](swarm.md#operator-orders)). Every other task follows, by rank band. The bands are `urgent`,
   then `high`, then `normal`, then `low`, read from the rank stored on the task row by
   `scripts.swarm_ledger.ledger_rank.order`. A task with no rank counts as `normal`, except a documentation follow
   up (profile `documentation` with `follow_up` set), which counts as `low`. `next` is stored as `urgent`.
3. **Focus record position.** The position of the first focus record that covers the task; a task no record covers
   takes the count of focus records.

Ties keep the order of the task rows in the ledger export. The rank band decides before the focus position, so an
`urgent` task inside a later focus goes out ahead of a `normal` task inside an earlier one.

The sort applies to every task that passes the claim holds above. While a focus has open work, the tasks outside it are
held and not offered, with two kinds of exception that are offered beside the focused tasks and go out after them,
whatever their rank:

- A task of a Doctor swarm's fix phase, which no focus holds.
- A task that joins the focus only because a focused task depends on it, is parked on it, or sits in a phase the
  focused task's phase waits on. It is offered, but no focus record covers it, so it has no focus position.

With no focus record on the ledger, every offer is unfocused: `urgent` tasks go out first, then the tasks under an
operator order, oldest order first, then the rest by rank band, in ledger order inside a band. An ordered `normal`
task goes out ahead of an unordered `high` one.

#### Offer order example

A ledger holds these rows in this order, with three focus records, `tasks/old` first, then `tasks/hot`, then
`tasks/new`, and a Doctor fix phase `p1`:

```text
row   rank     covered by           focus position   offer position
old   normal   focus tasks/old      0                2
hot   urgent   focus tasks/hot      1                1
new   normal   focus tasks/new      2                3
fix   urgent   none, phase p1       3 (outside)      4
```

`hot` goes first on its band, `old` and `new` follow on focus position, and the urgent `fix` task goes out last
because no focus covers it. Clear the three focus records and the order becomes `hot`, `fix`, `old`, `new`: two
urgent tasks and then two normal ones, each pair in ledger order.

#### Changing the offer order

```bash
# Put a phase first in the focus queue, or make it the whole queue
agentihooks swarm <slug> focus phases/<phase id> --top
agentihooks swarm <slug> focus phases/<phase id> --only

# Raise one task's rank band (master, planner or operator)
agentihooks api --slug <slug> --as <name> task set <task id> rank=urgent

# Mark a phase as the operator's order (the master, with his recorded words)
agentihooks api --slug <slug> --as <name> order phases/<phase id> --quote "<his words>"

# Clear a focus or a freeze record
agentihooks swarm <slug> unfreeze phases/<phase id>
```

Who may write each record is in [Freeze and focus](swarm.md#freeze-and-focus) and [Queue rank](swarm.md#queue-rank).
A change reaches the next readiness pass, which reads the ledger again when its revision moved.

#### Offer delivery order

Each lane of a swarm has one JetStream work queue, and a spoke takes one offer at a time from it, oldest first. The
sort orders only the offers one pass adds. Before it adds them, `publish` withdraws every queued offer the snapshot
names as stale: the task is gone, claimed, no longer `open`, at another claim generation, moved to another lane, held,
or kept back by a wait. It then adds each fresh offer whose id is not already queued. The offer id is
`ready:<slug>:<task id>:<claim generation>`, with `:root` appended for a workstation task, so the rank and focus of a
task are not part of it.

The bus also drops a published offer whose id it saw within its dedupe window, two minutes by default
(`dedupe_window_ms` of the bus config). An offer withdrawn and published again under the same id inside that window is
not queued; a pass after the window queues it.

Two things follow:

- An offer queued on an earlier pass stays ahead of the offers a later pass adds. Raising the rank of a task, or
  focusing it, does not move an offer already queued; the new order applies to offers published after the change.
- An offer a spoke hands back waits out a delay before any spoke can take it again, so offers queued after it can go
  out first: five seconds for one whose hive pin or needs the hive cannot meet, and `ack_wait_ms` for an unanswered,
  held or busy one (see [Launch failure limits](#launch-failure-limits)).

Each pass logs one line per swarm on the hub, with the count of claimable offers it built and the count it newly
queued:

```bash
kubectl -n <namespace> logs deployment/agentihooks-swarm-hub | grep 'hub: readiness swarm=<slug>'
# hub: readiness swarm=<slug> offers=6 placed=2
```

#### Offer order tests

| Test | Holds |
|---|---|
| `tests/swarm/test_events_placement.py::test_focused_offers_go_out_by_rank_band_then_focus_record_order` | Inside the focus, rank band decides before focus position |
| `tests/swarm/test_events_placement.py::test_a_focused_offer_goes_out_before_an_urgent_doctor_fix_outside_the_focus` | A focused `normal` offer goes out before an `urgent` Doctor fix task outside the focus |
| `tests/swarm/test_events_placement.py::test_offers_without_a_focus_go_out_by_rank_band_keeping_ledger_order_inside_a_band` | With no focus, rank band first and ledger order inside a band |
| `tests/swarm/test_focus_budget.py::test_the_focus_sort_puts_focused_tasks_ahead_of_an_unfocused_task_in_a_better_band` | `focus_budget.ordered` puts a focused task ahead of an unfocused one in a better band |

All four carry the `swarm-focus` feature mark and are listed under that feature in `feature-map.json`. They run in CI
with the rest of the suite.

#### Offer order limitations

- The dispatcher's claim key does not order offers. At `delegate` and `full` autonomy the capacity decision's ready
  work and preemption sort by `scripts.swarm.dispatcher.claim_key`. That key puts the critical chain guard band
  first. It keeps only these ranks as bands: an operator rank, an `urgent` rank, a `low` rank the swarm set on any
  task, and a documentation follow up with no rank. It orders every other task by its place in the dispatcher's
  queue. The dispatcher writes no rank to the ledger. Offers take the stored rank of every task as its band, a rank a
  master or planner set included.
- A task whose last launch failed keeps its place. The tick's launch order demotes it inside its band; offers do not.
- None of the tie rules inside a rank band from [Queue rank](swarm.md#queue-rank) apply: no longer critical path, no S
  task that unblocks another and no resumed work goes first. Focus record position breaks ties inside a band, then
  ledger order.

#### How the offer order reached the events engine

The legacy tick spawn sorted its launch queue with `focus_budget.ordered`, and `readiness.offers` published offers
in ledger row order. Pull request
[#11136](https://github.com/the-cloud-clockwork/agentihooks/pull/11136) gave the offers the same sort while it mapped
every feature whose tests reached the legacy spawn to tests that would survive its removal. That mapping was the file
`tests/LEGACY_SPAWN_COVERAGE.json`, checked by `tests/swarm/test_legacy_spawn_coverage.py` under the feature
`legacy-spawn-events-coverage`. It listed 23 features. The task had counted 31 from file imports; the pull request
description explains that many of those test files import the place or observe step only for helpers that stay.
16 entries named tests from the feature map that reach none of the spawn, 10 named clauses that retire with the spawn
and 3 named an events engine gap; an entry could name more than one of these.

Pull request [#11141](https://github.com/the-cloud-clockwork/agentihooks/pull/11141) removed the spawn, and the map
and its detector with it. `legacy-spawn-events-coverage` is now a retired id in `feature-map.json`, and
`swarm-events-only-launch` holds the events only engine. The gaps the map named stand as follows:

| Gap named by the map | Now |
|---|---|
| The admission claim carried no handoff document text for a recovery successor | Closed by [#11139](https://github.com/the-cloud-clockwork/agentihooks/pull/11139): admission sets the handoff and its envelope on the claimed row and attaches the predecessor's transfer; see [Handoff continuity on admission](#handoff-continuity-on-admission) |
| The admission claim resolved no continuation, so a reclaimed task did not continue from its recorded branch | Closed by [#11139](https://github.com/the-cloud-clockwork/agentihooks/pull/11139): admission resolves the continuation and stores the reclaim |
| Ordering by the dispatcher's claim key over ledger rank, and the demotion of a task whose launch failed | Open, as listed under [Offer order limitations](#offer-order-limitations) |

#### Offer order troubleshooting

| Symptom | Check |
|---|---|
| An `urgent` task waits while `normal` tasks of the same lane start | Its offer is queued behind offers from earlier passes, or it was handed back. Read the hub `readiness` line: `placed=0` with `offers` above zero means every offer was already queued, or the bus dropped a repeat inside its dedupe window. |
| A focused task starts after a task of a later focus record | The later task has a better rank band; the band decides before the focus position. |
| A task on the critical chain does not start first at `delegate` or `full` autonomy | Offers ignore the chain guard band. Raise the task's rank, or focus it, before its next offer. |
| A task outside the focus is offered while the focus has open work | It is a Doctor fix phase task, or a focused task waits on it. Read its `phase`, and the `depends_on` and `parked_on` of the focused tasks. |
| No offer for a task that looks ready | A claim hold, a recorded claimant or a wait holds it, or the swarm is not `running`; [Freeze and focus](swarm.md#freeze-and-focus) and `agentihooks swarm <slug> status` name the holds. |

### When a launch fails

An admission claims the task, starts its execution, occupies a seat and marks the ledger row `claimed` before the
hive launches anything. When the launch then fails, the hive gives the admission back with `reopen`, so the task
returns to `open` and is offered again: on the same poll for most failures, on the next poll for a launch the spoke
left `launching`, and through the dead letter on the offer's last delivery. An admit ask the hub did not answer and a
held answer send no `reopen`; the table below lists every case. Every eng, ci and plan spoke does this on every
`events` swarm; no setting turns it on or off. A master spoke admits nothing on the hub and is outside this path.

Before pull request [#11051](https://github.com/the-cloud-clockwork/agentihooks/pull/11051) the spoke reset only
its own claim and nacked the offer. The hub kept the admission, its readiness pass withdrew the offer because the
task was claimed, and every other hive's `admit` was refused as `already admitted to hive <hive>`. The task then
waited for the tick to retire the admitted agent with `its hive no longer lists it`, six minutes or more after the
launch (see [A hive worker whose pane is gone](#a-hive-worker-whose-pane-is-gone)). The investigation in pull request
#11051 found eight engineers of one swarm lost that way on 13 November, each for about eight minutes, after admit
asks that timed out, ledger authentication errors and launch refusals.

#### What the spoke does after a failure

The spoke reads its claim in `spoke-work` after the failure and acts on it. While the offer has deliveries left:

| Outcome of the offer | Spoke claim after it | What the spoke asks the hub | The delivery |
|---|---|---|---|
| The `admit` ask gets no answer inside the 10,000 millisecond request wait | `asked`, owned by this hive; the launch slot in `spoke-pending` stays reserved | Nothing on this poll. The next poll asks `admit` again and receives the admission the hub kept | Nacked for `ack_wait_ms` |
| The hub answers held: the hive is at its max agents, or another admission for the hive held its turn past the wait (`another admission for the hive is in progress`) | `ready`; the launch slot is freed | Nothing: the hub wrote no admission | Nacked for `ack_wait_ms`, so another hive may take it |
| The hub answers the `admit` ask with an error | `ready`; the launch slot is freed | `reopen` | Nacked for the backoff step |
| The hive cannot start its Serena router after the hub admitted the offer | `ready`; the launch slot is freed | `reopen` | Nacked for the backoff step |
| The runtime refuses the launch with `refused`, `ambiguous` or `unsupported` | `ready`; the launch slot is freed | `failed` with the harness and the runtime's detail, then `reopen` | Nacked for the backoff step |
| The runtime answers `unavailable` | `ready`; the launch slot is freed | `reopen` | Nacked for the backoff step |
| An error after the spoke wrote the launch intent | `launching`, owned by this hive, with the agent's name | Nothing on this poll; the next poll's recovery ends the agent and sends `reopen` (see [Recovering a launch the spoke left](#recovering-a-launch-the-spoke-left)) | Nacked for the backoff step |

The backoff step is the `backoff_ms` entry for the delivery's attempt, the last entry once attempts outrun the list,
and `ack_wait_ms` when the list is empty, as it is for every lane queue. Each failure also logs one line on the hive:
`hive spoke for swarm <slug> lane <lane> poll failed (<error type>: <error>)`.

`failed` goes first because the hub records it only against an admission the asking hive still holds. It stores the
reason in the swarm's `launch-failures` hash and, when the runtime named a harness, that harness in
`launch-failure-harness`, then logs `hub: events steering swarm=<slug> task=<task id> avoid=<harness>: <reason>`. The
task's next admission, on any hive, carries `avoid_harness`, so its launch picks another harness. A runtime that
answers `unavailable` sends no `failed` and steers nothing.

On the offer's last delivery, `max_deliver`, the spoke sends no `reopen` on the failure itself. It dead letters the
offer to `dead.<swarm>` and terminates it, and the dead letter sends `reopen` with `dead` set, which releases a held
admission the same way. A claim still `launching` at that point dead letters on the next poll, as soon as that
poll's recovery has ended the leftover agent.

#### What `reopen` releases

The hub finds the admission it kept for the offer under `spoke-admission:<task id>:<generation>`. When the asking
hive holds it, the hub releases it as the `reopen` row of [Handing the transfer back](#handing-the-transfer-back)
lists. The ledger write applies only while the row is `claimed` or `pr` and still names that agent. The hub then
deletes the admission key and answers `true`.

The change of claimant raises the task's claim generation (see
[Event driven engine](swarm.md#event-driven-engine)), so the nacked offer comes back stale and is withdrawn, and
the hub's readiness pass offers the task again under a new offer id. A `reopen` from a hive that does not hold the
admission, or a repeat after the release, answers `false` and changes nothing. A dead letter `reopen` that finds no
admission raises the claim generation of a task that is still `open` and unclaimed at that generation, so the next
offer carries a fresh claim key.

#### Recovering a launch the spoke left

Every poll starts by recovering the launch slot the spoke reserved in `spoke-pending`. A claim this hive left
`launching`, because the spoke raised or stopped after it wrote the launch intent, is recovered in three steps:

1. The spoke looks up the agent named in the claim and reaps it when it exists. When the lookup or the reap fails,
   the poll answers `uncertain` and the spoke takes no new work until a later poll gets past this step.
2. Below the delivery cap it sends `reopen`, which releases the admission as above.
3. It sets the claim to `ready` and frees the launch slot, and the same poll goes on to take new work.

At the cap, step 2 is skipped and the claim dead letters, whose `reopen` with `dead` set releases the admission.

A claim left `asked`, or `claimed` for longer than one 10,000 millisecond request wait, is never reopened: the hub
may hold an admission for it, so the spoke asks `admit` again and launches the admission it receives.

#### Late admissions keep their claim

An admission that finishes past the answer budget keeps its claim; see
[Handing the transfer back](#handing-the-transfer-back). The budget counts from the request's arrival, so time
queued for a hub serve thread is included. The hive whose ask timed out holds its claim as `asked` and receives
that admission on its next poll. Pull request #11051 also released and refused a late admission; pull
request [#11511](https://github.com/the-cloud-clockwork/agentihooks/pull/11511) reverted that part, because the
release raised the claim generation and withdrew the offer, so on 16 November no urgent ci task kept a live
claimant.

#### Launch failure limits

| Limit | Value | What it does |
|---|---|---|
| `max_deliver` | 5 | Deliveries of one lane offer. Failures below it send `reopen`; a failure on the last one dead letters the offer. |
| `ack_wait_ms` | 30,000 ms | An offer not acknowledged within it is delivered again. It is also the nack delay for an unanswered, held or busy offer, and for a failure when `backoff_ms` is empty. |
| `backoff_ms` | empty | Nack delay per attempt after a failure. |
| Slot request wait | 10,000 ms | How long a hive waits for the hub to answer `admit`, `reopen`, `failed` or `launched`. |
| Answer budget | 8,000 ms | The slot request wait less two seconds. An admission that finishes past it logs `late:` and keeps its claim. |
| Gone window | 6 minutes | How long after the launch the tick may retire an admitted agent its hive does not list. With `reopen` this retire no longer has to free a failed launch. |

The first three are the `QueuePolicy` fields. Lane work queues and lane spokes use its defaults, and no environment
variable or swarm setting changes them; the others are constants in the hive and hub code. A change to any of them
is a code change.

#### Checking a launch failure on a live swarm

```bash
# The hive's failed polls: a worker hive pod, then a workstation or VM hive
kubectl logs deployment/<release>-worker-hive -n <namespace> | grep 'poll failed'
journalctl --user -u agentihooks-hive --since "1 hour ago" | grep 'poll failed'
# hive spoke for swarm <slug> lane eng poll failed (RuntimeError: spawn refused: <detail>)
# hive spoke for swarm <slug> lane eng poll failed (Unanswered: <reason>)

# Harness steering and refused admissions on the hub
kubectl -n <namespace> logs deployment/agentihooks-swarm-hub | grep -E 'hub: events (steering|admission .* refused)'
# hub: events steering swarm=<slug> task=<task id> avoid=codex: <detail>

# The admission the hub keeps for an offer: no value once reopen released it
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET agentihooks:swarm:<slug>:spoke-admission:<task id>:<generation>
# The task's last launch failure, and the harness its next admission avoids
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HGET agentihooks:swarm:<slug>:launch-failures <task id>
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HGET agentihooks:swarm:<slug>:launch-failure-harness <task id>

# The task row after the release: open, no claimant, a higher claim generation
agentihooks ledger --slug <slug> get tasks/<task id> | jq '{state, claimed_by, claim_generation}'
```

A release has worked when, seconds after a `poll failed` line that is not `Unanswered`, the admission key has no
value and the task row reads `open` with an empty `claimed_by`. The tick then logs no
`retired <agent>: its hive no longer lists it` for that agent.

#### Upgrading for the launch failure release

The release runs in each hive's spoke, so a hive on a release from before #11051 still strands its failed
admissions. The hub refuses every `admit` from a member hive whose reported release is below its own, logging
`hub: events admission swarm=<slug> task=<task id> hive=<hive> refused: release <member> below hub release <hub>`,
so roll every hive image whenever the hub rolls.

#### Launch failure guards

- Feature `events-failed-launch-releases-admission` in `feature-map.json`. Its tests in `tests/hive/test_spoke.py`
  cover a failed admit ask and a failed launch that each send one `reopen` and are taken again after the backoff,
  a launch refused on a harness that sends `failed` before `reopen`, and a recovered own `launching` claim that
  sends `reopen` below the cap and dead letters at it. Pull request #11051 added no Semgrep rule for it: the defect
  was a missing call on a failure path, and the launch slot rollback before any admission correctly has none, so no
  code pattern tells the two apart.
- Feature `hub-admission-late-answer-keeps-claim`, with the Semgrep incident rule
  `hub-admission-late-answer-releases-claim`, scoped to `scripts/hub/admission.py`: a `reopen`, `_release` or
  `_unclaim` call inside `Admission._answered` is an error. Run it by hand with:

```bash
semgrep scan --config .semgrep-incidents/hub-admission-late-answer-releases-claim.yml scripts/hub/
semgrep --test --config .semgrep-incidents/hub-admission-late-answer-releases-claim.yml \
  .semgrep-incidents/hub-admission-late-answer-releases-claim.py
```

#### Launch failure troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| An eng, ci or plan task stays `claimed` by an agent no hive lists, its hive logged `poll failed`, and the tick retires the agent with `its hive no longer lists it` six minutes or more after the launch | The hive runs a release from before #11051, whose spoke gave back only its own claim | Roll the hive to the hub's release. Until it rolls, drain it with `agentihooks hive set <hive> draining=yes --keep`. |
| The hub logs `refused: release <member> below hub release <hub>` for every admit of one hive | The hive runs an older release than the hub, and the hub refuses it so an old spoke cannot strand a claim | Roll that hive, or, for a hive meant to stay on one release, set `workers.pinned: true` in its chart so its record reads `pinned=yes`. |
| A task's claim generation climbs by two on each attempt and no agent runs | Each admission's launch fails, and its `reopen` returns the task; both changes of claimant raise the generation | Read the hive's `poll failed` lines and the task's `launch-failures` entry, and fix the launch cause. After `max_deliver` failed deliveries of one offer, the offer dead letters. |
| The hive logs `poll failed (Unanswered: ...)` and the task stays claimed by an agent that has not launched | The hub finished the admission after the hive stopped waiting and kept it | Nothing to undo: the hive's next poll asks again and launches that admission. Find what slowed the hub in its `late:` lines. |
| A task launches again on the harness that just failed | The runtime answered `unavailable`, which sends no `failed`, or its refusal named no harness | Expected: only a refusal that names its harness steers the next admission. |

### Handoff continuity on admission

An eng, ci or plan successor the hub admits continues from what its predecessor left: the handoff text, its runtime
envelope and the handoff transfer travel in the spawn task the hub returns to the hive. No setting changes this; the
admission adds them on every claim.

#### What the admission adds

The admission takes the seat of a saved launch assignment, else the seat the handoff names; a preferred seat that is
taken gives way to the lowest free seat of the lane. After it occupies the seat, it reads the task's stored handoff.

| Stored for the task | The spawn task carries |
|---|---|
| A handoff text: one written by `agentihooks swarm <slug> handoff`, `ready` or `park`, or the recovery handoff `agentihooks hive remove` stores for each agent that did not hand off within its wait | `handoff`, the text, and `handoff_envelope`, the stored envelope resolved on the claimed row so a cleared protected branch is never continued |
| No handoff, but earlier lives or a recorded branch | `reclaim`, the branch the task continues from, or the decision to start fresh, and why; also stored for the admitted agent |
| Neither, on a first life | Nothing |

The admission then attaches the transfer. It takes the latest row of the task in the swarm's transfers hash that has
no successor yet, whatever recorded it, and sets on it:

| Field | Value |
|---|---|
| `successor` | The admitted agent's name |
| `seat` | The seat the agent occupies |
| `generation` | That seat's occupancy generation |
| `attached_at` | The attach time, in milliseconds |

The attached row is the spawn task's `transfer`. A task with no open transfer carries `"transfer": null`. The
admission records no transfer row of its own. An attached row stays with its successor until `reopen` or the launch
check free detaches it, unless its binding is already `live` (see
[Handing the transfer back](#handing-the-transfer-back)); the next admission then attaches that same row.

#### What the successor reads

The hive launches the spawn task as it is:

- The seat history in the opening prompt starts with the transfer: `Read this handoff before continuing. Confirm you
  read it and will take its first Next action by running agentihooks swarm <slug> confirm-handoff <transfer id> --next
  '<first Next action>'`, followed by the handoff text. Item 1 of the history then shows the handoff runtime envelope
  and `Handoff document: continue from it:` with the text. A transfer with no handoff text adds no confirm
  instruction, and a task with no handoff reads `1. Handoff document: none was left for this task.`
- When a transfer is attached and the envelope names the predecessor's `conversation_id`, the launch sets
  `AGENTIHOOKS_PREDECESSOR_SESSION` to it, and traces record it as `predecessor.session.id` (see
  [Observability with Langfuse](../reference/observability-langfuse.md)). With no transfer the launch names no
  predecessor.
- A transfer that names `avoid_harness`, as a content refusal or an outage handoff records, steers the successor's
  harness choice away from that harness.

#### When the transfer binds

Attach leaves `binding` at `pending` and `continuity` as the predecessor's handoff recorded it. The tick's
`handoff transfers` step, in the `close` group of every pass (see
[tick engine steps](../reference/tick-engine-steps.md)), skips a successor that is still `starting` or `launching`.
Once the seat's occupant and generation still match the row, it marks binding `live` when the successor runs, either
listed live by the runtime or reported with an execution by its hive, and `absent` otherwise. Continuity stays apart
from binding: it waits on `confirm-handoff` (see [swarm commands](swarm.md#commands)).

#### Handing the transfer back

Every path that returns an admitted task to `open` detaches its transfer before it drops the claim:

| Path | When | What happens |
|---|---|---|
| `reopen` | The spoke gives the offer back after a failed launch while deliveries remain, or dead letters the final delivery | The admission cancels the pending life, detaches the transfer, drops the claim and the agent, and sets the ledger row back to `open` with `claimed_by` empty. The row keeps its id, handoff text and `pending` binding with an empty `successor`, so the next admission attaches the same row. A repeated `reopen` finds no admission, answers `false` and leaves the transfer as it is; when the repeat is the dead letter of the final delivery, the hub may raise the task's claim generation instead. |
| Launch check free | The hub launch check halts an admitted agent that no hive started | The same steps, after the agent's hive slot is freed. |
| Refused attach | Another writer changed the transfers hash while the admission attached, so attach raised `The transfer changed; retry the command` | The same steps. The refused write never reached the row, so it keeps an empty `successor`, and the admission keeps nothing for the offer. The hive's admit ask is answered with that error; the task is `open` with no claimant, so the next ask for the offer starts clean. |

Detach clears `successor` only on a row whose binding is not `live`, so a transfer that already bound to a running
successor stays with it. A launch failure the hive reports with `failed` leaves the transfer attached; the `reopen`
that gives the offer back releases it.

An admission that finishes past its 8,000 millisecond answer budget, the 10,000 millisecond slot request timeout less
two seconds, keeps its claim and its attached transfer. The hub logs
`hub: events admission swarm=<slug> task=<task id> hive=<hive> late: finished after <n> ms, past the 8000 ms answer
budget, claim kept <step timings>`. A hive still waiting receives the admission; a hive that stopped waiting asks
again for the same offer on its next poll and receives the same admission, transfer included.

#### Checking continuity on a live swarm

```bash
# The task's transfers: who left each one, which successor holds it, and its binding and continuity
agentihooks swarm <slug> status --json \
  | jq -c '.transfers[] | select(.task == "<task id>")
      | {id, reason, predecessor, successor, seat, generation, binding, continuity}'
# {"id":"<transfer id>","reason":"recycle","predecessor":"engineer@<code>-<n>","successor":"engineer@<code>-<m>",
#  "seat":"eng-1@<slug>","generation":7,"binding":{"state":"live",...},"continuity":{"state":"pending"}}

# The same rows and the stored handoff text, straight from the swarm Redis
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HGETALL agentihooks:swarm:<slug>:transfers
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET agentihooks:swarm:<slug>:handoff:<task id>

# Admissions the hub finished late and kept
kubectl -n <namespace> logs deployment/agentihooks-swarm-hub | grep 'hub: events admission .* late:'
```

The keys start with `AGENTIHOOKS_SWARM_KEY_PREFIX`, default `agentihooks`. A row with a `successor` and binding
`pending` belongs to a successor the tick has not yet seen running. A row with an empty `successor` waits for the
task's next admission.

#### Continuity guards

- Feature `hub-admission-claim-continuity` in `feature-map.json`. Its tests in `tests/hub/test_admission.py` cover
  the attach after a handoff and after a recovery handoff, a task with no transfer, a reopen that hands the transfer
  to the next successor, a repeated reopen, a late admission that keeps its transfer, and a refused attach that
  releases the claim, the agent and the ledger row.
- Semgrep incident rule `admission-claim-skips-continuity`, scoped to `scripts/hub/`: a function that calls
  `tick.primed` with no `transfers.attach` in the same function is an error. The Semgrep workflow runs every rule in
  `.semgrep-incidents/`. Run it by hand with:

```bash
semgrep scan --config .semgrep-incidents/admission-claim-skips-continuity.yml scripts/hub/
semgrep --test --config .semgrep-incidents/admission-claim-skips-continuity.yml \
  .semgrep-incidents/admission-claim-skips-continuity.py
```

#### Continuity limitations

- Masters are outside this path: a master spoke rechecks its own offer, and the master launch attaches the master
  seat's transfer itself.
- The admission attaches only the latest open row of the task. An older row with no successor stays unattached.

#### Continuity troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| An events successor's opening has no `confirm-handoff` line, and the task's transfer keeps an empty `successor` while the successor runs | The hub runs a release from before pull request [#11138](https://github.com/the-cloud-clockwork/agentihooks/pull/11138), whose admission attached no transfer | Roll the hub to the newest release. The running successor goes on without the transfer; the open row attaches to the task's next admission. |
| An events successor reads `1. Handoff document: none was left for this task.` although its predecessor handed off | The hub runs a release from before pull request [#11139](https://github.com/the-cloud-clockwork/agentihooks/pull/11139), whose admission read no handoff, or no handoff text is stored for the task | Read the handoff key above. Roll the hub when the text is there. |
| The hive's admit ask is answered `The transfer changed; retry the command` | Another writer changed the swarm's transfers while the admission attached | Nothing to fix: the admission released its claim, agent and ledger row, and the next ask for the offer admits again. |
| A transfer names a successor that never launched | The admission is kept, because it finished late or the hive's launch is still running | The hive asks again and launches it. Otherwise `reopen` or the launch check free hands the transfer back; see [Handing the transfer back](#handing-the-transfer-back). |

### Saved account on a hive launch

A successor can carry a saved account. It is the `account` of the task's relaunch assignment (`launch_assignment`)
or, when the task has none, the `account` in the `launch` block of the predecessor's handoff envelope. On a launch
from a hive spoke the saved account is only a preference. The launch passes `--route <account>` only while the hub's
account slot holds or takes a worker seat on that account for the launching execution. When the account has no free
worker seat, the launch passes no `--route` and the agent's router picks the account the usual way (see
[Launch routing](load-balancing.md#launch-routing)). A full account therefore never pins a successor. Every launch
from a hive spoke does this unless it takes a route listed below, and no setting turns it off.

The check runs in the swarm runtime's spawn, before any process starts. The spoke writes its own hive into every
spawn task it launches (`account_slot_hive`), next to the execution the hub started for it. For such a task the
runtime sends the hub one `take` on the hive's slot subject, with that execution, the harness and the saved account
as `preferred` (see
[Hub slot requests and the hive binding](../reference/swarm-bus-scope.md#hub-slot-requests-and-the-hive-binding)).
The hub considers only the saved account:

| What the hub finds for the saved account | Hub answer | The launch |
|---|---|---|
| The execution already holds one of its slots | The account | `--route <account>` |
| A free worker seat | The account, after it adds a hold for the execution | `--route <account>`. The launched agent's own slot take for the same execution finds that hold, so the launch uses one seat, not two. |
| No free worker seat: its worker holds plus the sessions no hold accounts for have reached the smallest cap any hive reports for it, or that cap is 0 | `null` | No `--route` |
| The hive's sessions report lists no slot for that harness and account | `null` | No `--route` |
| The request fails, for example on a timeout or with no responders | An error | Refused as `unavailable` with `Account slot admission failed`; see [When the hub cannot answer](#when-the-hub-cannot-answer) |

A master's hold is not counted against the worker seats.

#### Which launches check the saved account

| Launch | Route it passes |
|---|---|
| A front door master or a launch on the interactive route | That route. The saved account is not checked. |
| A quota repick: the runtime holds the swarm's quota account rows, or the hub admitted a relaunch whose handoff reason is `quota` | `--route` with the account the repick chose. No `take` is sent for the saved account. |
| A task without `account_slot_hive`, such as a tick launch | `--route <saved account>` with no slot check, as before |
| A hive spoke launch with a saved account | Decided by the table above |
| A launch with no saved account | No `--route` |

For a quota handoff the hub did not admit with an account, the runtime first replaces the saved account with the
successor account it selects (see [Successor account selection](load-balancing.md#successor-account-selection)).
That account then goes through the same check.

With no `--route`, `agenti` picks a Claude account and the Codex router a Codex account by the rules in
[Launch routing](load-balancing.md#launch-routing). Because the launch runs with `AGENTIHOOKS_SLOT_HIVE` set, it then
takes a hive account slot on the account it picked, as every routed launch on a hive does (see
[Hives in the account login reference](../reference/account-login.md#hives)).

#### When the hub cannot answer

Any failure of the slot request is raised as `Unavailable("Account slot admission failed")`, and the runtime refuses
the spawn with status `unavailable` before it starts a process. The spoke then acts as for every `unavailable`
answer (see [What the spoke does after a failure](#what-the-spoke-does-after-a-failure)). The claim goes back to
`ready`, the launch slot is freed and the spoke sends `reopen` but no `failed`. The harness is therefore not steered,
and no launch failure is recorded for the task. The hive logs:

```text
hive spoke for swarm <slug> lane eng poll failed (RuntimeError: spawn unavailable: Account slot admission failed)
```

#### Before the saved account check

Before pull request [#11264](https://github.com/the-cloud-clockwork/agentihooks/pull/11264), a hive launch passed
the saved account as `--route` whenever the saved launch named one. On 14 November one task's exit handoff saved the
account reserved for the master, which was already at 6 of 6 sessions. Every successor launched with that route. The
hive account slot refused it with `No account slot available` within seconds, and `init-agent` waited out its 270
second profile canary deadline before the task passed to the next owner with the same saved account. The task used
up seven owners in 40 minutes.

#### Checking the saved account on a live swarm

```bash
# The launch the predecessor saved, and its account
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET agentihooks:swarm:<slug>:handoff-envelope:<task id> | jq '.launch'
# {"profile": "engineer", "harness": "claude", "account": "<account>", ...}

# The slots and caps of the hive that launched the successor
agentihooks hive show <hive-id>

# Launches refused because the hub did not answer the slot request: a worker hive pod, then a workstation or VM hive
kubectl logs deployment/<release>-worker-hive -n <namespace> | grep 'spawn unavailable: Account slot admission failed'
journalctl --user -u agentihooks-hive --since "1 hour ago" | grep 'spawn unavailable: Account slot admission failed'
```

The keys start with `AGENTIHOOKS_SWARM_KEY_PREFIX`, default `agentihooks`. A successor that runs on another account
than the envelope names had no free worker seat on the saved account, or was placed by a quota repick.

#### Saved account guards

- Feature `hive-saved-account-slot-preference` in `feature-map.json`. Its tests in
  `tests/swarm/test_runtime_account_slots.py` cover a saved account that routes only while its slot has a worker
  seat, a quota repick that sends no `take` for the saved account, an admitted relaunch outside a quota handoff that
  still needs a worker seat, and a slot request the hub cannot answer, which refuses the launch as `unavailable`
  before any process starts. Its tests in `tests/hive/test_account_slots.py` cover the hub's answer from the
  account's cap and sessions, a failed request raised as `Unavailable`, and the request going to the hub of the bound
  swarm on the hive's own subject.
- Semgrep incident rule `hive-saved-account-route-unchecked`, scoped to `scripts/swarm/`: inside a function named
  `spawn`, a route built as `["--route", saved["account"]] if saved.get("account") else ...` is an error. The Semgrep
  workflow runs every rule in `.semgrep-incidents/`. Run it by hand with:

```bash
semgrep scan --config .semgrep-incidents/hive-saved-account-route-unchecked.yml scripts/swarm/
semgrep --test --config .semgrep-incidents/hive-saved-account-route-unchecked.yml \
  .semgrep-incidents/hive-saved-account-route-unchecked.py
```

#### Saved account limitations

- Only the saved account is checked before the launch. When the account the router then picks has no free hive slot
  either, the launch still ends with `No account slot available`.
- The spawn does not release the hold it took when the launch fails before the agent starts. The hold lapses after
  45 seconds, or sooner once the hive's heartbeat reports the execution exited or dead.

#### Saved account troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Each successor of one task ends within seconds with `No account slot available`, `init-agent` waits out the profile canary deadline each time, and the handoff envelope names the same account | The hive runs a release from before #11264, which routed the saved account with no slot check | Roll the hive to the hub's release. |
| A successor runs on an account other than the one its predecessor saved | The saved account had no free worker seat, or a quota repick chose another account | Expected. Compare the account's sessions and cap in `agentihooks hive show <hive-id>`. |
| The hive logs `spawn unavailable: Account slot admission failed` | The hub did not answer the slot request | Check that the hub runs and serves the swarm's account slots; see the slot request rows in [Hub slot requests and the hive binding](../reference/swarm-bus-scope.md#hub-slot-requests-and-the-hive-binding). The `reopen` offers the task again. |
| A successor skipped its saved account and still ends with `No account slot available` | The account the router picked has no free hive slot either | Wait for a session on the account to end, or add another account to the hive; see [Account caps on every hive](#account-caps-on-every-hive). |

### When a hive is lost

The hub's `hive-loss` reactor keeps an index of every started execution, by hive. A hive counts as lost when both of
these hold:

- its presence has expired;
- this hub has watched presence for a full 45 seconds with no gap longer than one beat;
- its swarm's config engine is `events`, and this hub has seen it on `events` for a full 45 seconds.

Only spokes beat presence, and they poll only swarms on the `events` engine, so a swarm switched to `legacy` keeps
its running executions however long its hives stay silent. After a switch back to `events` its spokes beat again
before the 45 seconds pass. When the hub cannot read the swarm store, it keeps the swarms it last read and prints
`hub: hive loss kept its last events swarms; the store read failed (<error>)`.

The hub then publishes `execution.exited` with reason `hive-lost` for each of that hive's executions that are still
running. The `lifecycle` reactor releases each one and reopens its task, which raises the claim generation, so another
hive can pull the next offer.

A spoke also ignores a claim or launch held by a hive that is no longer present. Every five minutes, the `reconciler`
reactor rebuilds any missing `execution.started` from the swarm's agent records. It ends any execution
that a hive reports gone. An execution on a lost hive ends with reason `lost`.

#### The hub's lost hive line

Once the index no longer holds the lost hive's executions, the hub prints one line on stderr for each hive it
judged lost in that poll. The line names the swarm, the hive and every execution it exited, each with its task:

```text
hub: hive hive-a of swarm rig lost; exited execution execution-a of task task, execution execution-b of task task-b
```

The hub prints no line when:

- the hive still beats;
- every start it held for the hive had already exited or been replaced by a newer claim, so the hub dropped the
  starts without publishing an exit;
- the index update failed after eight contended attempts, which raises `hive loss index remained contended` instead.

Read the line from the hub's log:

```bash
kubectl -n <namespace> logs deployment/agentihooks-swarm-hub | grep 'hub: hive '
```

#### Unplanned loss drill

The drill proves the path above on a live swarm by removing a hive without warning:

1. Pick a live hive that holds claimed tasks: `agentihooks hive list` shows it `live`, and
   `agentihooks swarm <slug> status` shows its agents on claimed tasks. Note each task and its claimant.
2. Kill the hive's pod with no drain or handoff: `kubectl -n <namespace> delete pod <hive pod> --grace-period=0 --force`.
   Record the time.
3. Within one presence window plus one poll, the hub prints its lost hive line naming every execution of that hive.
   Record the line with its time.
4. Each named task reopens with a raised claim generation, and another seat claims it from the next offer. Record
   each reopen and reclaim time from the task's ledger history.
5. Watch `agentihooks swarm <slug> status` for fifteen minutes. The drill passes when no claim still names the lost
   hive's agents and no idle or stale claim finding names its tasks.

Put the commands, the hub line and the timestamps on the task proof.

### Stopping an agent whose work ended

An agent that finishes its task stays at its prompt with its session running, so its hive never sees it exit. The
hub's `lifecycle` reactor acts only on `execution.exited`, which a hive publishes when the agent's pane dies or when
the hive honors an execution stop. The tick's `reap` step therefore sends that stop. On every pass it judges each
agent whose hive recorded its `execution.started`, and for an agent whose work ended it sends the execution stop
with reason `ended` to that hive in the same pass. It sends the stop whether or not the controller can see the
agent's pane, because the pane lives on the hive. A pass that reaches its deadline judges no further agent and
names how many it left for the next pass.

Before [pull request #11464](https://github.com/the-cloud-clockwork/agentihooks/pull/11464) nothing sent that
stop, so a finished agent kept its seat, its launch slot and its place under its hive's max agents until its pane
was closed by hand, and a hive full of finished agents admitted no new work. The controller cannot close such a
pane itself: it runs in its own pod, where its pane lookups fail with `no herdr server is running`.

#### What counts as ended

The reap step sends nothing for an agent in any of these states:

- `awaiting-decision` or `retiring`;
- a master still `starting`;
- an agent whose launch acknowledgement is still being collected.

Otherwise the agent's work has ended when any of these holds:

| Condition | Agents it covers |
|---|---|
| The agent record reads `finished`, as after the agent's own `swarm done` or `swarm ready` | Every lane, masters included |
| The task row has `done` set, or its state is `done`, `blocked` or `handoff` | Every lane except the master |
| The task awaits verify and has no claimant | Every lane except the master |
| The task's claimant is no longer this agent, because it is empty or names another agent | Every lane except the master, for an agent run by another hive, by a runtime that is not local, or with a recorded host |

An agent whose work has not ended and whose pane the controller lists as live goes through the same idle and revoked
seat watch as an agent the tick launched. One whose pane the controller cannot list gets no action from the reap
step; the hub reconciler and the lost hive rules above cover it.

#### The stop and what the tick reports

The stop is an `execution.stop` event published on the swarm's stop stream, on the subject ending in
`execution.stop.<hive>`, which only that hive reads: a member credential may fetch and acknowledge its own hive's
stop consumer and no other, and only the hub publishes stops.
[Execution stops](../reference/swarm-bus-scope.md#execution-stops) lists the stream, the consumer names and the
grants. The stop's id is `stop:<started event id>`, and its data names the agent, the hive and the reason.
The first stop sent for a hive creates that hive's durable stop consumer; until then the hive's stop reader finds
nothing and tries again on its next pass.

| Stop answer | Tick action | Next |
|---|---|---|
| `sent` | `sent the stop for <agent> to its hive: its work ended` | The hive reaps the agent. |
| `pending` | None | The same stop went out less than 120 seconds ago, the bus dedupe window. A pass after the window sends it again while the agent is still seated. |
| `ended` | None | The execution already ended: its exit is out, or its spoke claim is gone, has left the `started` state it launched under or carries another claim revision. Nothing is sent. |
| `unknown` | None | No started event on the bus matches the agent's current execution. |
| `failed (<error>)` | `could not send the stop for <agent>, retrying next tick: failed (<error>)` | The bus refused the stop. The next pass judges the agent again and sends a new stop. |

Every stop the tick sends also prints one line on its stderr, which is the controller log in a cluster install:

```text
hub execution stop for engineer@<code>-<n> in swarm <slug> after ended: sent
```

#### How the hive honors the stop

Every hive reads its stops at the start of each lane loop pass, for every `events` swarm, and keeps reading while
it drains. That loop runs inside `agentihooks hive run` on a worker hive and in the lane spokes on the workstation.
For each stop it reads, the hive:

1. checks that the stop names an execution this hive started, and acknowledges and ignores any other stop;
2. reaps the agent's session through its own runtime. A failed reap prints
   `hive execution stop for <agent> in swarm <slug> could not reap it; retrying`, and the stop comes back after 30
   seconds until the reap succeeds, up to 100,000 deliveries;
3. publishes `execution.exited` with id `exited:<started event id>`, `reason` `stopped` and `stop_reason` `ended`.
   When the execution already ended, because its exit is out or its spoke claim is gone, has left the `started`
   state it launched under or carries another claim revision, the hive still reaps the agent and publishes nothing
   more.

The hub's `lifecycle` reactor then acts on that exit for the claim and generation the execution started:

1. It moves the spoke claim to `exiting`.
2. It removes the agent record, which frees the seat, and in the same Redis transaction releases the hive launch
   slot and, unless the task's claim authority is held, the task claim.
3. It settles a task the agent still holds in `claimed` or `pr`: `done` when the task row is done, otherwise `open`
   with no claimant. It leaves alone a task that waits on a merge, one on a newer claim generation and one in any
   other state.
4. It marks the spoke claim `exited`.

The agent leaves `agentihooks swarm <slug> status`, and its seat and launch slot are free for the next launch.

#### Upgrade

Both sides must run current code:

- The controller, or the workstation tick in local mode, needs a release carrying pull request #11464 to send the
  stop.
- Each hive service needs a release carrying the execution stop reader. A hive process loads its code when it
  starts, so a hive service started before its release carried the reader honors no stop until it restarts:
  `systemctl --user restart agentihooks-hive` on a workstation or VM, a pod roll on Kubernetes.

#### Checking it on a live swarm

```bash
# One line per stop the controller's reap step sent in the last hour
kubectl -n <namespace> logs deploy/agentihooks-swarm-controller --since=1h | grep 'hub execution stop for'
# hub execution stop for engineer@<code>-<n> in swarm <slug> after ended: sent

# Once the hive honored it, the agent is gone from the swarm
agentihooks swarm <slug> status

# On the hive: reaps that failed and stop reads that failed
journalctl --user -u agentihooks-hive --since "1 hour ago" | grep 'hive execution stop'
```

The feature map entry `events-ended-agent-stopped` lists the regression tests for this path. The Semgrep incident
rule `admitted-ended-agent-unstopped` fails a reap judge for hub admitted agents that returns no action for an agent
whose work ended.

#### Troubleshooting ended agents

| Symptom | Check | Fix |
|---|---|---|
| Finished agents keep their seats and fill a hive's max agents | The controller log for `hub execution stop for <agent>` | No line while finished hub admitted agents are seated: the controller predates pull request #11464; roll it onto a release that carries it. |
| The controller logs `sent` for the agent after each dedupe window, and the agent stays seated | The hive's log for `hive execution stop` | No line at all: the hive is not reading its stops, because its service predates the stop reader or is not running; restart it as in [Upgrade](#upgrade). The hive logs nothing for a stop it honors, so a quiet log alone proves nothing. `could not reap it; retrying`: the hive's runtime cannot find or reap the session; check herdr on that hive. |
| The hive logs `hive execution stops for swarm <slug> failed (<error>)` | The error type and message it prints | Fix the cause the error names. A NATS permission error means the hive's bus credential lacks the stop grants: on the workstation root hive, restart the hive service: `hive run` compares the stored root credential's publish and subscribe grant with a fresh mint at start and re-mints it when they differ, logging `hive re-minted the root bus credential in <path>`; a `hive kept the stored root bus credential (<reason>)` line names why it could not, and `agentihooks hive root-bus` mints it by hand. On a member hive, run `agentihooks hive refresh`, then restart its hive service, which keeps the bus it opened at start. |
| The tick prints `could not send the stop for <agent>, retrying next tick: failed (<error>)` | The error, NATS from the controller and the controller's bus credentials | Fix the bus; the next pass sends the stop again. |

### Retiring a hub admitted agent

The tick also retires a hub admitted agent whose work has not ended: when it sits idle, its account is revoked,
it repeats content refusals, it stays stuck on a provider error or its live binding no longer matches its record.
The tick drops the agent, refunds its claim life, reopens its task, and sends the agent's hive the execution stop
naming the retirement reason. The hive then ends the execution as in
[How the hive honors the stop](#how-the-hive-honors-the-stop), with `stop_reason` set to that reason.

Before [pull request #11209](https://github.com/the-cloud-clockwork/agentihooks/pull/11209) the `verify` and `reap`
steps judged only the agents on the hub reconciler's legacy list, which leaves out every agent whose hive recorded
its `execution.started` on a real bus. Nothing on the hub or hive side retired such an agent for a mismatch or
idling, so it kept its seat and its claim life never came back. Pull request #11209 put hub admitted agents into
both steps with a refund.
[Pull request #11250](https://github.com/the-cloud-clockwork/agentihooks/pull/11250) made the `reap` step's
retirements send the hive stop,
[pull request #11293](https://github.com/the-cloud-clockwork/agentihooks/pull/11293) did the same for the `verify`
step's mismatch, and [pull request #11296](https://github.com/the-cloud-clockwork/agentihooks/pull/11296) resends a
stop the bus refused.

#### Which retirements send the stop

| Stop reason | Step and trigger | Tick action |
|---|---|---|
| `idle` | `reap`: the idle watch counted 20 idle passes (`IDLE_KILL_TICKS`, from `IDLE_KILL_MS`, nineteen one minute passes) | `stalled <agent>` |
| `revoked` | `reap`: the agent's harness account is revoked | `freed the seat of <agent>: <harness> account <account> was revoked` |
| `refused` | `reap`: the pane shows repeated content refusals while the task row names the agent | `freed <agent> after repeated content refusals` |
| `stuck` | `reap`: the pane shows a provider error and either the agent's outage recovery is exhausted, or the agent is idle and its pane has been held on the error for 30 minutes (`HOLD_LIMIT_MS`) | `freed the seat of <agent>, stuck on a provider error` |
| `mismatch` | `verify`: the agent's live binding differs from its record in the fields the action names | `retired <agent> after mismatched <fields>` |

The drop appends `, task <task> reopened` when it reopened the task, and `, task <task> left to the merge of <url>`
when the agent was waiting on its own pull request's merge. An `idle` action adds
`; outage recovery could not proceed: <blocker>` when an outage recovery of the agent was still pending.

The `reap` step watches a hub admitted agent only while the controller lists its pane as live and its work has not
ended. It skips an agent in `awaiting-decision` or `retiring`, a master still `starting` and an agent whose launch
acknowledgement is still being collected. The `verify` step judges a hub admitted agent only when the runtime
returned binding facts for it. Masters are never retired for idling or a stuck provider error. A master on a
revoked account is recycled in its seat instead, and a master's mismatch first passes the master retirement hold,
which can keep it seated.

#### Retire, drop, then stop

For `idle` and `mismatch` the tick reaps the agent first:

1. The tick asks its own runtime to retire the agent, which reaps the session with the task's scratch homes. A
   `mismatch` skips this when the hive's latest report already shows the agent gone.
2. When the runtime cannot reach the agent, as from the controller pod while the pane lives on the hive, the tick
   sends the execution stop to the agent's hive instead. A `sent` or `pending` answer counts as retired.
3. With neither, the agent stays seated. An `idle` agent is judged again on the next pass. A `mismatch` agent is
   marked `retiring`, with the action `could not retire <agent> after mismatched <fields>, retrying next tick`.
4. The tick drops the agent with the refund below.
5. Once the store took the drop, the tick sends the stop naming the reason. For an agent its runtime reaped this is
   the only stop. For an agent stopped in step 2 it is the same stop inside the 120 second bus dedupe window, so it
   answers `pending` and nothing new reaches the hive.

For `revoked`, `refused` and `stuck` the agent counts as retired when its hive's latest report shows it gone or the
tick's runtime retires it. Otherwise the agent stays seated, and the action reads
`could not retire <agent> on revoked <harness> account <account>, retrying next tick`,
`could not retire <agent> after repeated content refusals, retrying next tick` or
`could not retire <agent>, stuck on a provider error, retrying next tick`. A retired agent is dropped with the
refund, then the stop goes out as in step 5.

The stop goes out only after a drop the store took. When the store refuses the drop, no stop follows and the next
pass judges the agent again. The `reap` step then reports
`kept <agent>, the store refused its drop, retrying next pass`, and a `verify` mismatch action ends
`, the store refused its drop, retrying next pass`. An unreachable `idle` or `mismatch` agent was already stopped in
step 2, so it stays seated with its execution stopped when the store refuses its drop.

An agent with no recorded start, a legacy launch, gets no stop: no started event on the bus matches it, so the
stop answers `unknown`. On the memory bus the stop answers `unknown` without opening a bus.

#### The claim life refund

Every one of these drops refunds. The drop gives the task back one claim life when all of these hold:

- the task row is `claimed` or `pr`, names the agent as claimant and does not await merge;
- the agent declared no wait on the merge of the task's own pull request;
- the task's started lives count is above zero, and a fresh ledger read still names the agent at the claim
  generation the pass read;
- the task claim still names the agent at that generation, no refund is recorded for that generation and the
  task's claim authority is not held.

The refund lowers the task's count in `agentihooks:swarm:<slug>:started-lives`, or cancels the life still pending
for it, and records the generation in `agentihooks:swarm:<slug>:claim:<task>:refunded`, so a generation is refunded
at most once. The drop then releases the claim, removes the agent record, reopens the task guarded on the claimant
and claim generation it read and, when the task reopened, keeps the agent's seat for it. In `deny` mode the
`claims` gate blocks an open task once it has spent three lives (`CAP` in `scripts.gates.claims`), so without the
refund three such retirements would block the task.

When the hive later publishes the agent's exit, the hub's `lifecycle` reactor releases the launch slot and marks the
spoke claim `exited`, and refunds no second life.

#### What holds an idle agent

A hub admitted agent counts idle passes under the same watch as an agent the tick launched. None of these count
toward its retirement:

- the pane waits on an input prompt, which also resets the idle count;
- a declared wait (`agentihooks swarm <slug> wait`) or a working heartbeat; a checked wait holds only while the last
  waits pass ran within the idle retirement window;
- text in the pane's input line, or an operator prompt to the pane within the quiet window;
- a ledger probe hold, or an automated gc run that owns the agent;
- an outage hold whose recovery is not exhausted, or an outage recovery still pending.

A held agent keeps its seat and claim, and the tick sends its hive no stop.

#### The stop line and resent stops

Every stop prints one line on the tick's stderr, the controller log in a cluster install, with the answers listed
in [The stop and what the tick reports](#the-stop-and-what-the-tick-reports):

```text
hub execution stop for engineer@<code>-<n> in swarm <slug> after idle: sent
```

When the stop sent after the drop answers `failed (<error>)` and the agent has an execution id, the tick keeps it
in the hash `agentihooks:swarm:<slug>:pending-stops`, keyed by execution id, with the agent record and the reason.
At the start of every `reap` pass the tick sends each kept stop again and reports one action for each:

```text
sent the idle stop for engineer@<code>-<n> again: sent
```

Any answer other than `failed` removes the stop from the hash. The first `failed` answer ends that pass's resends
and leaves the rest for the next pass. A kept stop whose execution the swarm still seats is removed unsent, because
the next judgement of that agent sends a fresh one. On the memory bus nothing is kept or resent. A stop sent before
the drop in step 2 is never kept: a failed one leaves the agent seated for the next pass.

#### Retirement stop limitations

- `agentihooks swarm <slug> stop now` retires each agent through the runtime and refunds its claim life, but sends
  no execution stop to any hive.
- A hive that predates the execution stop reader ignores the stop, so an agent the tick's runtime could not reach
  keeps its session running on that hive after the swarm dropped it.

#### Upgrading for the retirement stop

The controller, or the workstation tick in local mode, needs a release carrying pull requests #11209, #11250,
#11293 and #11296. Each hive needs the execution stop reader, as in [Upgrade](#upgrade).

#### Checking a retirement on a live swarm

```bash
# Retirement stops the controller sent in the last hour
kubectl -n <namespace> logs deploy/agentihooks-swarm-controller --since=1h \
  | grep -E 'hub execution stop for .* after (idle|revoked|refused|stuck|mismatch):'
# hub execution stop for engineer@<code>-<n> in swarm <slug> after mismatch: sent

# Stops the bus refused, waiting for the next reap pass
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HGETALL 'agentihooks:swarm:<slug>:pending-stops'

# The task's started lives after the refund, and the generation refunded
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HGET 'agentihooks:swarm:<slug>:started-lives' <task>
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET 'agentihooks:swarm:<slug>:claim:<task>:refunded'

# The task reopened with no claimant
agentihooks ledger --slug <slug> get tasks/<task>
```

The feature map entries `events-retirement-refunds-claim-life`, `events-retirement-stops-the-execution`,
`events-refused-stop-resent` and `events-legacy-free-sends-no-stop` list the regression tests for this path, and
`events-smoke-mismatch-retire` and `events-smoke-idle-retire` the smoke tests on a real nats-server and Redis. The
Semgrep incident rule `tick-free-skips-execution-stop` fails a tick path that retires an agent through
`retire_unless_gone` and drops it with a refund without sending the hive stop.
[Pull request #11211](https://github.com/the-cloud-clockwork/agentihooks/pull/11211) added that rule and the legacy
no stop tests, and [pull request #12020](https://github.com/the-cloud-clockwork/agentihooks/pull/12020) added the
mismatch and idle smoke tests.

#### Troubleshooting retirements

| Symptom | Check | Fix |
|---|---|---|
| An idle hub admitted agent is never retired | `agentihooks swarm <slug> status`, the agent's declared wait and its pane | Expected while a hold in [What holds an idle agent](#what-holds-an-idle-agent) applies, or while the controller cannot list its pane. With none of those, the controller predates pull request #11209; roll it onto a release that carries it. |
| `could not retire <agent> after mismatched <fields>, retrying next tick` on every pass | The controller log for `hub execution stop for <agent>` | `unknown`: no started event matches the agent's current execution. `ended`: its execution already ended, and the hub reconciler settles it. `failed (<error>)`: fix the bus the error names. |
| The tick reports `kept <agent>, the store refused its drop, retrying next pass`, or a mismatch action ends with that text | The agent record and the swarm Redis | No stop went out unless step 2 already stopped the agent. The next pass judges the agent again. |
| `sent the <reason> stop for <agent> again: failed (<error>)` on every pass | NATS from the controller and the controller's bus credentials | Fix the bus; the stop stays in `pending-stops` until the bus takes it. |
| The controller logs `sent`, and the retired agent's session still runs on its hive | The hive's log for `hive execution stop` | As in [Troubleshooting ended agents](#troubleshooting-ended-agents). |
| A task blocks at the claim cap after its agents were retired for these reasons | `started-lives` and `claim:<task>:refunded` for the task | No refund was recorded: the controller predates pull request #11209, or the task row no longer named the agent when it was dropped. |

### Resuming a dead agent the hub started

When the pane of a hub started agent dies before its work ends, its hive reopens the agent's own conversation in the
same seat, under the same task claim. This is the dead resume the tick gives agents it launched itself (see
[Outage holds and recovery](swarm.md#outage-holds-and-recovery)). The tick's reap step sends a hub admitted agent's
hive an execution stop once the agent's work ends (see [Stopping an agent whose work
ended](#stopping-an-agent-whose-work-ended)), and until then watches the agent only while the controller lists its
pane as live. It never resumes a dead one, so on the `events` engine the hub's `lifecycle` reactor decides the resume
and sends the agent's hive a `resume` command order. When the resume cannot run, the agent is dropped and its task
reopens for a fresh agent.

Before [pull request #11246](https://github.com/the-cloud-clockwork/agentihooks/pull/11246) the reactor had no
resume. It dropped every dead hub started agent and reopened its task, so the next agent started without the dead
agent's conversation.

#### Which exits are resumed

An `execution.exited` for a dead pane reaches the reactor two ways:

- the hive publishes it when its execution report shows the agent's pane `exited` or `dead`;
- the hub's `reconciler` calls the reactor's handler directly on each five minute pass while the hive's latest report
  shows the pane ended, no longer lists the agent's recorded pane or, for a hub admitted agent past its startup
  window, no longer lists the agent at all.

Other exits carry a `reason`: the hub's `hive-loss` reactor publishes `hive-lost` and the reconciler `lost` for an
execution on a lost hive, and the hive publishes `stopped` after it honors an execution stop.

The reactor first matches the exit to the spoke claim and the execution record. It holds the exit while an outage
the tick shared is open or the tick's outage recovery of the agent is still pending (see
[Outage holds for agents the hub started](#outage-holds-for-agents-the-hub-started)). For an execution whose spoke
claim is still `started`, it then orders a resume only when every one of these holds:

| Check | When it fails |
|---|---|
| The agent's hive is known: the agent record's hive, else the host its runtime target names. | No resume. |
| The exit carries no `reason`. | No resume. |
| No deliberate exit is recorded for the agent. `agentihooks terminate-agent` records one; the reactor consumes it here. | No resume. |
| The execution has had fewer than three resumes (`RECOVERY_ATTEMPTS`), counted in `resumes` on its spoke claim. | No resume. |
| The swarm is not `stopped` or `stopping`. | No resume. |
| The task row's claim generation is the one the execution started under. | No resume. |
| The tick's dead resume gate holds: no handoff was submitted for the task, the agent recorded a conversation id, its conversation is under the swarm's compact limit and has not ended after each of its last three tick resumes, and the agent still holds its record, its seat as occupant, its task row in `claimed` or `pr` naming it, and its task claim. | No resume. |

The tick counts only its own resumes toward that gate; the hub's resumes count only in the spoke claim's
`resumes`. An exit that orders no resume takes the normal exit path: the reactor removes the agent record,
releases the launch slot and the claim, and returns a task the agent still holds to `open` with no claimant, as in
[How the hive honors the stop](#how-the-hive-honors-the-stop). The next offer goes to a fresh agent.

#### The resume order

The reactor writes the order into the spoke claim, with the agent's launch time and pane as it judged them and the
time of the exit, and raises the claim's `resumes` count. It then puts the order on the hive's command stream,
`agentihooks:hive:<hive>:commands` under the default keyspace root. The order id is
`resume:<execution>:<generation>:<attempt>`, so each attempt is a new order with its own receipt,
`agentihooks:hive:<hive>:command:<order id>`. The order carries the swarm, the agent, the execution, its generation,
the judged launch time and pane, and the prompt the tick's resume also types:

```text
Your pane ended unexpectedly. Continue your task in this conversation and seat. Read your task progress and proof and handle your inbox before continuing.
```

While the order is out the agent keeps its record, seat, task claim and task row, and the exit is held. Each later
reading of the exit decides from the order's receipt:

| What the reactor reads | Action |
|---|---|
| No answer, less than 30 minutes (`RESUME_DEADLINE_MS`) after the exit that ordered the resume | Holds the exit. |
| Answer `done` | Holds the exit. The agent runs in its new pane. |
| Answer `failed` | Posts a swarm notice and drops the agent; its task reopens. |
| No answer 30 minutes after the exit that ordered the resume | Posts a swarm notice naming `its hive never answered` and drops the agent; its task reopens. |
| An exit observed before the resumed pane's launch time | Holds it: the exit belongs to the old pane. |
| An exit of the resumed pane | Judges it as a new death and orders the next attempt. |

The notice appears in the ledger's notifications panel:

```text
Starting a fresh agent after engineer@<code>-<n> ended because resume failed: <failure>.
```

Each death gets one resume order, and the exit of a resumed pane is judged as a new death. After the third resume
of the same execution, the next death drops the agent without a notice and its task reopens.

#### How the hive runs the order

The hive reads its command stream on every heartbeat, every fifteen seconds, including while it drains. Every other
command order runs inside the heartbeat. A resume order runs in its own process,
`python -m scripts.hive.resume_order <hive> <order>`, so a slow herdr resume never delays the heartbeat:

1. The heartbeat sets the running marker `<receipt>:running` for 120 seconds, counts the start in the receipt's `runs`
   field and starts the process. While the marker lives, no second process starts.
2. The process renews the marker every 30 seconds while it works.
3. A marker that expires with no answer, as after a crashed process, lets a later heartbeat start another process.
   When a fourth start comes due, the heartbeat stores the answer `failed` with `resume never answered` instead.
4. The order stays on the command stream until its receipt holds an answer; the next heartbeat then deletes it.
5. The first answer stored is final: a late answer never overwrites it. An exception while the resume runs stores
   `failed` with `error: <exception type>`. A process that fails before it opens its Redis client stores no
   answer: its marker expires and a later heartbeat starts another process, as in step 3. A process that cannot be
   started raises out of the heartbeat and ends `hive run`; the service starts it again five seconds later, and the
   first heartbeat after the marker expires starts the order again.

The hive keeps every resume order process it starts, so a restart on a moved checkout waits for a resume in flight
(see [Adding a machine to a hive](#adding-a-machine-to-a-hive)).

Inside the process the hive:

1. Reads the execution record and checks that it names the agent, generation, launch time and pane the hub judged,
   that it runs on this hive (the host its runtime target names, else its record's hive), and that the agent still
   holds its task claim. Any difference answers `failed` with `identity`.
2. Reaps any process still running under the agent's name. A failed reap answers `failed` with the runtime's detail,
   such as `a process carrying <agent> still runs`.
3. Reopens the agent's conversation in its seat through the runtime's native resume, the same herdr resume the tick
   uses. The swarm's repository is passed as the origin the swarm recorded at its switch to `events`
   (`repo_origin`), when it has one, instead of the workstation path. A refused resume answers `failed` with the
   runtime's detail.
4. Checks the record and the claim again. When a successor claimed the task during the resume, it closes the new pane
   and answers `failed` with `identity`.
5. Records the new pane and launch time on the agent record and resets its idle count to zero. A failed write closes
   the new pane and answers `failed` with `error: <exception type>`.
6. Answers `done`.

The hub checks the ledger task row, its claim generation and the seat before it orders a resume. The hive checks the
execution record and the task claim before and after the resume. A successor's work is therefore never resumed by
the old agent: a successor's claim, or a newer launch that replaced the record, makes the hive answer `identity`.

#### Upgrading for the hub resume

The hub and every hive need a release carrying pull request #11246:

- A hub on an older release orders no resume; every dead hub started agent is dropped and its task reopens.
- A hive on an older release cannot parse a resume order and deletes it unanswered, so the hub waits the full 30
  minutes before it hands the task to a fresh agent. An order leaves out a launch time or pane that is unset, and
  other command orders never set them, so older hives still parse those.

A hive process loads its code when it starts: restart `agentihooks-hive` with `systemctl --user restart
agentihooks-hive` on a workstation or VM, or roll the hive pods on Kubernetes.

#### Checking a resume on a live swarm

```bash
# The resume orders a hive received, with their running markers
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" --scan --pattern 'agentihooks:hive:<hive>:command:resume:*'

# One order's receipt: sent, runs and, once answered, ack
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HGETALL 'agentihooks:hive:<hive>:command:resume:<execution>:<generation>:<attempt>'
# ack {"status": "done", "target": {"host_id": "<hive>"}, "failure": ""}

# The agent keeps its seat and task
agentihooks swarm <slug> status
```

A receipt expires a day after its last write. The feature map entry `hub-dead-resume` lists the regression tests for
this path.

#### Troubleshooting dead agent resumes

| Symptom | Check | Fix |
|---|---|---|
| A hub started agent died and its task reopened with no notice | The exit's reason, the task's handoff, `agentihooks swarm <slug> status` and the swarm state | Expected for a lost hive, an execution stop, a deliberate `terminate-agent`, a submitted handoff, a conversation at its compact limit, a third resume already spent or a stopped swarm. With none of those, the hub predates pull request #11246: roll it onto a release that carries it. |
| Notice `resume failed: its hive never answered` | The hive's release and its service | The hive predates pull request #11246, or its service is not running. Upgrade or restart it as in [Upgrading for the hub resume](#upgrading-for-the-hub-resume). |
| Notice `resume failed: resume never answered` | The hive's service log | Three resume processes started and none stored an answer. Read the hive log for the process's error. |
| Notice `resume failed: identity` | The task's claimant and the agent record | The record or claim changed between the hub's order and the hive's resume: a successor took the task or a newer launch replaced the record. Expected: the old agent is dropped, and the task stays with its successor or reopens for a fresh agent. |
| Notice `resume failed: a process carrying <agent> still runs` | herdr on that hive | The old session could not be reaped. Close it on the hive; the next death of a later agent resumes normally. |
| Notice `resume failed: <runtime detail>` or `resume failed: error: <exception type>` | The detail it names | Fix the cause it names: a missing conversation, the hive's checkout of the repository, or the error type in the hive log. |

### Outage holds for agents the hub started

On the `events` engine a hub started engineer leaves the swarm through the hub's `lifecycle` reactor, and its inbox
wakes come from the hub's `inbox-wake-expiry` reactor. The tick keeps its outage records in files under its own swarm
home, which the hub, running on another host, cannot read. So on every pass the tick also publishes those records to
Redis, and both reactors read that copy. While a Redis, ledger or provider outage is open, a hub started engineer
keeps its record, seat, task claim, launch slot and task, and gets no inbox wake. After recovery it gets the resume
prompt the tick gives its own agents (see [Outage holds and recovery](swarm.md#outage-holds-and-recovery)).

Before [pull request #11245](https://github.com/the-cloud-clockwork/agentihooks/pull/11245) the hub had no outage
check. An exit the hub read during an outage dropped the engineer, released its claim and slot and reopened its
task. The hub's inbox wake looked for outage records in a home the hub does not have, so it woke engineers during
outages and counted outage time toward each wake window.

A hub started engineer is one whose hive recorded its start on the bus. On any bus but the in-memory test bus, the
hub reconciler's `admitted_agents` lists it, and the tick's reap step judges it by the hub admitted rules in
[Stopping an agent whose work ended](#stopping-an-agent-whose-work-ended).

#### How the hub reads the tick's outages

The tick's `outage probes` step (group `wrapper probes`, every pass; see the
[tick step catalogue](../reference/tick-step-catalogue.md)) runs the provider probes, then publishes every outage
record of the swarm, open and closed, to one Redis string. The publish runs even when the probes raise.

```text
agentihooks:swarm:<slug>:open-outages
```

`agentihooks` is the default keyspace root; `AGENTIHOOKS_SWARM_KEY_PREFIX` moves it. The value is the swarm's
`outages.json` from the tick's swarm home, wrapped with the publish time in milliseconds:

```json
{
  "at": 1795035600000,
  "records": {
    "provider": {
      "kind": "provider",
      "started_at": 1795035000000,
      "ended_at": null,
      "evidence": ["engineer@<code>-<n> (codex, account <account>): stream disconnected before completion"]
    },
    "ledger": {
      "kind": "ledger",
      "started_at": 1795030000000,
      "ended_at": 1795030120000,
      "evidence": ["Ledger health probe failed"]
    }
  }
}
```

A record whose `ended_at` is `null` is open. Every kind the tick records counts: `redis`, `ledger`, `hive-ledger`
and `provider`. A record's `past` list holds the earlier closed spans of its kind, which the inbox wake uses, and a
provider record also carries its affected agents and probe state.

The share follows these rules:

- It never expires. A tick that stops publishing, because it crashed or its host is down, leaves the last published
  state standing, open outages included, and the hub keeps holding on it.
- A share older than ten minutes (`SHARED_STALE_MS`, 600000 ms) that still holds an open record still holds, and
  every hub read of it prints one line to the hub's log. A share exactly ten minutes old is not logged, and a stale
  share with no open record is read without a line.

  ```text
  outage share of <slug> is stale since <publish time in ms>, still holding provider, ledger
  ```

- When the tick finishes a stopped swarm, the same step that closes it deletes the share.
- The hub sees each record as the tick last published it, at most one pass behind the tick's own view.
- A Redis outage reaches the share only after Redis answers again. While Redis is down, every tick pass ends at its
  Redis check and publishes nothing, and the hub cannot drop an agent without Redis either. The first pass that finds
  Redis back publishes the `redis` record still open; it closes at the end of that pass, and the next pass publishes
  it closed. An engineer that stalled across the outage stays held through its pending outage resume, described in
  [After the outage](#after-the-outage).

#### Which exits the hub holds

The `lifecycle` reactor reads an exit from a hive's `execution.exited` or from its own reconciler pass (see
[Which exits are resumed](#which-exits-are-resumed)). It first matches the exit to the spoke claim and the
execution record. It then checks the outage hold before it judges a resume, in this order; the first check that
decides ends the judgement:

| Order | Check | Result |
|---|---|---|
| 1 | The exit already returned the engineer's slot and claim (`returned` on the spoke claim) | Not held: the exit finishes settling. |
| 2 | The tick has an outage resume pending for the engineer: state `pending` in `outage-resumes`, fewer than three attempts, the same task, seat, execution, generation and claim generation, and no deliberate exit | Held, even after every shared record closed. |
| 3 | The engineer's record is `finished`, as after a handoff | Not held. |
| 4 | No shared record is open | Not held. |
| 5 | The ledger task row is done: `done` true or state `done` | Not held. |
| 6 | A deliberate exit is recorded for the engineer, as `agentihooks terminate-agent` records | Not held. |
| 7 | None of the above | Held. |

A held exit changes nothing. The engineer keeps its record, seat, task claim and launch slot, its task row stays as
it was, no life is refunded and no resume is ordered. The hub reconciler reads the exit again on each five minute
pass while the hive's report still shows the pane ended or the engineer gone. Once nothing holds it, the exit goes
on as in [Which exits are resumed](#which-exits-are-resumed): a hub resume when every resume check passes, else the
engineer is dropped, its slot and claim return and its task reopens.

#### Inbox wakes during an outage

The hub's `inbox-wake-expiry` reactor reads the same share each time a wake deadline expires:

- While any shared record is open it sends no wake and sets the next deadline one retry window later
  (`AGENTIHOOKS_INBOX_RETRY_WINDOW_S`, 300 seconds by default).
- Once every record is closed, the next deadline starts from the later of the receiver's seat clock and the item's
  last recorded event, and adds the retry window plus the outage time since that start. Overlapping records count
  once. An outage therefore never uses up an item's
  window or one of its three wakes.

#### After the outage

The tick recovers hub started engineers the way it recovers its own:

1. The tick's `track outages` step records an outage resume, state `pending`, for each engineer that owns its task
   and stalled during the outage. Owning means: the engineer is `working`, its task row is `claimed` or `pr` and
   names it, and it holds the task claim, its seat and a live claim lease. Stalled means its pane is gone, shows a
   provider error or is idle. The record lives in the Redis hash `agentihooks:swarm:<slug>:outage-resumes`, one
   field per engineer.
2. Once every outage record in the tick's home is closed, the `recover outages` step sends each pending engineer
   this prompt, through a nudge to a live pane, or by reopening the engineer's own conversation in its seat when its
   process died:

   ```text
   Service is back after the outage. As <engineer>, re-read your task folder <folder> (steering.md, progress.md, proof.md), then run agentihooks msg inbox and continue your own task <task>. Keep your existing claim; do not claim new work.
   ```

   A busy pane, typed input or a permission prompt makes it wait, and the record's `blocked` field names why. A dead
   process the runtime cannot resume restarts from an outage handoff. After three attempts the resume is
   `exhausted`.
3. While the resume is `pending`, the hub holds the engineer's exit (check 2). Once it is `done` or `exhausted`, the
   next reading of the exit settles it.

#### Limits of the hub outage hold

- The hold has no time limit: an exit stays held as long as a shared record stays open or a resume stays pending.
- The share is only as fresh as the tick. With the tick down, the last published state stands, and an outage that
  opens after the tick stopped never reaches the hub.
- A stale open record holds until a tick publishes again or the stop of the swarm deletes the share.
- The stale age compares the hub's clock with the tick's publish time, so clock skew between the two hosts moves
  the point where the stale line starts.
- The hold covers an exit only until its slot returned. An exit already past that point finishes settling during
  an outage.
- An outage never holds a finished engineer, a done task or a deliberate exit.

#### Upgrading for the hub outage hold

Both sides need a release carrying pull request #11245:

- The tick, which is the controller, or the workstation tick in local mode, publishes the share. With an older tick
  no share exists and the hub holds nothing.
- The hub reads the share. An older hub holds no exit during an outage, and its inbox wake reads only its own empty
  outage home.

The tick publishes on its first pass with the new code. A hub process loads its code when it starts, so roll the
hub deployment onto the release.

#### Checking the outage hold on a live swarm

```bash
# The share the hub reads: publish time and every record
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET 'agentihooks:swarm:<slug>:open-outages'

# The tick's own open outages, and the held engineer still in its seat
agentihooks swarm <slug> status

# Outage resumes the hub waits for
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HGET 'agentihooks:swarm:<slug>:outage-resumes' 'engineer@<code>-<n>'
# {"identity": ["<task>", "<seat>", "<execution>", 1, 7], "episodes": [["provider", 1795035000000]], "attempts": 0, "state": "pending", "last_at": 0}

# Stale shares the hub read
kubectl -n <namespace> logs deployment/agentihooks-swarm-hub | grep 'outage share of'
```

The feature map entry `events-outage-hold-hub-agents` lists the regression tests for this path.

#### Troubleshooting the hub outage hold

| Symptom | Check | Fix |
|---|---|---|
| A hub started engineer was dropped and its task reopened during an outage | The share: does the key exist, is `at` recent, was a record open? The engineer's record and task row at the time | No key: the tick predates pull request #11245 or has run no pass since; upgrade it. Every record closed: the tick never recorded the outage, so only its recorded kinds hold. A finished engineer, a done task or a deliberate exit: expected. With an open record and none of those, the hub predates pull request #11245; roll it onto a release that carries it. |
| The hub logs `outage share of <slug> is stale since <ms>, still holding <kinds>` | Whether the tick still runs passes for the swarm | The tick stopped publishing, and the hub keeps holding on its last state. Bring the tick back; its next pass publishes again. A swarm that is stopping deletes the share when its stop completes. |
| Engineers stay seated long after the outage ended | The engineer's field in `outage-resumes` | `pending` below three attempts: `recover outages` has not run yet or waits, and `blocked` names why, such as `its pane holds input`. Clear what it names. After three attempts the resume is `exhausted` and the next reconciler pass settles the exit. |
| No inbox wakes reach hub started engineers | The share for an open record | Expected while an outage is open. Wakes start again one retry window after it closes. A stale open record: as for the stale line above. |

### Running a spoke

With `AGENTIHOOKS_DEPLOYMENT=distributed`, `agentihooks swarm spoke` runs the master lane spoke on the workstation,
and `agentihooks hive run`, the worker hive image's last start step, runs beside its telemetry loop an eng, ci and
plan spoke for every swarm on the `events` engine, leaving out the lanes in `HIVE_EXCLUDED`. The workstation spoke
runs the same lane spokes for every lane. Legacy engine swarms get no lane spoke. Each new spoke logs
`hive spoke for swarm <slug> lane <lane> takes swarm.<slug>.ready.<lane>`, and a stopped set of lane spokes logs
why and starts again after 30 seconds. Spokes take work from the bus and bind to the hub's streams instead of
creating them. Outside distributed mode `agentihooks swarm spoke` refuses, because the workstation then runs the
swarm engine itself, but `agentihooks hive run` still starts the eng, ci and plan spokes: under the `events` engine
the tick launches no workstation task and offers each one pinned to the root hive, so the root hive's lane spokes
are its only taker, and only a spoke poll beats the hive's presence. `hive run` loads the operator env files before
the spokes read their bus settings, and a draining hive polls no spoke but keeps beating presence on every `events`
swarm, so the hub never judges its running agents lost. Only a memory bus refuses the lane spokes, on every hive.
The bus settings a member hive needs arrive with its
invite; see [The hub and the bus](../reference/deployment-modes.md#the-hub-and-the-bus). The workstation root hive
has no invite and gets them from `agentihooks hive root-bus`; see
[Root hive bus settings](../reference/deployment-modes.md#root-hive-bus-settings).

## Workstation fallback

When no UI hive fits, because none is live, or the live ones lack the repo, or lack both a slot and the interactive
login for the master's harness (a slot alone for a [backend seat](#backend-master-seats)), the hive controller
leaves the master to the workstation herdr server. It does the same when the hive runtime is disabled on the
controller. When the controller's own herdr server answers, the controller spawns the master there. Otherwise it
refuses the launch with "the workstation hive spawns master work" followed by the placement reason, and the tick
reports that line in its actions.

The refusal carries the master placement wait reason after the colon:

| Reason after the colon | Cause | Fix |
|---|---|---|
| `no live hive takes master` | No hive that takes `master` is live: the UI hive's heartbeat is over ninety seconds old, it is draining, or it excludes `master`, and every headless hive excludes it too. | Start or restart `agentihooks hive run` on the UI hive, end its drain, or remove `master` from its `excluded` list. |
| `no hive for master lists the repo <repo>` | Hives take `master`, but none lists the swarm's repo, under any checkout path or the same remote origin. | Clone the repo on the UI hive; its next heartbeat lists it. See [Matching a repo across machines](#matching-a-repo-across-machines). |
| `no hive for master advertises a claude slot` | A hive takes `master` and lists the repo, but holds neither the native Claude interactive login nor a Claude subscription or api slot whose account has a free seat and is not closed. For a backend seat (`master-2@<id>` and up) the native login does not count, so only such a slot fits. | Log the UI hive in with `claude` (front door only), or give it a `AH_CC_TOKEN_<slug>` with quota left; `agentihooks hive show <hive>` lists `interactive` and `slots`. |

For example, a swarm on the workstation `desk` and one Anton pod, where the operator cloned the repo only on the
pod:

```bash
agentihooks hive set desk ui=yes excluded=
agentihooks hive show anton-1      # "ui": "no", "excluded": ["master"]
```

The master launch skips `anton-1`, which excludes `master`, and finds no repo on `desk`. With the controller's own
herdr server answering, the master starts there. Without it, the tick's actions read, with the swarm's `repo`
setting in place of `agentihooks`:

```text
the workstation hive spawns master work: no hive for master lists the repo agentihooks
```

A move order follows the same rule: while the pod excludes `master`,
`agentihooks swarm <id> master move --to anton-1` refuses with
`the master cannot move to hive anton-1: no live hive takes master`.

The regression test `test_a_master_with_no_fitting_ui_hive_is_left_to_the_workstation_when_headless_hives_exclude_it`
holds this rule: a UI hive that lacks the repo or a slot beside a headless hive that excludes `master` leaves the
master waiting on the UI hive's reason, and the controller refuses it with the workstation line, never placing it on
the headless hive.

The `master-headless` health finding marks a master record placed on a headless hive before this rule. The
`master-fallback-auth` finding marks a master that wanted its interactive login and runs on another route, so phone
access through Remote Control is lost.

Placement runs at every master launch, so the successor after the next handoff goes back to the best UI hive that
is live then. `agentihooks swarm <id> master move --to HIVE` forces that move without waiting: the master hands
off and its successor starts on the named hive.

## Prompt and workspace transport

A hive launch runs the agent on another machine, which has no copy of the ledger and no task work folder. The
controller therefore builds everything the agent reads at start and sends it in the launch payload; the hive sends
the agent's work folder lines back every heartbeat.

### What the launch payload carries

When placement picks a hive, the controller writes the payload to
`<root>:swarm:<slug>:launch-prompt:<execution id>` (`<root>` is `AGENTIHOOKS_SWARM_KEY_PREFIX`, default
`agentihooks`). The key expires with the 300 second launch slot reservation. Next to the task fields and the
profile decision it holds:

| Field | Content |
|---|---|
| `hive_opening` | The full opening prompt, built on the controller where the ledger is, for the Claude harness. |
| `hive_opening_digest` | The sha256 hex digest of `hive_opening`. |
| `hive_seeds` | The task's work folder files: `steering.md` whole, and the last 131,072 bytes of JSON of `progress.md`, `proof.md` and `recon.md`. Empty when the task has no work folder. |

The opening names the work folder as `~/.agentihooks/swarm/<slug>/tasks/<task id>`, so the same text is correct
below whichever home the hive runs the agent in.

### What the hive does with it

An order the hive picks up after its 300 seconds is acknowledged as failed with the reason `expired` before the
payload is read. Otherwise the hive checks the payload and acknowledges the order as failed with the reason `prompt`
when:

- the payload key is missing;
- the payload is not JSON, or its task id differs from the order's;
- `hive_opening` is missing or not text, or `hive_opening_digest` does not match it.

The controller ends the execution, drops the launch order and reports the spawn refused with
`hive launch failed: <reason>`.

A payload that passes is written below the hive's own home, in `~/.agentihooks/swarm/<slug>/tasks/<task id>`:

- `steering.md` is replaced with the controller's copy on every launch.
- `progress.md`, `proof.md` and every file the controller's folder carries besides them are written when the hive
  does not hold them yet, and replace the hive's file when it does not already contain the text that was sent. A hive
  file that holds the sent text and more, such as a `progress.md` the agent kept appending to on a relaunch of the same
  task on the same hive, stays as it is. A hive file that is a symbolic link is never written.

The agent then starts with `hive_opening` exactly as carried. The hive does not rebuild the prompt from the task.

### Work folder lines on the ledger page

On every fifteen second heartbeat, each hive writes the work folder lines of the executions it hosts to
`<root>:hive:<hive id>:workspaces`, expiring after 45 seconds. Each entry holds `latest_progress` and
`latest_proof`: the last three non-blank lines of `progress.md` and `proof.md`, read from the last 4,096 bytes of
each. A key is left out when its file has no such line. The copy covers every swarm task whose latest execution,
the live one or else the one started last, is a worker on that hive. Masters are left out.

When the controller publishes swarm status, each task with a work folder takes its lines from the hive that runs,
or last ran, its latest execution. A task whose latest execution ran on the controller's own machine is read from
the local folder. These lines are what the ledger page shows in the task's Contract and proof fold.

### Work folder files on any host

On the same heartbeat each hive writes the files of those folders to `<root>:hive:<hive id>:workspaces:files`,
expiring after a day so a hive that died still hands its last copy over. The table below lists what travels. On each
status publish the controller writes each task's copy, from the hive running its latest execution, into its own
folder. A launch carries the controller's folder to the next hive, added files included, and a file the hive already
holds is replaced unless it already contains what was sent, so a successor on any host reads what its predecessor
wrote.

What travels from a task folder:

| File | Travels | Limit |
|---|---|---|
| `steering.md` | Only from the controller to the hive, at launch | Whole file |
| `progress.md`, `proof.md`, `recon.md` | Both ways | The last 131,072 bytes of their JSON text each, which holds a recon plan of 500 lines of about 260 ASCII characters whole |
| Any other file directly in the folder, such as `plan.md`, `measure.sh`, `handoff.md` or `mutation-fix.patch` | Both ways | 32 files, taken in name order; a file over 65,536 bytes is skipped and does not count toward the 32 |
| Subfolders and the files in them, symbolic links, names that do not start with an ASCII letter or digit, such as hidden names starting with `.`, names whose later characters are not Unicode word characters, `.` or `-`, names over 128 characters, files that are not UTF-8 text | Never | Not carried |

The controller mirrors a task only when its latest execution ran on a hive. A task whose latest execution ran on the
controller's own machine keeps the controller's folder, and a stale hive copy is not written over it. The mirror
replaces each file the hive sent with the hive's text and leaves every other controller file in place; a file the
agent deleted on the hive stays on the controller. Steering in a hive copy is never written back.

The sequence for a task reclaimed from one hive onto another:

1. The first agent, on hive `lab-vm`, appends to `progress.md` and writes `measure.sh` in its task folder.
2. Within fifteen seconds `lab-vm` writes both files to `agentihooks:hive:lab-vm:workspaces:files`.
3. On its next status publish the controller writes them into `~/.agentihooks/swarm/<slug>/tasks/<task id>` on its
   own machine, even when `lab-vm` has stopped beating since, as long as its copy is less than a day old.
4. The successor launches on hive `lab-vm-2`. Its launch payload carries `steering.md`, `progress.md`, `proof.md` and
   `measure.sh` from the controller's folder, and `lab-vm-2` writes them below its own home before the agent starts.

Read the copy a hive holds for one task, and how long it has left, on the swarm Redis:

```bash
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET agentihooks:hive:lab-vm:workspaces:files \
  | jq -r '."rig-grade-swarm"."t945" | keys[]'
# measure.sh
# progress.md
# proof.md
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" TTL agentihooks:hive:lab-vm:workspaces:files
# up to 86400
```

A task missing from that output has no execution on that hive as its latest one, or its folder holds none of the files
that travel. Nothing is configured: the 32 file cap, the 65,536 limit and the one day lifetime are fixed in the code.

### Resume and recovery prompts

The restore prompt after a reboot and the resume prompt after an outage tell the agent to re-read its task folder.
For an agent on a remote hive both prompts name `~/.agentihooks/swarm/<slug>/tasks/<task id>`, the folder below the
hive's home, instead of the controller's absolute path.

### Upgrading

A hive refuses a launch payload without `hive_opening` and its digest, so an older controller cannot launch on a
newer hive. Deploy the controller first, then the hives.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Launches on one hive end with `hive launch failed: prompt` | The controller predates prompt transport and sends no opening and digest. | Upgrade the controller, then the hive. |
| Launches on one hive end with `hive launch failed: expired` | The hive picked up the launch order more than 300 seconds after the controller sent it. | Check that the hive beats (`agentihooks hive list` shows `live`) and its `agentihooks-hive` service runs. |
| A remote task's Contract and proof fold is empty | The hive stopped beating for more than 45 seconds, so its synced copy expired, or the agent has not written a line yet. | Check `agentihooks hive list`. The lines come back on the hive's next heartbeat. |
| A successor on another host does not see a file its predecessor wrote | The file does not travel: it sits in a subfolder, is a symbolic link, has a hidden or unsupported name, is not UTF-8 text, is over 65,536 bytes and is not `recon.md`, or comes after the first 32 files by name. | Move what the successor needs directly into the task folder as a plain text file under the limit. See [Work folder files on any host](#work-folder-files-on-any-host). |
| A successor on another host does not see a file its predecessor wrote, and the file is one that travels | The predecessor's hive wrote no copy: it ran a version from before pull request [#7834](https://github.com/the-cloud-clockwork/agentihooks/pull/7834), or its copy expired a day after its last beat. Or the controller runs a version from before #7834 and never writes hive copies into its folders. | Read the key with `redis-cli GET <root>:hive:<id>:workspaces:files`. Upgrade the controller and the hives. A copy that expired is gone; recover the file from the predecessor hive's own folder while that host is up. |
| Lines a relaunched agent wrote on its hive are replaced by the controller's copy | The hive's file did not contain the text the controller sent, so the controller's copy won. This happens when the hive file was edited rather than appended to. | Append to `progress.md` and `proof.md` instead of rewriting them. |
| A file deleted on the hive is still in the controller's folder | The mirror writes the files a hive sends and removes none. | Delete the file in the controller's folder too, or leave it: the next launch carries it again. |

## Redis restarts

`agentihooks hive run` keeps running when the swarm Redis restarts, rolls out or drops the hive's connection. The
daemon reconnects with a bounded backoff and resumes its heartbeat, registration and launch watch in the same
process, so a Redis rollout no longer leaves a joined hive down until someone restarts its service or pod.

### What counts as a lost connection

The beat loop treats two redis-py errors as a lost connection:

- `ConnectionError`, which includes `AuthenticationError`;
- `TimeoutError`.

Either one counts wherever the loop meets it: while connecting, during a beat (heartbeat, registry record, launch
orders, reports) or during the scheduler process timer wait between beats. Any other error, such as a `NOPERM`
answer to a member credential or a `ResponseError`, ends `hive run` with a traceback as before. The
`agentihooks-hive` user service then restarts it after 5 seconds (`Restart=always`), and a worker pod restarts its
container.

### Reconnect and backoff

On a lost connection the daemon:

1. Stops the connection's helper threads, the headroom publisher and the herdr stats publisher.
2. Prints `hive Redis connection lost (<error>: <detail>); reconnecting in <n>s` to stderr.
3. Sleeps the delay for that attempt.
4. Opens a new pair of clients through the same factory the service started with, then beats at once.

The delays run 1, 2, 4, 8, 16 and 30 seconds, then stay at 30 seconds for as long as Redis stays down. The sequence
starts again at 1 second only after a reconnected loop completes a full beat and the wait that follows it, so a Redis
that accepts a connection and drops it during the beat keeps the longer delay.

Each reconnect goes through the same Redis connection code as the first start. When the member's saved Redis
password is refused, it first tries the password saved in `hive.env` by another process on the host, then refreshes
the member through the hive endpoint, one refresh per host at a time. A refused refresh surfaces as an
`AuthenticationError`, so the daemon backs off and tries again instead of exiting.

### What resumes

After a reconnect the first beat runs at once on the new connection:

- the heartbeat and the hive's registry record, so the hive reads `live` again in `hive list`;
- the order consumer marker and the launch watch, so a launch order queued while Redis was down, or after it came
  back, is consumed on that beat;
- the quota, sessions and execution reports, and the headroom reading;
- both helper threads, started fresh on the new connection.

The process keeps its quota refresh worker, GitHub App token renewal and bundle refresh clocks across the
reconnect. Lane spokes started by `hive run` talk to NATS, not Redis, and are not restarted by it.

While the daemon backs off, the hive writes nothing to Redis. Its reports expire after 45 seconds and it turns stale
after the 90 second live window, so placement stops sending it new launches until the first beat after the
reconnect. What happens to agents already running there follows the hive's lost and fencing rules; see
[When a hive is lost](#when-a-hive-is-lost) and [Fencing](#fencing).

### Client timeouts

The two clients the daemon opens disable redis-py's own retry, so a lost connection reaches the loop at once
instead of being retried inside one command:

| Client | Connect timeout | Read timeout | Used for |
|---|---|---|---|
| Command client | 3 seconds | 5 seconds | Beats, reports and the helper threads' writes. |
| Wait client | 3 seconds | 15 seconds | The scheduler process timer waits between beats and in both helper threads. |

Both send TCP keepalives. A connection with no reply for more than 15 seconds gets a `PING` health check before its
next command.

### A dead connection never stalls the beat

A Redis connection can die without closing: the TCP socket stays established, the peer stops answering and the
bytes the hive sent sit unacknowledged in the socket's send queue. Issue
[#5472](https://github.com/the-cloud-clockwork/agentihooks/issues/5472) records the case: on 21 October 2026, after a
workstation reboot, the local hive service published no beat for 13 minutes while its single beat thread waited on
such a socket with 153 bytes in the send queue. Before pull request
[#5475](https://github.com/the-cloud-clockwork/agentihooks/pull/5475) the wait between beats used a client with
no read timeout, so that wait never ended. The service stayed active and logged nothing, every hive read stale and the
tick spawned nothing until the service was restarted.

With the timeouts in [Client timeouts](#client-timeouts), every Redis call the daemon makes ends in bounded time:

- A beat command that gets no answer raises `TimeoutError` when the command client's read timeout expires.
- The wait between beats raises `TimeoutError` when the wait client's read timeout expires, one beat interval after
  the wait's last reply.
- A connection that died while idle fails on its health check `PING` instead of on a later read.

The beat loop treats `TimeoutError` like `ConnectionError`: a lost connection. It prints the reconnect line, backs off
1 second on the first failure, opens a new pair of clients and beats at once, as in
[Reconnect and backoff](#reconnect-and-backoff). A stall during a beat therefore ends in a reconnected beat about 6
seconds later. A stall during the wait raises within one beat interval of the last beat, and the reconnected beat
follows after the backoff, 15 to 16 seconds after the last beat. The helper threads do not reconnect: the headroom
thread logs `hive headroom wait failed` and waits one headroom interval, and the herdr stats thread logs
`hive herdr stats wait failed` and exits until the next beat starts it again on the new connection.

The tests for this, in `tests/hive/test_daemon.py`, point the daemon at a listening socket that never accepts or
answers, once during a beat and once during the wait, and assert that the reconnect line is logged and the next beat
lands within one beat interval, plus a quarter second margin for the wait stall. They run with scaled constants (a 0.5
second beat) and do not count the backoff sleep.

The scheduled waits stay shorter than the read timeout they run under, so a healthy wake always arrives before the
socket times out and a read timeout always means a dead connection:

| Wait | Scheduled delay | Wait timeout | Read timeout it runs under |
|---|---|---|---|
| Between beats | 15 seconds minus the larger of the beat's run time and 2 seconds, never below 0 | At most 14 seconds | 15 seconds |
| Headroom thread | 10 seconds | 12 seconds | 15 seconds |
| Herdr stats thread | 10 seconds | 12 seconds | 15 seconds |

These values are constants in `scripts.hive.daemon`, not settings. The wait read timeout and the health check
interval equal `BEAT_SECONDS`; the 3 second connect timeout (`CONNECT_TIMEOUT_S`), the 5 second command read timeout
(`READ_TIMEOUT_S`) and the 2 second slack (`WAKE_SLACK_S`) are fixed values.

To see whether a running hive sits on a dead connection, list its sockets to the Redis port with their send queues,
replacing `6379` with the port in the hive's Redis URL:

```bash
ss -tnp | grep -E "python|agentihooks" | grep ":6379"
```

An established connection whose `Send-Q` stays above 0 across several samples a few seconds apart is not being
answered. On current code the daemon raises on it within 15 seconds, reconnects after the backoff and logs the
reconnect line to the journal.

### Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `hive list` shows a joined hive stale after a Redis rollout | `journalctl --user -u agentihooks-hive`, or the pod log, for `hive Redis connection lost` | Repeated lines with a 30 second delay mean Redis or the network is still unreachable from the hive; fix that and the hive returns on its next beat. No lines and an exited service mean the hive runs code from before [#5168](https://github.com/the-cloud-clockwork/agentihooks/pull/5168); upgrade it and restart the service or roll its pod. |
| Every reconnect logs `AuthenticationError` with `the hive refused a refresh` | Whether the hive endpoint is reachable and the member is still enrolled | Rejoin the hive with a new invite when the member was revoked; see [Adding a machine to a hive](#adding-a-machine-to-a-hive). |
| `hive run` exits with a traceback right after Redis returns | The error in the traceback | The error is not a lost connection, so the daemon does not retry it; fix the error it names, such as a Redis user missing a permission. The service restarts the process after 5 seconds. |
| A hive reads stale while its service is active, the journal shows no `hive Redis connection lost` line, and `ss -tnp` shows its Redis connection established with a `Send-Q` above 0 | The hive's agentihooks version | The hive runs code from before [#5475](https://github.com/the-cloud-clockwork/agentihooks/pull/5475), whose wait between beats has no read timeout. Restart the service to recover at once, then upgrade the hive. See [A dead connection never stalls the beat](#a-dead-connection-never-stalls-the-beat). |
| The journal shows `hive Redis connection lost (TimeoutError: ...)` every few beats and the hive flaps between live and stale | Redis latency from the hive, for example `redis-cli --latency` against the same address | Redis or the path to it accepts connections but does not answer within the 5 second command or 15 second wait read timeout. Fix the Redis load or the network path; the daemon reconnects on its own each time. |

## Failover timing

| Clock | Value | Effect |
|---|---|---|
| Hive heartbeat | every 15 seconds | Quota and the other reports expire after 45 seconds without a beat. The sessions report lives as long as the hive reads live; see [Sessions report and the account cap](#sessions-report-and-the-account-cap). |
| Headroom | every 10 seconds and every beat | A reading older than 30 seconds counts as missing and the hive's room reads 0; see [Headroom](#headroom). |
| Live window | 90 seconds | A hive past it is stale and gets no new launches. |
| Redis read timeouts | 5 seconds per command, 15 seconds per wait | A Redis connection that stops answering raises within one beat interval and reconnects after the backoff; see [A dead connection never stalls the beat](#a-dead-connection-never-stalls-the-beat). |
| Redis reconnect backoff | 1, 2, 4, 8, 16, then 30 seconds | A hive that lost its Redis connection retries at these delays and beats at once when it reconnects; see [Redis restarts](#redis-restarts). |
| Minute tick | 60 seconds (`AGENTIHOOKS_CONTROLLER_TICK_SECONDS`) | Each tick checks the master's hive. |
| `master-move-grace` | 300 seconds | How long the master's hive may go without a heartbeat before the tick moves the master. |
| Controller lease | three ticks, 180 seconds by default | Another controller takes over at a higher epoch once it lapses. |
| Worker execution lease | 240 seconds | A Swarm v2 Pod worker keeps its task through a controller handover; see [worker execution lease through a controller rollout](swarm.md#worker-execution-lease-through-a-controller-rollout). |
| Launch slot reservation | 300 seconds | A started launch order not acknowledged by then is recorded as ambiguous. |
| Launch slot refresh | two ticks, 120 seconds by default | Each tick keeps the live slot rows a live hive reports; see [Launch slot rows](#launch-slot-rows). |

The master moves when its hive's heartbeat is older than the grace and the runtime does not see the master live,
so a lost master hive is replaced between five and six minutes after its last beat at the defaults. A hive that
never beat, or whose record is gone, never moves its master. Set the grace with
`agentihooks swarm <id> set master-move-grace=SECONDS`.

The steps of the move are in [Automatic failover](#automatic-failover).

## Master failover and master move

The master seat changes hive in two ways: the minute tick moves it off a hive that went quiet, and the operator
moves it to a hive he names. In both, the successor starts through hive launch in the same master seat. The
operator move pins it to the named hive; the automatic move pins it when a live hive qualifies.

### Automatic failover

Each tick checks the live master record. It moves the master only when all of these hold:

- The master's hive has beaten at least once and its record still exists.
- Its last heartbeat is older than `master-move-grace` seconds.
- The runtime does not see the master's session live.

A stopped swarm is checked only when an operator message is waiting for its master.

Then, in one tick:

1. It stores a recovery handoff for the master seat. The handoff says the master should continue on the swarm,
   names the hive and how many seconds ago it stopped beating, and marks Done and Stopped at as `unknown`, because
   the old master left no document. Its Read first list names the master inbox.
2. It records the transfer with the reason `hive-lost`.
3. It bumps the master seat generation and fences the old execution on the quiet hive.
4. It asks master placement for a target among the live hives and records the pending move with that hive.
5. It marks the old master record finished and logs `moved master <name> off lost hive <hive>` in the tick output.

The next master start reads the recovery handoff, pins the successor to the recorded hive and clears the pending
move once the successor is placed. When no live hive qualifies, the move records no target and the successor
starts unpinned on the normal master path.

Tune the grace per swarm:

```bash
agentihooks swarm rig-grade-swarm set master-move-grace=600
```

A hive that pauses longer than the grace, such as a laptop asleep, loses its master; a shorter grace replaces a
lost master sooner. The default is 300 seconds.

#### Master move code preload

The `master move fail over` step runs in the tick's `observe` group with a waiting budget floor of two seconds
(`budgets.FLOORS` in `scripts.tick.steps.budgets`). The move reads the live hives through the hive launch code, so
it needs three modules: `scripts.hive.launch`, `scripts.hive.daemon` and `scripts.swarm.local_slots`. The tick
names them in `MASTER_MOVE_MODULES` in `scripts.swarm.tick`.

The tick loads those modules at the start of every pass, in the `start` site check of `TickSites`
(`scripts.tick.sites`), before any step runs and outside every step budget. Once a tick process has imported them,
later passes find them already loaded and the check costs nothing.

Before this preload, the first pass after a tick process started imported the modules inside the fail over step.
Its budget was then about one second, measured from warm passes. On a contended runner the import outlasted it.
The engine abandoned the step as `late`
(see [When a step exceeds its budget](../reference/tick-engine-steps.md#when-a-step-exceeds-its-budget)) and the
move was dropped for that pass. The swarm master recovery test that moves a master off a lost hive to the hive
with room failed on its first run for this reason and passed only on a retry.

Nothing is configured for the preload. A change that makes the move use another module that the tick does not
already import adds it to `MASTER_MOVE_MODULES` in the same pull request. Its guard is the test
`test_the_tick_loads_the_hive_launch_code_before_the_fail_over_step_starts` in
`tests/swarm/test_master_recovery.py`, which removes the modules, runs one tick on a lost master hive and asserts
every module is loaded when `master move fail over` starts. The tests in that file also raise every tick step's budget
floor to at least 30 seconds, so a slow runner cannot abandon a step mid test.

Check whether the fail over step is late on a running swarm:

```bash
agentihooks swarm rig-grade-swarm tick-report --passes 10
```

The step appears in two tables of the report: the step rows give its median and p95 wall time, and the outcome
columns give its late share and abandoned count. An abandoned record means a pass dropped the move; the next pass
retries it.

### Operator master move

```bash
agentihooks swarm rig-grade-swarm master move --to vm
```

The command checks the move before it writes anything, and refuses with these errors:

| Error | Cause |
|---|---|
| `no live master to move` | The swarm has no live master record. |
| `the master already runs on hive vm` | The target is the master's current hive. |
| `hive vm is not live` | The hive has no record, or its last heartbeat is over ninety seconds old. |
| `the master cannot move to hive vm: <reason>` | [Master placement](#master-placement) would not give the master that hive; the reason is the placement wait reason. |

On success it sends one durable inbox order from `operator` to the master seat and prints the pending move as
JSON, with `from`, `to`, `master`, `item` (the order's inbox id) and `state` set to `ordered`. The order tells the
master to finish its current step, write a Handoff v2 carrying its pending decisions, held messages, claimed task
needs, audit plan and operator overrides, run `agentihooks swarm <id> handoff <doc> --reason operator` and stop.
After that handoff, the tick starts the successor on the named hive and closes the order as done.

A second `master move` replaces the first: it cancels the earlier order in the inbox with "the master move was
replaced" and records the new target.

The successor is pinned only when its transfer follows the master the move named. A master that leaves without a
handoff, or a different master that takes the seat first, starts on normal placement and the pin is not applied.

The ledger page offers the same order: each live hive row in the Swarm tab's Hives panel carries a **move master**
button. It is disabled while another swarm control from the page is running, on a hive that is not live and on the
hive the master already runs on.

### Troubleshooting

- **The master's hive is down and the master did not move.** Check the hive's heartbeat with
  `agentihooks hive list`. A hive that never beat, or whose record was removed, never moves its master, and the move
  waits the full grace plus up to one tick. While the runtime still reports the master's session live, the tick
  leaves it alone.
- **The move happened one pass later than expected.** A pass that abandoned `master move fail over` dropped the
  move, and the next pass made it. Read the step's abandoned count with
  `agentihooks swarm rig-grade-swarm tick-report --passes 10`; see [Master move code preload](#master-move-code-preload).
- **The successor started on another hive than the one ordered.** The pin applies only to the successor of the
  master the order named. If that master left without the handoff the order asks for, its successor uses normal
  placement. Run `master move` again for the new master.
- **The old hive came back and its master is still running.** A returning hive terminates each fenced execution
  before its first heartbeat and takes no launch orders while one still runs; see [Fencing](#fencing).

## Fencing

Fencing stops a superseded agent or controller from acting after its replacement starts.

- **Seat generation.** A failover bumps the master seat's generation and records the old execution as fenced on
  its hive.
- **Returning hive.** Before it writes its first heartbeat, a hive that comes back terminates every fenced
  execution by its recorded process group. While one still runs it takes no launch orders.
- **Controller epoch.** Each controller holds a lease with an epoch. Launch orders and ledger writes carry it, and a
  write from an older epoch is refused with "the controller lease is stale". Release a lease by hand with
  `agentihooks swarm <id> controller release`.
- **Ledger source.** Moving a workstation ledger into the hosted service with `--freeze` fences the source: its
  SQLite database refuses writes with "ledger moved to hosted service" and media writes answer 409.

## Launches on a hive

The controller hands a hive a launch order; the hive's own Redis user receives it, launches the agent and writes
the acknowledgement the controller reads. What the order's payload carries and what the hive writes from it are in
[Prompt and workspace transport](#prompt-and-workspace-transport).

### Refusal causes

A launch the hive cannot complete is acknowledged `failed` with a cause, and the controller records the launch as
failed with "hive launch failed: `<cause>`".

| Cause | Meaning |
|---|---|
| `identity` | The order does not match the admitted execution: generation, lane, task, harness, profile, account slot, route, target hive or state differ, the seat holds a newer execution, or the started agent reported a different harness, profile or account. |
| `prompt` | The payload is missing, is not the order's task, or carries no opening prompt or a digest that does not match it. |
| `stale_epoch` | The order's controller epoch is older than the current controller lease when the hive takes it. |
| `refused` | The controller epoch moved on between the order check and the launch or right after it, or the launch raised a swarm error. |
| `expired` | The order expired before the hive took it, or the agent started after the expiry; the hive stops such an agent. |
| `ambiguous` | The agent may have started but its process could not be identified, or the order was taken 300 seconds ago with no result. |
| `<status>: <detail>` | The local runtime refused the spawn, such as `refused: <why>` or `unavailable: <why>`, with the runtime's own reason. |

### Launch check on a hive

A hive agent's launch check uses the time its own session started and never expects an overlay its host cannot
render. A hive without agentibrain installed launches engineers without the `brain` overlay, and they keep running.

When the hive acknowledges a launch as `live`, the acknowledgement's execution carries:

| Field | Value |
|---|---|
| `profile_decision.host_overlays` | The overlays the hive can render for the order's profile: every overlay the profile chain declares that resolves on the hive, then the worn overlays of the launch, without duplicates. |
| `launched_at` | The session start time the hive recorded, in milliseconds. |
| `launch_timings` | The launch timings the hive recorded for the session. |
| `overlays` | The worn overlays of the launch. |

The controller stores these fields on the execution record when it collects the acknowledgement, and the agent
record the tick reads carries them. A field the acknowledgement omits keeps the value the controller already holds.

The tick's launch check then reads:

- **Start time**: the later of `launched_at` (or the spawn time when `launched_at` is zero) and `harness_at`, taken
  from the launch timings when present, else the time the profile canary validated. A miss is judged only once the
  sixty second deadline, or the longer canary deadline, has passed since this time.
- **Expected overlays**: `host_overlays` when the agent's profile decision holds it, else the overlays the
  workstation declares for the profile and its worn overlays. This list applies to agents graded by process
  observation; a workstation agent has no `host_overlays`, so its check is unchanged.

An agent on a hive is graded by its session join report instead of process observation, so the overlay comparison
and the `late` check do not run for it. Every check the join report names is required except `brain`, which is
report only: a hive without the brain connector reports the `hive_brain` miss and keeps its agent running. Join
report grading for hive agents arrived after this change, in
[pull request #5481](https://github.com/the-cloud-clockwork/agentihooks/pull/5481). A headed hive's join report
also names `kubectl` and `git_identity`; see
[Headed hive checks](#headed-hive-checks-kubectl-and-git-identity).

Check a fresh hive launch:

```bash
agentihooks swarm <slug> status
# launch  engineer@<code>-<n>  passed  41250ms
# launch  engineer@<code>-<n>  reported  38800ms
#   hive_brain  report only  expected available; observed missing
```

Each `launch` line names the agent, the report state and the time from session start to the join, or to the check's
decision when the agent never joined. `passed` means no miss and `reported` means only report only misses, such as
`hive_brain` for a hive agent or `late` for a workstation agent. `failed` means an enforced miss: the tick retires the
agent and relaunches it once; a second enforced miss retires it without a relaunch. The report is kept per task under
the swarm key `<root>:swarm:<slug>:launch-check-reports`, where `<root>` is `agentihooks` unless the keyspace root is
set.

Before [pull request #5319](https://github.com/the-cloud-clockwork/agentihooks/pull/5319) the acknowledgement carried
none of these four fields. [Issue #5292](https://github.com/the-cloud-clockwork/agentihooks/issues/5292) records the
result on the Anton worker hive: the check expected the workstation's overlays, `brain` included, so every hive
engineer failed `overlay` sixty seconds after start and was retired, and `launched_at` read zero, so the start time
was wrong.

| Symptom | Cause | Fix |
|---|---|---|
| A hive engineer fails `overlay` and is retired soon after start | The hive runs a release older than pull request #5319, so its acknowledgement carries no `host_overlays`. | Upgrade the hive to a release carrying the change and roll its pod. |
| A hive agent's execution record shows `launched_at` 0 | The same older release. | The same upgrade. |

### Headed hive checks: kubectl and git identity

A headed hive is a hive whose registry record carries `ui=yes`. Placement ranks headed hives first for masters, `qa`
and `frontend`; a master falls back to a headless hive, while `qa` and `frontend` wait for a headed one (see
[Placement by role](#placement-by-role)). Every hive is meant to run agents with the workstation's configuration,
and `agentihooks hive compare` reports where one differs (see
[Comparing hive configuration](#comparing-hive-configuration)). An agent on a headed hive is also graded on two
tools the workstation always has: a `kubectl` that reaches the cluster and a global git identity for its commits.

When an agent's session starts, the join hook reads the hive's registry record and writes `headed` into the join
report's `context`: `true` when `ui` is `yes`, `false` otherwise. A headed report runs two more checks:

| Check | Command it runs as the agent's user | Passes when |
|---|---|---|
| `kubectl` | `kubectl get nodes --request-timeout=10s -o name` | The command exits 0 and prints at least one node. |
| `git_identity` | `git config --global user.name`, then `git config --global user.email` | Both commands exit 0 and print a value that is not blank. |

Each command has a 20 second timeout. A binary missing from `PATH`, a timeout and a nonzero exit all count as a
miss. The join report records no reason for these two checks, so a miss reads `kubectl (required)` with no error
text after it.

On a headed hive both checks are required, like every other join check except `brain`:

| Hive | `kubectl` and `git_identity` | Grade with both passing | Grade when either one misses |
|---|---|---|---|
| `ui=yes` | Run and required | `complete`, or `partial` when only `brain` misses | `failed` |
| `ui=no`, or no registry record | Not run and not graded | Unchanged | Not applicable |

The launch check reads the misses as `hive_kubectl` and `hive_git_identity`. Neither is report only, so the agent
fails its launch check and is handled as an enforced miss, as described in
[Launch check on a hive](#launch-check-on-a-hive). The tick's check keeps an agent that its idle heartbeat shows
working at the deadline and records the miss as held, so a working agent keeps running with the miss.

In this section's examples the `hive list`, `hive join` and `hive compare` lines separate their fields with tabs,
shown aligned here.

```bash
agentihooks hive list
# <id>  name=swarm-master  live  ui=yes  workers=1 of 6  ...
#   engineer@<code>-<n>  failed  misses=kubectl (required), git_identity (required)  heartbeat=<ms>

agentihooks swarm <slug> status
# hive join  engineer@<code>-<n>  failed  misses=kubectl (required), git_identity (required)  heartbeat=<ms>
# launch  engineer@<code>-<n>  failed  60412ms
#   hive_kubectl  enforced  expected available; observed missing
#   hive_git_identity  enforced  expected available; observed missing
```

#### Providing the tools on a headed hive

A headed hive needs three things, the same as the workstation:

1. **kubectl.** The linked bundle declares `kubectl` in its `deps.json`, so `agentihooks deps ensure` installs it
   on every hive that links the bundle. The bundle entry accepts kubectl 1.33.12 or newer. To install, it downloads
   the 1.33.12 release binary for Linux amd64 or arm64, checks it against the SHA256 pinned in the entry and writes
   `~/.local/bin/kubectl`. The install fails when `~/.local/bin` is not on `PATH`.
2. **A read only cluster config.** kubectl reads `~/.kube/config` for the hive's user, or the file `KUBECONFIG`
   names. Build it from a read only service account token held in the deployment's secret store, write it with
   mode `600`, and never commit it. The `view` ClusterRole does not cover nodes, so the account also needs
   `get` and `list` on nodes for the check to pass.
3. **A global git identity.** Set `user.name` and `user.email` in the hive user's global git config from the
   hive's bootstrap, keeping an identity already set. Worker hive pods do this in their start script with the name
   `agentihooks hive` and the email `agentihooks-hive@users.noreply.github.com`.

A read only account the check accepts, applied to the cluster by the deployment's GitOps repository:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: hive-reader
  namespace: <swarm namespace>
automountServiceAccountToken: false
---
apiVersion: v1
kind: Secret
metadata:
  name: hive-reader-token
  namespace: <swarm namespace>
  annotations:
    kubernetes.io/service-account.name: hive-reader
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: hive-reader-view
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: view
subjects:
- kind: ServiceAccount
  name: hive-reader
  namespace: <swarm namespace>
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: hive-reader-cluster
rules:
- apiGroups: [""]
  resources: [nodes]
  verbs: [get, list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: hive-reader-cluster
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: hive-reader-cluster
subjects:
- kind: ServiceAccount
  name: hive-reader
  namespace: <swarm namespace>
```

The cluster config the hive's provisioning writes from that token and the cluster CA, both read from the secret
store at write time:

```yaml
apiVersion: v1
kind: Config
clusters:
- name: swarm
  cluster:
    server: https://<cluster API address>:6443
    certificate-authority-data: <base64 CA from the secret store>
users:
- name: hive-reader
  user:
    token: <token from the secret store>
contexts:
- name: hive-reader
  context:
    cluster: swarm
    user: hive-reader
current-context: hive-reader
```

The git identity lines a bootstrap runs, which keep an identity already set:

```bash
git config --global user.name >/dev/null || git config --global user.name "agentihooks hive"
git config --global user.email >/dev/null || git config --global user.email agentihooks-hive@users.noreply.github.com
```

#### Checking a headed hive

```bash
# From any joined machine: mark the hive headed, then read it back
agentihooks hive set <id> ui=yes
agentihooks hive show <id>                       # "ui": "yes"

# On the hive, as the user its agents run as
agentihooks deps check                           # exit 0: nothing missing, kubectl included
kubectl get nodes --request-timeout=10s -o name  # one node/<name> line per node
kubectl auth can-i list nodes                    # yes
git config --global user.name                    # a name
git config --global user.email                   # an email

# From the workstation: kubectl is installed on the hive like on the workstation
agentihooks hive compare
# <id>  differs  ...
#   deps missing kubectl                         # only while the hive lacks it

# The checks each running agent on the hive recorded at join
P=${AGENTIHOOKS_SWARM_KEY_PREFIX:-agentihooks}
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HVALS "$P:hive:<id>:join-reports" \
  | jq -c 'select(.status == "running" and .context.headed) | {agent, grade, kubectl: .checks.kubectl, git_identity: .checks.git_identity}'
```

A healthy headed hive lists its agents as `complete misses=none` in `agentihooks hive list`.

#### Limitations

- The two checks run once, when the session joins. The heartbeat refresh probes again only the Redis, transcript,
  profile, settings, pane, start time and `brain` checks, and the ledger retry probes only `ledger_join` and
  `ledger_write`, so a running agent keeps its `kubectl` or `git_identity` miss after the hive is fixed. The next
  session on the hive runs both checks again.
- The `kubectl` check proves that the config reaches the cluster and can list nodes. It does not prove any other
  permission an agent's task needs.
- A hive's `ui` setting is read when the session joins. Changing it with `agentihooks hive set <id> ui=...` grades
  only the sessions that start afterwards.

#### Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `kubectl (required)` on a headed hive, and `command -v kubectl` prints nothing on the hive | `agentihooks deps check` on the hive | Run `agentihooks deps ensure`, and put `~/.local/bin` on the hive user's `PATH` when the install says kubectl is not on it. |
| `kubectl (required)`, kubectl installed, and `kubectl get nodes` says the connection to the server was refused or names `localhost:8080` | `ls -l ~/.kube/config` on the hive | The hive has no cluster config. Deliver it as in [Providing the tools on a headed hive](#providing-the-tools-on-a-headed-hive). |
| `kubectl (required)`, and `kubectl get nodes` prints `Forbidden` | `kubectl auth can-i list nodes` on the hive | Bind the account to a ClusterRole with `get` and `list` on nodes, as in the example. |
| `kubectl (required)`, and `kubectl get nodes` answers `Unable to connect to the server` with a timeout | The route from the hive to the cluster API address in the config | Open the route, or point the config at an address the hive reaches. |
| `git_identity (required)` | `git config --global user.name` and `git config --global user.email` on the hive | Set both in the hive's bootstrap, as in the example. |
| A headless hive's agents show no `kubectl` or `git_identity` | `agentihooks hive show <id>` reads `"ui": "no"` | Expected: only headed hives run the two checks. |
| A `ui=yes` hive's join reports carry no `kubectl` or `git_identity` check | The hive's agentihooks version | The hive runs a release from before pull request #10581. Upgrade it and restart its hive service. |

Before [pull request #10581](https://github.com/the-cloud-clockwork/agentihooks/pull/10581) a headed hive was
graded like a headless one. [Issue #10577](https://github.com/the-cloud-clockwork/agentihooks/issues/10577) records
the case: the automation VM hive ran with `ui=yes` and took workstation tasks, but a read only probe as its user
found no kubectl on `PATH`, no `~/.kube` and no global git `user.name` or `user.email`, and its agents' join reports
checked neither.

### A worker that finishes before the tick collects its acknowledgement

Until the tick collects a hive launch's `live` acknowledgement, the execution record names only the hive in its
runtime target (`host_id`). The process identity a retire needs (`process_namespace`, `pid`, `pid_start`), the
pane, the launch time and the launch timings arrive with the acknowledgement.

A short task can finish between the hive's acknowledgement and the next tick. The tick then collects the
acknowledgement of an execution already marked `finished`. It checks that the controller epoch, claimant, seat,
generation and acknowledging hive match the order, and copies the acknowledged runtime target, pane, launch time
and timings into the execution record. The record stays `finished`, the agent is not revived, the launch is
recorded as started, and the tick reports the action:

```text
<agent> finished <task> before the tick collected its hive acknowledgement
```

When the record already carries the acknowledged target, the tick leaves it unchanged. An acknowledgement that is
not `live` refuses the spawn and leaves the record `finished`. The refusal reason depends on what the receipt holds:

| Receipt | Spawn refused with |
|---|---|
| No acknowledgement | `the hive launch order ended before its acknowledgement` |
| An acknowledgement with status `failed` | `hive launch failed: <cause>`, the hive's own cause from [Refusal causes](#refusal-causes) |

A failed acknowledgement reads the same here as on a launch the tick collects while the execution still runs, so
the swarm timer journal and the Doctor's failed spawn finding carry the hive's cause. A Codex worker whose profile
canary timed out on the hive, for example, is written to the journal as:

```text
<slug>: spawn failed for <task>: hive launch failed: refused: agentihooks init-agent: mounted profile canary did not validate before timeout: the codex agent reported no canary within 90s
```

Before [pull request #8039](https://github.com/the-cloud-clockwork/agentihooks/pull/8039) a failed acknowledgement
on a finished execution was reported with the missing acknowledgement reason. The Doctor finding
`failed-spawn/t-runners-doc-worker001` read "the hive launch order ended before its acknowledgement" while the
hive's acknowledgement held the canary timeout above.

With the process identity recorded, the retire order the tick sends for that agent reaches the hive with a process
it can stop or find already gone, and the retire ends.

Before this, the record kept only `host_id`. Every tick sent a retire order that the hive refused because it could
not identify a process, the per agent retire count grew each tick, and from the third failing tick
`agentihooks swarm <id> status` listed a `retire failed` finding with this evidence:

```text
<agent> is still running after <n> retire attempts
process 0
refusal: hive <host> refused retire: runtime target names no process
```

On the Doctor swarm the finding for one ci agent reached 25 failing ticks and came back after a resolved verdict
because the count kept growing.

### A hive worker whose pane is gone

The tick cannot read another hive's panes or processes, so it judges a worker on another hive from that hive's
execution report. Every hive daemon publishes the report on each 15 second beat under `<root>:hive:<id>:executions`,
which expires after 45 seconds, so a hive that stopped beating has no report. The report carries:

| Field | What it holds |
|---|---|
| `observed_at` | When the hive took the snapshot, in milliseconds. |
| `started_at` | When the hive's own process started. |
| `names` | The agent names the hive's herdr lists. |
| `process_namespace` | The hive's process namespace. Written only while the hive runs at least one execution. |
| `pane_ids` | Every pane id herdr lists. Written only while the hive runs at least one execution; `null` when herdr refuses the pane listing or its answer cannot be read. |
| `admitted` | One entry per execution the hub admitted to this hive that no host target names yet, keyed by execution id: its `generation`, the `pane` this hive's herdr reads for it, and the live `binding` facts this hive reads for it (`null` when it reads none). |

A worker the hub admitted carries no host target, so the controller has no herdr that can read its pane. The tick
reads that worker's pane from the `admitted` entry of the hive that admitted it, and counts it idle or working from
that pane like any local worker; an entry for another generation, or none, reads `unknown`. A worker admitted to the
hive the tick runs on is read from that hive's own herdr. Both the controller and the hive must run a release that
carries this report field; until then the worker reads `unknown`, which counts as working and never reaches the idle
retire.

The live binding check reads such a worker's facts from the same entry's `binding` when the worker runs on a local
backend over the Codex app server transport, never from the controller's own server check: an app server's liveness
can only be proven on the host that runs it, so the controller's own check stores an unknown process fact and would record the
worker's model and effort as unobserved. An entry for another generation, an entry without a binding, or no entry at
all gives the worker no facts, and the tick skips it that pass as it does a hosted worker whose hive reports no
binding. Any other hub admitted worker keeps the controller's own session read, and an app server worker admitted to
the hive the tick runs on is read from that host. The hive reads each admitted binding the way it reads a hosted one,
so it also settles a dead app server of an admitted execution as failed, which frees its account slot.

A worker's execution record names its hive in its runtime target (`host_id`). The tick counts the worker gone,
and retires it with `its hive pane is gone`, when the report is no older than the worker's launch and one of these
holds:

- the hive process started after the launch;
- `names` lists other agents but not this one, six minutes or more after the launch;
- `pane_ids` is a list, and either the report's `process_namespace` differs from the one recorded at launch,
  which is a restarted hive pod, or the worker's pane id is not in the list.

The namespace check needs a readable pane listing: a report without `pane_ids` never retires a worker through it.
The tick also retires a worker with `its hive pane is gone` when its runtime reads the pane as `exited`. Other
reasons share the same path:

| Reason | When |
|---|---|
| `its hive no longer lists it` | All of: the hub admitted the worker before its hive recorded a host target; the hive that admitted the worker has a report; six minutes or more have passed since the launch; the report's `names` omits the worker. The last holds only while `names` lists other agents, or when the hive process started after the launch. |
| `its hive stopped reporting` | The worker's hive publishes no report and its registry beat is more than ten minutes old, the worker's own heartbeat is also older than ten minutes, and no declared wait holds. For a worker already `finished`, a hive beat older than 90 seconds is enough. |

The report keeps the claim when it cannot prove the pane gone:

| Report | Outcome |
|---|---|
| No report for the hive | The worker is not gone; the ten minute lost window applies instead. |
| `observed_at` before the worker's launch | The snapshot predates the worker, so it cannot retire it. |
| `pane_ids` is `null` or missing | An unreadable listing is an outage, never an empty one; neither the pane id nor the namespace is compared. |
| An empty `pane_ids` list | Every pane is gone; the worker is retired. |

A lane worker whose task is still active and whose own heartbeat is no older than ten minutes keeps its claim
no matter which of these reasons the tick finds: the tick refreshes its claim lease and judges it again next tick. A master
is never kept this way.

Retiring the worker:

1. clears any master retire request for it;
2. refunds the claim life of a task that is parked, was handed off at quota, or was handed off because an
   operator preempted this worker, so the reclaim does not count against it;
3. releases the task claim and removes the agent record, which frees the engineer seat for a successor execution;
4. reopens the task when the worker still held it and it is not waiting on a merge;
5. settles the worker's open inbox items: they move to its seat for the next occupant when the task reopens,
   otherwise they are withdrawn and each sender is told.

The tick sends the hive no retire order, so a replacement pane that reused the slot is never stopped. The hive's
launch slot frees on its own: the hive daemon refreshes launch slots only for executions whose agent record
still exists, so the dropped record's slot expires after its 45 second time to live. The tick prints one line
per worker:

```text
retired <agent>: its hive pane is gone, task <task> reopened
```

Both the controller and every hive daemon must run a release that carries
[pull request #5387](https://github.com/the-cloud-clockwork/agentihooks/pull/5387). A hive daemon on an older
release publishes no `pane_ids` or `process_namespace`, so its workers keep their claims until it is upgraded.

Before that pull request a hive worker whose pane disappeared stayed `suspect` without end: the record held its
engineer seat and the hive slot, and no successor could start.

To see what the tick reads, fetch the hive's latest report from the swarm Redis:

```bash
P=${AGENTIHOOKS_SWARM_KEY_PREFIX:-agentihooks}
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" GET "$P:hive:<id>:executions" \
  | jq '{observed_at, started_at, process_namespace, pane_ids}'
```

An empty answer means the hive has not beaten for 45 seconds.

### Join reports after a hive restart

Every agent on a hive writes one join report when its session starts: the SessionStart join hook grades its checks
and stores the report in the hash `<root>:hive:<id>:join-reports`, one field per execution id. When the session
ends, the hook writes `ended_at`, `exit_reason` and `status` into the same report; a Codex session's supervisor
writes the same end with `exit_reason` `codex transport exited` when its transport exits. A report without
`ended_at` keeps the agent listed on its hive while its agent record runs, and counted by the hive's drain.

An agent killed together with its pod never runs that end. The hive daemon's join report heartbeat, which runs on
every 15 second beat, ends those reports itself once the restarted daemon is back.

#### What a join report records about its member

| Field | Value |
|---|---|
| `member` | The host name of the machine or pod that runs the agent, `registry.member_name()`: the pod name on a worker hive. |
| `epoch` | `<boot id>/<start tick>`: the kernel boot id from `/proc/sys/kernel/random/boot_id`, and the start time of process 1 in clock ticks since boot, field 22 of `/proc/1/stat`. Empty when either cannot be read. |

A container restart inside the same pod, such as after an OOM kill, keeps the pod name and starts a new process 1,
so the epoch changes. A reboot changes the boot id. Restarting only the `agentihooks-hive` service on a machine
whose process 1 is its init system keeps the epoch. The start tick counts from boot, so a step of the wall clock
does not move it.

The join hook runs in the agent's session and the heartbeat in the hive daemon, so the check relies on both reading
the same host name and the same process 1. The hive daemon and the agents it starts run in the same pod or on the
same machine, and the hive chart sets none of `hostPID`, `hostNetwork` or `shareProcessNamespace`. Pull
request #11279 recorded the agent hook and the daemon reading one PID namespace on a worker pod and on the
workstation root hive; compare them on a live hive with the commands in
[Checking it on a live hive](#checking-it-on-a-live-hive).

#### What the heartbeat does on each beat

The heartbeat takes the hive's reports whose `status` is `running` and judges each in this order:

1. **Restarted member.** It ends the report with `exit_reason` `agent ended with its member's restart` and `status`
   `ended` when all of these hold: the daemon can read its own epoch, the report's `member` is the daemon's own host
   name, and the report carries an epoch that differs from the daemon's.
2. **Gone from the swarm.** It ends the report with `exit_reason` `agent gone from the swarm` and `status` `ended`
   when the swarm holds no agent record for the report's swarm, execution id and generation in a state other than
   `finished` or `ended`.
3. **Refresh.** A report both checks keep, whose `heartbeat_at` is 180 seconds old or older, gets `heartbeat_at` set
   to the beat time and its checks probed again. A check that passed at join stays passed.

The first two checks run on every beat; only the refresh waits 180 seconds. A restarted hive therefore ends the
reports it wrote before the restart on its first beat. An end is written only while the stored report still
belongs to the same session. Ended reports stay in the hash.

The restart check sorts the running reports this way:

| Report | Restart check |
|---|---|
| Written by this member under the current epoch | Kept |
| Written by this member under an earlier epoch | Ended with `agent ended with its member's restart` |
| Written by another member of the same hive | Kept; only the member that wrote a report ends it for a restart |
| No `member` or no `epoch`, written by a release before [pull request #11279](https://github.com/the-cloud-clockwork/agentihooks/pull/11279) | Kept |
| Any report, while the daemon cannot read its own epoch | Kept |

A kept report still goes through the gone check and the refresh.

#### What reads the report

| Reader | While the report runs | Once it has ended |
|---|---|---|
| `agentihooks hive list` | Lists the agent under its hive with its grade, misses and `heartbeat=<ms>`, the last heartbeat time in epoch milliseconds, while its execution still runs, and counts it in `workers=` unless it is a master. | Drops the agent from the hive's lines and from `workers=`. |
| `agentihooks hive drain`: the pod's preStop, the release roll and `hive remove` | Counts the agent as running and waits for it to hand off. When one member of a shared hive drains, it counts only agents whose recorded process namespace is its own. | No longer counts the agent, so the drain stops waiting on it. |
| The [stall watchdog](swarm.md#stall-watchdog) | Counts a task's hive owner as live while its report runs with a heartbeat no older than 360 seconds, twice the refresh interval. | Stops counting that owner as live, so the task can stall as `owner not live` after `AGENTIHOOKS_STALL_OWNER_MINUTES` (default 10). |
| `agentihooks swarm <slug> status` | Prints a `hive join` line for the agent. | Adds `exit=<reason>` and `status=ended` to that line. |

```bash
agentihooks swarm <slug> status | grep 'hive join'
# hive join  engineer@<code>-<n>  complete  misses=none  heartbeat=<ms>
# hive join  engineer@<code>-<m>  complete  misses=none  heartbeat=<ms>  exit=agent ended with its member's restart  status=ended
```

The fields are separated by tabs, shown aligned here.

The heartbeat changes only the join report. The agent record, its task claim and its inbox items stay until the
controller tick retires the agent, as in [A hive worker whose pane is gone](#a-hive-worker-whose-pane-is-gone).

Before [pull request #11279](https://github.com/the-cloud-clockwork/agentihooks/pull/11279) the heartbeat refreshed
every running report whose agent record still ran. That pull request records the incident: on 14 November the
worker container of `swarm-hive-001` was OOM killed and restarted at 17:09:58 UTC. At 21:48 UTC `ps` in the pod
showed no process for two engineers, while their join reports still read `running` with heartbeats from 21:50 UTC,
so `hive list` showed both as `complete` and `hive drain` counted them as running.

#### Upgrade

The join hook that records `member` and `epoch` and the daemon that compares them run from the same agentihooks
install on the hive. Upgrade the hive to a release carrying
[pull request #11279](https://github.com/the-cloud-clockwork/agentihooks/pull/11279) and restart its hive service or
roll its pod. Agents that start after the upgrade write both fields; reports of agents that started earlier carry
neither and are kept by the restart check.

#### Limitations

- A report without `member` or `epoch` ends only through the gone check, when its agent record ends.
- A pod that is deleted and replaced, rather than restarted, comes back under a new host name. The replacement
  reads the old reports as another member's and keeps refreshing them until their agent records end and the gone
  check ends them.
- The check needs each member to have its own host name and its own process 1. With `hostPID` set, process 1 is
  the node's init, so a container restart leaves the epoch unchanged; with `hostNetwork` set, every pod on a node
  takes the node's host name.

#### Checking it on a live hive

```bash
# On the hive: its member name and current epoch
hostname
echo "$(cat /proc/sys/kernel/random/boot_id)/$(sed 's/.*) //' /proc/1/stat | cut -d' ' -f20)"
# swarm-hive-001-agentihooks-swarm-worker-hive-68c5d5fb-wwszw
# e8adb1a1-f80b-4d66-962f-26f39eacd37d/189301145

# On the hive: the daemon and a Claude agent process read one PID namespace
readlink /proc/"$(pgrep -f 'agentihooks hive run' | head -1)"/ns/pid /proc/"$(pgrep -x claude | head -1)"/ns/pid
# pid:[4026532913]
# pid:[4026532913]

# From any joined machine: the running reports with the member and epoch each recorded
P=${AGENTIHOOKS_SWARM_KEY_PREFIX:-agentihooks}
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HVALS "$P:hive:<id>:join-reports" \
  | jq -c 'select(.status == "running") | {agent, member, epoch, heartbeat_at}'

# The reports the heartbeat ended for a restart
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" HVALS "$P:hive:<id>:join-reports" \
  | jq -c --arg r "agent ended with its member's restart" 'select(.exit_reason == $r) | {agent, member, epoch, ended_at}'
```

A running report whose `member` is the hive's host name and whose `epoch` differs from the hive's current one is
ended on the hive's next beat. If it is still running a beat later, either the hive daemon runs a release older than
pull request #11279, or the daemon cannot read its own epoch because `/proc/sys/kernel/random/boot_id` or
`/proc/1/stat` is unreadable in its pod, which keeps every report.

### Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| A `retire failed` finding shows `refusal: hive <host> refused retire: runtime target names no process` | The agent's execution record: does its runtime target carry `process_namespace`, `pid` and `pid_start`, or only `host_id`? | A record collected by a current controller carries the identity, and its retire ends. A record left with only `host_id` by an older controller keeps failing until the tick drops the agent from the swarm, which clears its retire count and the finding. |
| The tick printed "finished … before the tick collected its hive acknowledgement" | The execution record's state | Expected for a task that finished within one tick; the record stays `finished` and the launch is counted as started. |
| A spawn failed with `hive launch failed: <cause>` for an execution already `finished` | The cause, against [Refusal causes](#refusal-causes) | The hive acknowledged the launch as failed. Fix the cause on the hive; for a canary timeout see [Profile launch home](../reference/profile-launch-home.md). |
| A spawn failed with `the hive launch order ended before its acknowledgement` | `agentihooks hive list` for the order's hive | The order's receipt holds no acknowledgement, so the hive never answered it. Check that the hive beats and its `agentihooks-hive` service runs. |
| A hive worker whose pane closed stays `suspect` and holds its seat | The hive's report, as in [A hive worker whose pane is gone](#a-hive-worker-whose-pane-is-gone) | `pane_ids` `null` or missing: herdr refused the listing, or the hive daemon predates pull request #5387; fix herdr on the hive or upgrade it. `observed_at` older than the launch: the hive's snapshot is stale; check that it beats. A fresh heartbeat from the worker keeps the claim until it ages past ten minutes. |
| The tick printed `retired <agent>: its hive pane is gone` for a worker that was running | The report's `process_namespace` and `started_at` against the worker's launch | The hive pod restarted after the launch, which ends every process it ran. The task reopens for a successor. |
| After a hive pod restart, `hive list` still shows agents with fresh heartbeats whose processes are gone, and `hive drain` waits on them | Each running join report's `member` and `epoch` against the hive's own, with the commands in [Join reports after a hive restart](#join-reports-after-a-hive-restart) | No `member` or `epoch`: the report predates pull request #11279 and ends when its agent record ends. A `member` naming another pod: the pod was replaced, not restarted, so the report ends only when its agent record ends. The same member with an earlier epoch: the hive daemon runs an older release, so upgrade the hive; or the daemon cannot read its own epoch, so check that the two `/proc` files the epoch comes from are readable in the pod. The same member and epoch: the agent should still run, so look for its process in the pod. |
| `swarm status` shows `exit=agent ended with its member's restart` for an agent | The hive pod's restart time against the agent's launch | Expected: the agent's process ended with the restart, so `hive list` and the drain no longer count it. The agent record, its claim and its inbox items stay until the controller tick retires the agent, as in [A hive worker whose pane is gone](#a-hive-worker-whose-pane-is-gone). |

## Hive placement for one swarm on a local machine

A machine in `local` mode spawns every lane in its own herdr panes. `agentihooks swarm <id> set hive-placement=on`
makes that one swarm's tick place like a `hybrid` controller: eng, ci, plan and dispatch work goes to joined hives
with the role and room, and the master goes to a UI hive. Every other swarm on the machine, and every ledger client,
stays local, because `AGENTIHOOKS_DEPLOYMENT` is unchanged. Before turning it on, run the machine itself as a hive
(`agentihooks hive install`, then `agentihooks hive set <id> ui=yes max-agents=N`) so local work and the master keep a
hive to land on. `hive-placement=off` rolls back on the next
tick. On a machine in any other mode the setting changes nothing.

Hives fill first. When no hive takes an eng, ci, plan or dispatch spawn, the tick runs it in the machine's own herdr
panes while the machine runs fewer worker agents than `agentihooks swarm <id> set local-max-agents=N` (default 15),
counted across every swarm on the machine, and only while the host room guard finds load and memory room. At the cap
the spawn waits with the hive's reason and the local count.
Masters, qa and frontend keep the UI host rule, and a task pinned to a hive never runs locally.

### Placement order for a worker spawn

For each eng, ci, plan or dispatch spawn the tick decides in this order:

1. Hive placement picks a live hive that takes the role, has room and holds the repo, as in
   [Placement](#placement). When one does, the agent launches there. When the chosen hive is the machine itself, the
   spawn takes a local slot under the cap below.
2. When no hive takes it, the spawn falls back to the local machine only when all of these hold:
   - the controller is `hybrid`, or the machine is `local` and the swarm sets `hive-placement=on`;
   - the role is not `master`, `qa` or `frontend`;
   - the task does not set `hive`;
   - the machine's own herdr server answers;
   - the machine's hive record, if one exists, does not exclude the role or lane, and declares every capability in
     the task's `needs`.
3. The local machine takes the spawn while its local count plus the seats kept for waiting `workstation=yes` tasks
   is under the local cap. The tick claims a local slot for the agent, clears the task's placement wait and
   launches the agent in a local herdr pane.
4. Otherwise the spawn waits with the reason below, and a later pass tries it again.

A fallback that fails a condition in step 2 waits with the hives' reason alone, such as
`no hive for eng has room: anton-1 ...`. A fallback at the cap waits with the hives' reason followed by the local
count:

```text
no hive for eng has room: anton-1 ..., and the local machine runs 8 of its 8 agents: <room reason>: <detail>
```

### The local cap

The cap a spawn meets is the smallest of:

| Bound | Set with |
|---|---|
| `local-max-agents`, stored per swarm, default 15 | `agentihooks swarm <id> set local-max-agents=N` |
| The machine's hive `max-agents`, when the machine is a hive | `agentihooks hive set <machine> max-agents=N` |
| The agents running plus the [workstation admission room](#workstation-admission-room-and-the-swarm-share) | Not set; measured each pass |

The local count is the union of:

- the local worker agents every swarm on the machine records for this machine, leaving out masters, agents still
  starting with no pane, and finished or ended agents;
- every named Claude or Codex session running on the machine that is not a master;
- local slot claims made in the last five minutes whose agent has not appeared yet.

Agents on remote hives and in Kubernetes never count. The count leaves out agents on the spawning task in the same
swarm, so a successor does not count its predecessor. A slot claim is a Redis transaction on the machine's claim
hash, retried when another claim lands first, so launches in one pass, or in two swarms on the same machine, see
each other's claims. The agent records and running sessions behind the count are read again on every claim, so a
session started by hand is counted by the next claim.

A slot frees once its agent's record reads finished or ended and its session no longer runs, when a failed spawn
releases its claim, or when a claim whose agent never appeared turns five minutes old.

### Workstation admission room and the swarm share

Each local spawn reads the machine's admission room from a fresh host reading taken in the tick, with the local
agents already running and the launches still pending on the machine. A disk below the worktree floor gives room 0
with `disk: <free> GB free on the agent home, below the <floor> GB worktree floor` before any other limit is read.
Otherwise the room is the smallest of these limits:

| Limit | Rule |
|---|---|
| Host CPU | Whole agents at 0.5 CPU each under 80 percent of the CPUs, measured from CPU used when the reading carries it, else from the five minute load. With CPU used in the reading, a five minute load above 80 percent of the CPUs and the hive's own CPU at or above 40 percent of them gives room 0; the hive's own CPU is its pod load when the reading carries one, else CPU used. |
| Host memory | Whole agents at 850 MB each under 80 percent of total memory. |
| Pod CPU and memory | The same rules on the cgroup limits, when the machine runs in a pod with limits set. |
| Swarm share, CPU | 40 percent of the CPUs at 0.5 CPU per agent, less the local agents running: 12 agents on 16 CPUs. |
| Swarm share, memory | 40 percent of total memory at 850 MB per agent, less the local agents running. |

The swarm share yields to the machine's agent ceiling. When the machine is a hive, the tick reads its ceiling, the
same figure `hive list` shows as max agents: the computed ceiling from the hive's headroom reading, held under the
operator's `max-agents` cap when one is set. A swarm share limit counts only while it fits at least the agents left
under that ceiling, and drops out of the room otherwise. The share therefore never holds the machine below its
ceiling, while the host and pod CPU and memory lines still bind as before. On a machine with no hive record there is
no ceiling to yield to, and both share limits always count.

These two share limits apply only to local spawns on the workstation. The refusal
`with CPU used at or above the 40 percent swarm share, fits 0 more` is a different check, part of the host CPU line
every hive room reads: it refuses while the five minute load is above 80 percent of the CPUs and the hive's own CPU
is at or above 40 percent of them, and the ceiling never removes it. A hive in a pod reads both the five minute load
and CPU used for its whole node, which includes other pods, so when the reading carries the pod load the check counts
it and the refusal reads `with pod load <n> at or above the 40 percent swarm share`. An empty hive on a node shared
with other pods then gets the room of the CPU used line.

The room found this way is then held under `local-max-agents` and the hive `max-agents`, as in the table above.

A workstation with 16 CPUs, `max-agents=20`, `local-max-agents=20`, a five minute load of 4.32, a low one minute load
and ample memory, so the computed ceiling is at least 20, and 12 local agents running:

| Limit | Fits |
|---|---|
| Swarm share, CPU | 0 more, below the 8 left under the ceiling of 20, so it drops out |
| Host CPU | 16 more under the 80 percent line |
| `max-agents` and `local-max-agents` | 8 more |

The spawn is admitted and the local cap reads 20. Before
[#7039](https://github.com/the-cloud-clockwork/agentihooks/pull/7039) the share bound regardless of the hive's
`max-agents`, and the same fallback spawn waited at 12 with:

```text
no hive for eng has room: ..., and the local machine runs 12 of its 12 agents: cpu: 12 swarm agents at 0.5 CPU each on 16 CPUs, fits 0 more under the 40 percent swarm share
```

A spawn whose placement already chose the machine's own hive waits without the hives' reason in front:

```text
, and the local machine runs 12 of its 12 agents: cpu: 12 swarm agents at 0.5 CPU each on 16 CPUs, fits 0 more under the 40 percent swarm share
```

#7039 let the share yield to the stored
`max-agents`; [#10131](https://github.com/the-cloud-clockwork/agentihooks/pull/10131) changed the figure it yields
to into the computed ceiling held under that cap, so a host whose own reading allows fewer agents than `max-agents`
yields only that far.

When the ceiling itself equals what the share fits, the share still names the limit; with 10 agents running under a
ceiling of 12 the room reads
`cpu: 10 swarm agents at 0.5 CPU each on 16 CPUs, fits 2 more under the 40 percent swarm share`. A busy host binds on
its own line: at a five minute load of 12.00 on 16 CPUs the room reads
`cpu: five minute load 12.00 on 16 CPUs, 0.5 CPU per agent, fits 1 more under the 80 percent line` with 12 agents
running under any ceiling above 12.

To let the workstation run twenty local workers:

```bash
agentihooks hive set <machine> max-agents=20
agentihooks swarm <id> set local-max-agents=20
agentihooks hive list | grep <machine>
agentihooks swarm <id> autoscale --json | jq .local_slots
```

`hive list` shows the hive's max agents. `local_slots` reads 20, the smaller of `local-max-agents` and the hive's
stored `max-agents`; it does not read the host. The cap a spawn meets reads lower when the computed ceiling or a host
line is tighter: the spawn's refusal detail names the binding limit, and `autoscale --json` lists each hive's room
under `hive_admission` and names the binding limit under `binding`.

### Workstation with eight local agents

This layout runs at most eight worker agents on the workstation and the rest on remote hives.

```bash
agentihooks hive install
agentihooks hive set desk ui=yes max-agents=8
agentihooks swarm <id> set hive-placement=on
agentihooks swarm <id> set local-max-agents=8
agentihooks swarm <id> autoscale --json | jq .local_slots
```

`local_slots` reads `8`, the smaller of `local-max-agents` and the hive `max-agents`. Remote hives fill to their own
`max-agents` first; up to eight more workers run on the workstation; anything beyond waits. To keep every worker
off the machine, exclude the worker roles on its hive record
(`agentihooks hive set desk excluded=eng,ci,plan,dispatch`). `hive-placement=off` returns the swarm to local
spawning, as described above.

### Verify placement

```bash
agentihooks swarm <id> status
agentihooks hive list
TZ=UTC journalctl --user -u agentihooks-swarm.service --since "1 hour ago" --until now -g "local slot claim" --no-pager
```

An agent placed on a hive shows that hive in `status`; an agent that fell back runs in a local herdr pane. Each
local slot claim writes one journal line, granted or refused:

```text
<slug>: local slot claim <agent> for <task> on <machine> counted <n> of <maximum>: <names>
```

The claim is refused when `<n>` is at or above `<maximum>`. While the cap refuses spawns the spawn stall finding stays
clear; see [The local agent cap hold](swarm.md#the-local-agent-cap-hold).

### Troubleshooting local fallback

| Symptom | Cause | Fix |
|---|---|---|
| Hive slots are full and the workstation sits idle while eng and ci work waits on `no hive for eng has room` | A condition in step 2 of the placement order fails for this spawn. | Follow the row for this symptom in [Placement by role](#placement-by-role). |
| Waits read `the local machine runs 8 of its 8 agents` with fewer swarm agents than that | The count includes every swarm on the machine, named non master sessions started by hand, and claims under five minutes old. | Read the names in the `local slot claim` journal line; end the stray session or wait five minutes for a stale claim to lapse. |
| The cap reads below `local-max-agents` | The machine's hive `max-agents` or the host room is smaller. | `agentihooks hive show <machine>`; `agentihooks swarm <id> autoscale` shows the host room and reason. |
| Waits read `fits 0 more under the 40 percent swarm share` | The machine has no hive record, so the share has no ceiling to yield to, or the hive's ceiling, its computed figure held under `max-agents`, is at or below the local agents running; see [Workstation admission room and the swarm share](#workstation-admission-room-and-the-swarm-share). | Run the machine as a hive with `agentihooks hive install` and `agentihooks hive set <machine> max-agents=N`; raise `max-agents` when it is the lower figure; when `hive list` shows a computed max agents at the running count, the measured load or memory holds it there. |
| Waits read `with <r> kept for waiting workstation tasks` | Tasks marked `workstation=yes` wait for the machine, and their seats are kept from fallback spawns. | Expected; those tasks take the seats first. |
| A qa, frontend or master task never runs locally | The UI host rule: these roles never fall back. | Start a live UI hive that takes the role. |

## Worker hives in Kubernetes

The `agentihooks-swarm` chart runs worker pods from the hive worker image. Each pod:

1. Points git at `python3 -m scripts.github_app_cli credential` for github.com, which answers with a GitHub
   App installation token when `GITHUB_APP_PRIVATE_KEY` is set, else with `GITHUB_TOKEN`. In App mode
   the start script caches that token in `github-token` under the agentihooks home, private to the worker user. The
   token never reaches a remote URL or the log.
2. Clones the repos in `workers.repositories` under its repo root, and the bundle at `HIVE_BUNDLE_URL` and
   `HIVE_BUNDLE_REF` beside them as `agentihooks-bundle`.
3. Writes the container env file `hive-container.env` and, with `workers.ssh.enabled`, starts sshd
   ([Operator SSH into a worker hive](#operator-ssh-into-a-worker-hive)). Then it installs a mounted credential as
   `hive.env`, unless a saved `hive.env` already holds the same member's refreshed credential
   ([Member credential across restarts](#member-credential-across-restarts)). With no mounted credential and no
   `hive.env` yet, it waits for one, up to `HIVE_DELIVERY_WAIT_S` (default 900) seconds, then exits 1 with
   `worker-hive: no member credential was delivered within <n>s; run agentihooks hive add from the UI hive`.
4. Runs `agentihooks init` with the bundle linked, then `agentihooks deps ensure`. The role is rendered at spawn.
   Init imports from the image's locked venv; see
   [Python dependencies in the worker image](#python-dependencies-in-the-worker-image).
5. Sets itself `ui=no ephemeral=yes draining=no excluded=<workers.excluded> master-swarms=<workers.masterSwarms>
   prefer=<workers.prefer>`, by default `excluded=master master-swarms= prefer=`, keeping a stored excluded list, ui and ephemeral.
   `workers.prefer` takes `role:rank` pairs, such as `eng:1,ci:1,plan:1`: placement gives a role's agents to the
   lowest ranked hive while admission leaves it room. The same `hive set` writes `pinned=<yes|no>` from `workers.pinned`;
   a pod from a chart without that setting writes no pin. A second `hive set` then writes `max-agents=<workers.maxAgents>`
   without `--keep`, or `max-agents=auto` when the chart sets none, so every start replaces a stored or hand-set cap with
   the chart value.
6. Runs `agentihooks hive run`.

When the pod stops, for an image update, a rollout or a node drain, its preStop runs `agentihooks hive drain --wait
<workers.drainSeconds>` (default 3600): the hive sets `draining=yes`, so placement sends it no new agent, sends
each agent it hosts one inbox order to hand off, so the agent's successor starts on another hive, and waits until no
agent it hosts is running. An agent counts as running while its join report has not ended; see
[Join reports after a hive restart](#join-reports-after-a-hive-restart). The drain writes its result to the pod log.
The grace period is `workers.drainSeconds` plus 30 seconds, so a Recreate rollout starts the new pod as soon as the
old hive's agents hand off, or when the wait runs out. With `workers.rollout.enabled` the drain is bounded by
`workers.rollout.drainSeconds` and the hives roll one at a time; see
[Release roll one hive at a time](#release-roll-one-hive-at-a-time).

The bundle origin and the token come from the hive settings record through `workers.envFromSecrets`. Write the
whole record from the workstation with one command:

```bash
agentihooks hive setup --dry-run   # key names it would write and leave out; writes nothing
agentihooks hive setup             # into secret/k8s/agentihooks-swarm-workstation-hive-settings
agentihooks hive setup --path <record>
```

It takes every key the agentihooks env files define (`.env` and each `*.env` in the agentihooks home, except
`hive.env` and `hive-container.env`), with its value from the agent environment, and prints key names only; a key
the environment does not hold is skipped. It leaves out account tokens, which the accounts records carry,
`SWARM_SECRET` and `AGENTIHOOKS_LEDGER_AGENT_TOKEN`, values that are host paths and values that name localhost, a
loopback, unspecified or link local address, naming each with its reason. It adds the bundle origin as `hive bundle-origin` does and keeps the record's other keys.

To write only the origin, read from the `origin` remote and current branch of the linked bundle, else of
`AGENTIHOOKS_BUNDLE_PATH`:

```bash
agentihooks hive bundle-origin                 # into secret/k8s/agentihooks-swarm-workstation-hive-settings
agentihooks hive bundle-origin --path <record> # another record under the secret mount
```

It needs `OPENBAO_URL` and `OPENBAO_TOKEN`, keeps the record's other keys, and writes an empty origin when no bundle
is linked. A linked bundle with no `origin` remote naming a host and repository is refused. [Bundle on a worker
pod](#bundle-on-a-worker-pod) covers what the pod does with the origin.

`agentihooks hive join /path/to/invite` enrolls a machine from one private invite
file. It reads the file without executing it, refreshes the member through the
hive endpoint and saves its Redis and NATS credentials privately. The hub operator
issues that file with `agentihooks hive invite`, using `--bus-url` for the NATS
address reachable by the member. A `--like` invite copies that address. When
the NATS signer record lists the member's swarms, the credential reaches only
those swarms' subjects and streams; see
[Swarm scoped bus credentials](../reference/swarm-bus-scope.md). Bus and
ledger addresses survive later credential refreshes. A private hub CA must be
trusted before joining, for example through `SSL_CERT_FILE`.

Bus credentials become scoped once the hub signer record carries `member_swarms`.
`agentihooks hive scope`, run with `AGENTIHOOKS_NATS_SIGNER` naming that record,
creates the entry and lists every existing member with each `--swarm`, or every
swarm when none is given. From then on the hive endpoint rereads the record on
each refresh and refuses a member it does not list before rotating its Redis
password, and `hive invite` lists each new member and marks its invite
`AGENTIHOOKS_BUS_SCOPED=1`. `hive join` with such an invite refuses an unscoped
bus credential. Before the backfill the hub mints no member bus credential:
refreshes, joins and `hive root-bus` refuse, and `hive invite` and `hive create`
refuse before minting a member, naming `agentihooks hive scope`. The
join refusal guards a scoped member against a downgrade; the hub refusal is the
enforcement, since anyone holding the invite file can edit its marker.

The hive only Helm example uses the same invite: create a Secret with
`kubectl create secret generic hive-invite --from-file=invite=/path/to/invite`,
then install the chart with `-f deploy/helm/agentihooks-swarm/examples/hive-only.yaml`.
It deploys one worker and no hub or NATS server. Worker startup runs `hive join`
before configuring the hive or accepting work, and refuses startup if enrollment
fails. GitHub and provider account credentials remain separate Secret references.

`workers.credentialSecret` is required; it carries the member invite and, with `workers.ssh.enabled`, also the
sshd `authorized_keys` and host key the pod installs for `agentihooks hive add` to reach it. Credentials come from
ExternalSecrets by name. Replicas that mount the same credential would report the same hive id and agent limit, so
the chart refuses `workers.replicas` above 1. Scale with `workers.hives`: each entry, `name` plus its own
`credentialSecret`, renders one more hive pod, `<release>-worker-hive-<name>` with component `worker-hive-<name>` and
its own worktrees ConfigMap; [Scaling worker hives](#scaling-worker-hives) gives an example and the scaling
refusals. Add a pod with no installed credential with `agentihooks hive add` from the UI hive; the pod name is used
only then.

The pod's memory follows its agent count: `workers.memoryPerAgentMi.request` (default 700) and `.limit` (default
2048), in MiB, are multiplied by `workers.maxAgents` into the container's memory request and limit, so the scheduler
places the pod only on a node with room for every agent it may run. The chart refuses a request below one or a limit
below the request, and memory set under `workers.resources`. `workers.nodeSelector`, `workers.affinity` and
`workers.tolerations` place the pod; by default it schedules on any node and never uses the host network.
[Worker hive memory and placement](#worker-hive-memory-and-placement) gives examples.

`workers.homeVolume` is the volume source of `/home/worker`, which holds the repo clones, worktrees and scratchpad. It
defaults to an emptyDir on the node root disk; a `hostPath` of type `Directory` on a data disk, or a
`persistentVolumeClaim` (set `emptyDir: null` beside it; the chart refuses more or fewer than one source) keeps them on
a larger disk. A hostPath of type `Directory` starts the pod only where the path exists, so create it only on the
mounted data disk. Any source other than an emptyDir rolls the pod out by
Recreate so two hives never share it. `workers.worktreeMinFreeGb` (default 50), `workers.worktreeMaxPerRepo` (default 60)
and `workers.worktreeStaleDays` (default 7) reach agents as `WT_MIN_FREE_GB`, `WT_MAX_PER_REPO` and `WT_STALE_DAYS`
through the release's worktrees ConfigMap: `wt.sh new` evicts the oldest safe worktrees to keep that floor and cap, and
`agentihooks gc` removes worktrees untouched that many days.

`hiveStorage` keeps hive host folders in one per node setting. `hiveStorage.nodes.<node>` names `homes`, an optional
`tmp` and an optional `mount` (`path`, `fsType` and `marker`, a path that exists only on the data disk);
`hiveStorage.default` serves nodes without an entry. A hive that sets `workers.node` is pinned to that node, mounts
`<homes>/<folder>` as `/home/worker` and `<tmp>/<folder>` as `/tmp` (an emptyDir without `tmp`), where the folder is the
release name, or `<release>_<name>` for a `workers.hives` entry, which inherits `workers.node`; it replaces `homeVolume`
and rolls out by Recreate. A pre-install and pre-upgrade Job makes both folders for uid 10001 first, and refuses unless
`mount.path` is an `fsType` filesystem holding `mount.marker`, so an unmounted data disk never gets hive folders on the
system drive. `hiveStorage.homeFolders.hives` lists hives whose `homeVolume` is set by hand under `default.homes`; one
DaemonSet makes their folders on every node `homeFolders.nodeSelector` selects, except the nodes `hiveStorage.nodes`
lists. [Worker hive home volume](#worker-hive-home-volume) gives examples, checks and refusals.

### Python dependencies in the worker image

The worker pod runs `agentihooks` from `/opt/venv`. That venv carries the same Python dependencies as the
workstation install, `pip install -e ".[all]"`: every package in `[project] dependencies` of `pyproject.toml` and
every package of the `all` extra, such as `croniter`, `redis`, `boto3`, `psycopg2-binary` and `nats-py`.

The image fills the venv in two layers:

| Layer | Dockerfile | Python install |
|---|---|---|
| Worker base | `docker/swarm-node/Dockerfile` | `pip install --require-hashes -r requirements.lock` from `docker/swarm-node/requirements.lock`, offline from the downloaded wheels, then the venv is made read only |
| Hive layer | `deploy/worker/Dockerfile` | the agentihooks wheel with `--no-deps`, built from a clone of `AGENTIHOOKS_REF` by default or from the build context with `AGENTIHOOKS_SOURCE=local`, as the kind proof builds it, and the pinned `ruff`, into a build stage venv copied over `/opt/venv`; the build requirements stay in that stage's system Python |

The hive layer resolves no runtime dependency of agentihooks, so `requirements.lock` is the only source of the
packages agentihooks imports in the pod. A package that `pyproject.toml` requires and the lock leaves out is missing
in the pod while the workstation has it. Pods built before [pull request #5200](https://github.com/the-cloud-clockwork/agentihooks/pull/5200)
had this gap: `agentihooks init` in step 4 failed with `ModuleNotFoundError: No module named 'croniter'`, and agents
in that pod ran without the profile.

Two checks keep the lock and the workstation install equal:

| Check | Where it runs | Fails when |
|---|---|---|
| `test_base_lock_satisfies_every_requirement_of_the_workstation_install` in `tests/deployment/test_worker_hive_image.py` | the Tests workflow and the merge queue unit suite | a requirement of `[project] dependencies` or the `all` extra has no pinned version in `requirements.lock` that its specifier accepts |
| Init check in `deploy/worker/kind-proof.sh` | the `kind` job of the Worker hive pod workflow (`hive-worker.yml`) | `agentihooks init --profile engineer` in the built image fails, or its `~/.claude/CLAUDE.md` lacks the line `<!-- profile: engineer -->` |

The init check runs the built image as a Pod with an empty, memory backed `/home/worker` and `fsGroup` 10001, so
init renders into a fresh home writable by the worker user instead of the bare image's home.
[Worker hive pod image build](../ci-timing.md#worker-hive-pod-image-build) covers the rest of that proof.

To add or raise a dependency:

1. Change `pyproject.toml`.
2. Regenerate the lock from its existing pins plus the new requirement, so every other entry keeps its version.
   Replace `<package>` with the package's name and `<specifier>` with its requirement from `pyproject.toml`, such as
   `croniter` and `>=6.2.4,<7`; the second grep drops its old pin when it has one. The lock also pins `setuptools`
   and `setuptools-scm`, which build
   the agentihooks wheel in the base; they stay in the input.

   ```bash
   in=~/scratchpad/agentihooks/worker-lock/input.txt
   mkdir -p "$(dirname "$in")"
   grep -oE '^[A-Za-z0-9._-]+==[^ ]+' docker/swarm-node/requirements.lock \
     | grep -viE '^<package>==' > "$in"
   echo '<package><specifier>' >> "$in"
   uv pip compile "$in" --generate-hashes --python-version 3.12 \
     --python-platform x86_64-unknown-linux-gnu \
     --output-file docker/swarm-node/requirements.lock
   ```

3. Write the lock's new checksum into `requirements_sha256` in `docker/swarm-node/versions.lock`. The base build
   refuses a lock whose checksum differs:

   ```bash
   sha256sum docker/swarm-node/requirements.lock
   ```

4. Push. A change under `docker/swarm-node` is a base input, so the Worker hive pod proof rebuilds the base from the
   new lock and runs the init check on it, and the Swarm worker image workflow publishes the new base on `dev`.

To check a running pod after a rollout, read its start log and import the packages init needs inside it:

```bash
kubectl logs deployment/<release>-worker-hive -n <namespace> \
  | grep -E 'worker-hive: agentihooks init|ModuleNotFoundError'
kubectl exec deployment/<release>-worker-hive -n <namespace> -- \
  python -c 'import croniter, redis, boto3, psycopg2, nats; print("worker venv imports: ok")'
```

A healthy start prints neither grep pattern, and the import prints `worker venv imports: ok`.

| Symptom | Cause | Action |
|---|---|---|
| The pod log shows `ModuleNotFoundError: No module named '<package>'` from `agentihooks init` | The pod runs an image whose base lock lacks a package `pyproject.toml` requires | Roll the pod onto a current image. On a current image, add the package to `requirements.lock` as above. |
| `test_base_lock_satisfies_every_requirement_of_the_workstation_install` fails with `unmet` naming requirements | `pyproject.toml` gained or raised a requirement the lock does not satisfy | Regenerate the lock and its checksum in the same pull request. |
| The `kind` job fails after the build, with `init.txt` in the `worker-hive-proof` artifact showing an init error or nothing | `agentihooks init --profile engineer` failed in the image, or rendered a profile other than `engineer` | Read `init.txt`; fix the import or render failure it names. |
| The base build fails on the requirements checksum | `requirements_sha256` in `versions.lock` does not match `requirements.lock` | Write the `sha256sum` output into `versions.lock`. |

### Worker hive home volume

`/home/worker` holds every repo clone, worktree and scratchpad the hive's agents use, so its disk decides how many
worktrees fit. `wt.sh new` refuses below `WT_MIN_FREE_GB` free on that disk, and a node root disk with less free space
than the floor stops every engineer on the hive at its first worktree. Put the home on a data disk with room above the
floor. Choose one of three forms:

| Form | Set | Home and `/tmp` | Rollout |
|---|---|---|---|
| Default | nothing | emptyDir on the node root disk, lost with the pod | RollingUpdate |
| Hand set source | `workers.homeVolume` | the hostPath or claim you give; `/tmp` stays an emptyDir | Recreate |
| Per node storage | `workers.node` and `hiveStorage` | `<homes>/<folder>` and `<tmp>/<folder>`, made by a Job before the pod starts | Recreate |

**Hand set hostPath.** Pin the hive to the node that carries the data disk and mount a folder on that disk. Type
`Directory` makes the kubelet refuse the mount when the folder is missing, so the pod never starts on the root disk of a
node whose data disk did not mount. Create the folder, owned by uid 10001, on the mounted disk before the rollout:

```yaml
workers:
  homeVolume:
    emptyDir: null                 # one source only; the chart refuses two
    hostPath:
      path: /mnt/data/agentihooks-hive
      type: Directory
  nodeSelector:
    kubernetes.io/hostname: worker-001
  worktreeMinFreeGb: 50
```

**Hand set claim.** A `persistentVolumeClaim` keeps the home on whatever disk its storage class provides. It gains
nothing when the only storage class provisions on the node root disk.

```yaml
workers:
  homeVolume:
    emptyDir: null
    persistentVolumeClaim:
      claimName: swarm-hive-001-home
```

**Per node storage.** `workers.node` names the node; its `hiveStorage.nodes` entry, else `hiveStorage.default`, gives
the host folders. The chart pins the pod to that node, mounts `<homes>/<release>` as `/home/worker` and
`<tmp>/<release>` as `/tmp`, and runs a pre-install and pre-upgrade Job (`<fullname>-storage`, an ArgoCD PreSync hook
when `workers.rollout.enabled`) on that node that makes both folders for uid 10001 with mode 0750. With `mount` set,
the Job first checks that `mount.path` is an `fsType` filesystem holding `mount.marker` and fails otherwise, so the sync
stops before any folder lands on the system drive. Both `homes` and `tmp` must sit under `mount.path`.

```yaml
workers:
  node: worker-001
  hives:
    - name: b
      credentialSecret:
        name: swarm-hive-001-b-join
        key: credential
hiveStorage:
  nodes:
    worker-001:
      homes: /mnt/data/agentihooks-hive/homes
      tmp: /mnt/data/agentihooks-hive/tmp
      mount:
        path: /mnt/data
        fsType: xfs
        marker: .data-disk          # a file that exists only on the data disk
```

With release `swarm-hive-001`, the base hive mounts `/mnt/data/agentihooks-hive/homes/swarm-hive-001` and entry `b`
mounts `/mnt/data/agentihooks-hive/homes/swarm-hive-001_b`; each `workers.hives` entry gets its own `-storage-<name>`
Job and inherits `workers.node` unless it sets its own.

**Folders for hand set hives on many nodes.** When several hives set `homeVolume` by hand to folders under one root,
`hiveStorage.homeFolders` makes those folders on every node it selects, outside the nodes `hiveStorage.nodes` lists,
with a DaemonSet that keeps running:

```yaml
hiveStorage:
  default:
    homes: /mnt/data/agentihooks-hive
  homeFolders:
    hives: [swarm-hive-002, swarm-hive-003]
    nodeSelector:
      agentihooks/hive-disk: "true"
```

**Check before a sync.** Render the release and read the home volume, its strategy and placement:

```bash
helm template swarm-hive-001 deploy/helm/agentihooks-swarm -f values-hive.yaml \
  | grep -nE 'type: Recreate|name: home$|hostPath:|path: |kubernetes.io/hostname'
```

**Check after the rollout.** Read the free space the agents see and the floor they get. `<fullname>` is the release
name when it contains `agentihooks-swarm`, else `<release>-agentihooks-swarm`:

```bash
kubectl -n <namespace> exec deploy/<fullname>-worker-hive -- df -h /home/worker
kubectl -n <namespace> exec deploy/<fullname>-worker-hive -- printenv WT_MIN_FREE_GB
kubectl -n <namespace> logs job/<fullname>-storage
```

`df` must show the data disk size, not the node root disk, and free space well above `WT_MIN_FREE_GB`. The storage Job
is deleted once it succeeds, so its log exists only while it fails or runs. One engineer on the hive passing
`wt.sh new` proves the floor holds.

### Worker hive home volume refusals

The chart refuses these values at render time; the storage Job and the kubelet report the rest on the cluster.

| Symptom | Cause | Fix |
|---|---|---|
| `workers.homeVolume needs exactly one volume source; set emptyDir: null beside a hostPath or persistentVolumeClaim` | The default `emptyDir: {}` merged with the source you added. | Set `emptyDir: null` beside the hostPath or claim. |
| `workers.node <node> has no homes: set hiveStorage.nodes.<node>.homes, or hiveStorage.default.homes when the node has no entry` | `workers.node` names a node with no homes folder. | Add `homes` to that node's entry or to `hiveStorage.default`. |
| `hiveStorage for node <node> names a mount without its fsType and marker` | `mount.path` is set alone. | Give `mount.fsType` and `mount.marker`. |
| `hiveStorage folder <folder> for node <node> is not under its mount <path>` | `homes` or `tmp` lies outside `mount.path`. | Move the folder under the mount, or fix `mount.path`. |
| Storage Job log: `<path> on <node> is not the <fsType> disk holding <marker>, so its data disk is not mounted` | The data disk is unmounted, has another filesystem, or lacks the marker. | Mount the disk on the node, or create the marker on it, then sync again. |
| Pod stays in `ContainerCreating` with a `FailedMount` event naming the home hostPath | A hand set `Directory` hostPath does not exist on the node. | Create the folder on the mounted data disk, owned by uid 10001, or use `hiveStorage` so the Job makes it. |
| `hiveStorage.homeFolders.hives needs hiveStorage.default.homes` | The DaemonSet has no root to make folders under. | Set `hiveStorage.default.homes`. |
| `hiveStorage.homeFolders.hives entry "<name>" must be a folder name: lowercase letters, digits and dashes` | An entry holds another character, such as `_`. | Name the hive's home folder, and the entry, without that character. |
| `wt.sh new` refuses below the free space floor inside the pod | The home disk has less free space than `WT_MIN_FREE_GB`. | Move the home to a larger disk or run `agentihooks gc`; never lower `workers.worktreeMinFreeGb` to pass. |

### Bundle on a worker pod

A worker pod installs the same bundle, profiles, rules, skills and MCP servers as the workstation. The workstation
knows its bundle only as a host path, which the settings record never carries, so the record names it as an origin:

| Key | Value | Written from |
|---|---|---|
| `HIVE_BUNDLE_URL` | `https://<host>/<owner>/<name>.git`, or empty | the `origin` remote of the linked bundle, else of `AGENTIHOOKS_BUNDLE_PATH` |
| `HIVE_BUNDLE_REF` | a branch name, or empty | the bundle checkout's current branch |

`agentihooks hive setup` and `agentihooks hive bundle-origin` write both keys. An `ssh` or `https` remote is written
as the same `https` address, so the pod clones it with the GitHub token. A bundle checkout on a detached head is
refused with `the linked bundle at <path> is not on a branch`. The pod reads the keys through
`workers.envFromSecrets`; chart values never set them.

The pod uses them in steps 2 and 4 of [Worker hives in Kubernetes](#worker-hives-in-kubernetes). `HIVE_BUNDLE_REF`
defaults to `dev`. A clone a persistent home kept is fast forwarded from the origin instead of cloned again; a failed
fast forward logs `worker-hive: bundle fast forward failed; init runs on the kept clone`. `agentihooks init --bundle
<clone>` links the bundle and renders its rules, skills, MCP servers and the `agenti` alias.

| Situation | Log line | Result |
|---|---|---|
| `HIVE_BUNDLE_URL` empty | none | No clone. Any bundle a persistent home kept is unlinked and init runs on the package roles alone, so a hive without a private bundle keeps working. |
| Clone fails | `worker-hive: bundle clone failed; init runs on the package roles alone` | The pod serves on the package roles; `hive compare` shows `bundle missing <origin>`. |
| Init with the bundle fails | `worker-hive: agentihooks init with the bundle failed; init runs on the package roles alone` | The bundle is unlinked and init runs again on the package roles alone. |
| Init on the package roles fails | the init error | The pod stops. |
| `deps ensure` fails | `worker-hive: agentihooks deps ensure failed; each launch retries the bundle dependencies` | The pod serves; each launch retries the missing dependencies. |

The image carries node, npm, uv and gh; npm global installs and uv tools land in `~/.local` under the worker home.

`agentihooks hive compare` reads each hive's linked bundle as a lowercase `<host>/<owner>/<name>` in its `bundle`
section. A pod with no bundle shows `bundle missing <origin>` against the workstation, and a pod linked to another
bundle shows both `bundle missing <workstation origin>` and `bundle extra <pod origin>`. The branch is not compared.
Profiles shipped by the bundle, rules, skills, MCP servers and dependencies follow in their own sections, so a
missing bundle also shows as missing entries there.

To bring a pod to the workstation's bundle and check it:

```bash
# On the workstation, with OPENBAO_URL and OPENBAO_TOKEN set
agentihooks hive bundle-origin
# OpenBao secret/<record>: wrote HIVE_BUNDLE_REF, HIVE_BUNDLE_URL (<n> keys total); bundle https://github.com/<owner>/<bundle>.git at dev

# Roll the pod so its start script reads the record
kubectl rollout restart deployment/<release>-worker-hive -n <namespace>

# In a shell on the pod
agentihooks bundle list                 # the linked bundle is agentihooks-bundle under the repo root
ls ~/.claude/rules ~/.claude/skills     # the bundle's rules and skills
grep 'alias agenti' ~/.bashrc

# On the workstation, once the pod's next beat has published its configuration
agentihooks hive compare
```

`hive compare` then lists the pod as `equal`. Host paths are never part of the snapshot, so a different repo root or
home folder is not a difference.

### Member credential across restarts

`hive.env` lives in the pod's agentihooks home, so it outlives a container restart when the home is on a persistent
volume. Every `agentihooks hive refresh`, whether run by hand or by the swarm Redis client after a refused login, mints
a new Redis password, retires the previous one and saves the new Redis URL into `hive.env`. The ledger credential
in `hive.env` stays the same, so it names the member across refreshes.

At start, before `hive join`, the pod picks its `hive.env` from the mounted credential file (`HIVE_CREDENTIAL_FILE`):

| Saved `hive.env` | Start installs |
|---|---|
| None | The mounted credential |
| Same `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL` as the mounted credential | Nothing: the saved file, with the newest refreshed Redis password, stays |
| A different `AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL` | The mounted credential, replacing the saved one |

The mounted copy holds the Redis password of the original join, which the first refresh retired. A release from before
this rule installed the mounted copy on every start, so after any refresh the hive registry update at start failed with
`AuthenticationError` and the pod crash looped. A pod that mounts a new member, for example after `hive remove` and a
new invite, still replaces the saved file, because its ledger credential differs.

When the saved Redis password is refused anyway, the swarm Redis client refreshes the member through the hive endpoint
and retries; with the ledger CA in place (see [How refresh trusts the endpoint](#how-refresh-trusts-the-endpoint)) the
pod reaches `hive run` without operator action.

```bash
# The pod's saved member and the mounted one name the same member
kubectl exec -n <namespace> <pod> -- bash -c '
  key="^AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL="
  if cmp -s <(grep "$key" "$HIVE_CREDENTIAL_FILE") <(grep "$key" "$AGENTIHOOKS_HOME/hive.env"); then
    echo same member
  else
    echo different member
  fi
'

# The pod is Running, and on the swarm host its hive shows live
kubectl get pod -n <namespace> <pod>
agentihooks hive list
```

| Symptom | Cause | Fix |
|---|---|---|
| The worker hive pod is in `CrashLoopBackOff` and its log ends in `AuthenticationError: the hive Redis credential was refused and the hive refused a refresh (...)` | The saved Redis password was retired and the automatic refresh failed; the text in brackets names why | Read the bracketed reason. `CERTIFICATE_VERIFY_FAILED` means the refresh does not trust the endpoint's CA: check the pod sets `LEDGER_CA_PEM` from the ledger CA. `the ledger credential belongs to no hive member` means the member was removed: deliver a new credential with `agentihooks hive add` or a new invite |
| The pod crash loops with `AuthenticationError` after every restart while a refresh by hand works | The pod runs a release from before a restart kept the refreshed `hive.env` | Roll the pod onto a current image |

### Accounts on a worker pod

A worker pod holds every Claude and Codex account the workstation routes, never one account per pod. Its hive reads
each `AH_CC_TOKEN_<slug>` and `AH_CX_TOKEN_<slug>` in its environment by name and advertises one subscription slot per
account, so placement and the launch reservation can put an agent of any account on the pod. Give the pod the tokens
through one Secret in `workers.envFromSecrets` whose keys are the environment variable names. In Anton the
`<hive>-accounts` ExternalSecret extracts every `AH_CC_TOKEN_*` key from the `agentihooks/claude-creds` record and
every `AH_CX_TOKEN_*` key from `agentihooks/codex-creds` in OpenBao, and drops any other key:

```yaml
workers:
  envFromSecrets:
    - swarm-hive-001-env
    - swarm-hive-001-accounts   # every AH_CC_TOKEN_* and AH_CX_TOKEN_* key, named as its variable
    - swarm-hive-001-settings
```

A token added to either record reaches the pod at the next ExternalSecret refresh (one hour in Anton) and the next pod
start, since environment variables are read at container start. `agentihooks hive setup` never writes account tokens
into the settings record; the accounts records carry them.

The pod holds the tokens but never probes their quota, so its own readings stay missing and its subscription caps
would read 0. Each beat, a subscription slot with no reading, or an older one, takes the cap and quota reading of the
newest reading any other live hive published for the same account, usually the workstation's; see
[Account caps on every hive](#account-caps-on-every-hive). The pod therefore admits up to the account's real cap, and
the launch reservation still counts the account's sessions across every hive against that one cap.

Check that a pod carries every account and real caps from the workstation:

```bash
agentihooks hive compare --base <workstation id>   # no "accounts missing AH_CC_TOKEN_<slug>" line for the pod
agentihooks hive show <pod hive id>                # "slots": one subscription slot per account, each with its cap
```

| Symptom | Cause | Fix |
|---|---|---|
| `hive compare` prints `accounts missing AH_CC_TOKEN_<slug>` or `AH_CX_TOKEN_<slug>` for the pod | The accounts Secret lacks the key: the token is not in the OpenBao record, the key is not prefixed `AH_CC_TOKEN_` or `AH_CX_TOKEN_`, or the pod started before the Secret refreshed. | Add the token to the record under its variable name, wait for the ExternalSecret refresh or force one, then roll the pod. |
| A pod slot shows `cap` 0 while the workstation shows a cap for that account | No other live hive has published a reading for the account: the workstation is stale or does not hold that token either. | Start or restart the workstation hive so it probes the account; `agentihooks hive list` shows which hives are live. |
| The pod lists no Codex slot for an account it holds | The Codex router refuses the token: it is not a usable Codex credential, or it expires sooner than the router's minimum token life. | Replace the token in `agentihooks/codex-creds`; see [Codex accounts](load-balancing.md#codex-accounts). |

A distributed controller can also serve a workstation swarm without worker hives, launching that swarm's engineer and
CI work as Pods while its master, planners and dispatcher stay on the workstation; see
[Serving a live swarm from a distributed controller](../reference/deployment-modes.md#serving-a-live-swarm-from-a-distributed-controller).

### Worker hive Redis connection

A worker pod's heartbeat, sessions report and agent readings go to the Redis its member URL names. In Anton that URL
reaches the pod from OpenBao as an environment variable, so a move of the swarm Redis is a change to one OpenBao
property, never an edit inside the pod.

**Where the URL comes from.** The `agentihooks-swarm-workstation-hive-settings` ExternalSecret, which the pod reads
through `workers.envFromSecrets`, maps the `redis_url` property of the member record
`k8s/agentihooks-swarm-workstation-hive` into two keys:

```yaml
data:
  - secretKey: AGENTIHOOKS_HIVE_REDIS_URL
    remoteRef:
      key: k8s/agentihooks-swarm-workstation-hive
      property: redis_url
  - secretKey: AGENTIHOOKS_SWARM_REDIS_URL
    remoteRef:
      key: k8s/agentihooks-swarm-workstation-hive
      property: redis_url
```

`redis_url` holds the member's own Redis URL on the current swarm Redis: user `hive-<id>`, its password and the Redis
address. Both keys carry the same value, so a process reaches the same Redis whether it resolves the member URL or the
plain swarm URL. No agentihooks command writes this property: `hive invite` writes the `credential` property of the
record, and `hive setup` writes the separate settings record.

**Which URL a process uses.** The swarm Redis client takes `AGENTIHOOKS_HIVE_REDIS_URL` unless the process runs in
local mode, else `AGENTIHOOKS_SWARM_REDIS_URL`, else `redis://127.0.0.1:6379/0`. The agentihooks env files never
overwrite a variable already in the process environment, so `hive run` and every process the pod starts use the
URL from the Secret, ahead of the member file `hive.env`. Two cases change that:

| Case | URL used |
|---|---|
| `hive.env` names a different Redis user than the environment, as after a rejoin as another member | the `hive.env` member and URL replace the environment's |
| Redis refuses the environment's member login | the `hive.env` URL, when it answers; otherwise the client refreshes the member through the hive endpoint, saves the new grant to `hive.env` and uses it |
| An ssh shell or herdr pane on the pod, which has no pod environment | the env files: `hive-container.env` loads before `hive.env`, so the `hive.env` URL wins |

`hive.env` is rewritten at every pod start, when `hive join` refreshes the member through the hive endpoint, and at
every `agentihooks hive refresh`. Each refresh creates or resets user `hive-<id>` on the Redis the hive endpoint
serves and gives it a new password.

**Moving the swarm Redis.** A pod keeps reporting to the Redis its URL names for as long as that Redis accepts the
login. When the swarm moves and the old Redis stays up, the hive goes `stale` in `hive list` while its pod stays
`Running` and accepts ssh sessions. Point it at the new Redis through OpenBao:

1. Make sure the hive endpoint serves the new Redis and the member's ledger credential is in the new Redis's hive
   index. Without the index entry a refresh answers 403 `the ledger credential belongs to no hive member`.
2. Refresh the member so user `hive-<id>` exists on the new Redis, and copy the refreshed URL into the record without
   printing it:

   ```bash
   ssh anton-hive agentihooks hive refresh
   ssh anton-hive "grep -m1 '^AGENTIHOOKS_HIVE_REDIS_URL=' ~/.agentihooks/hive.env | cut -d= -f2-" \
     | bao kv patch secret/k8s/agentihooks-swarm-workstation-hive redis_url=-
   ```

3. Force the settings ExternalSecret to resync instead of waiting out its one hour refresh, then roll the pod, since
   it reads environment variables only at container start:

   ```bash
   kubectl -n agentihooks-swarm annotate externalsecret agentihooks-swarm-workstation-hive-settings \
     "force-sync=$(date +%s)" --overwrite
   kubectl -n agentihooks-swarm rollout restart deployment/agentihooks-swarm-worker-hive
   ```

Check the result without printing the URL:

```bash
kubectl -n agentihooks-swarm get externalsecret agentihooks-swarm-workstation-hive-settings   # STATUS SecretSynced
agentihooks hive list                                       # the worker hive reads live again
agentihooks swarm <slug> status                             # an agent placed on the worker hive's id
redis-cli -u "$AGENTIHOOKS_SWARM_REDIS_URL" ACL GETUSER hive-<id>   # on the new swarm Redis: the user exists
```

Keep the old Redis running until the hive reads `live` on the new one: the pod keeps reporting to it until the roll,
so it stays the rollback copy.

| Symptom | Cause | Fix |
|---|---|---|
| `hive list` shows the worker hive `stale` while its pod is `Running` | The member URL still names the old swarm Redis, which accepts the login, so the pod never fails over | Follow the move steps above |
| After the roll, hive commands fail with `the hive Redis credential was refused and the hive refused a refresh (...)` | The record's password was replaced by a later refresh, or user `hive-<id>` is missing on the new Redis, and the automatic refresh through the hive endpoint failed for the reason in the brackets | Fix that reason (see [Hive endpoint troubleshooting](#hive-endpoint-troubleshooting)), refresh the member and copy the URL again, then resync and roll |
| `hive refresh` answers 403 `the ledger credential belongs to no hive member` | The new Redis lacks the member's hive index entry | Copy the hive records to the new Redis, or issue a new member with `agentihooks hive invite` |
| The ExternalSecret reports an error for `redis_url` | The record has no `redis_url` property | Write it as in step 2 |

### Operator SSH into a worker hive

`workers.ssh.enabled` runs sshd inside the worker pod, so the operator can open a shell or a herdr session on the
hive and `agentihooks hive add` can deliver a member credential to it. It is off by default.

```yaml
workers:
  ssh:
    enabled: true
    port: 2222        # 1024 or higher; sshd runs as the unprivileged worker user
  credentialSecret:
    name: worker-hive-join
    key: invite
```

The Secret named by `workers.credentialSecret` must then hold two more keys beside the member credential:

| Secret key | Mounted at | Content |
|---|---|---|
| `authorized_keys` | `/run/hive-join/ssh/authorized_keys` | The operator public keys allowed to log in, one per line |
| `host_key` | `/run/hive-join/ssh/host_key` | The sshd private host key, so the host key stays the same across pod restarts |

Deliver them through the same ExternalSecret as the credential. The chart refuses a port below 1024 with
`workers.ssh.port must be 1024 or higher: sshd runs as the unprivileged worker`.

**What the pod runs.** The start script installs the host key mode 0600 under `$AGENTIHOOKS_HOME/ssh`, writes
`$AGENTIHOOKS_HOME/ssh/sshd_config`, checks it with `sshd -t`, and starts `sshd -D -e`, which logs to the pod log at
`VERBOSE`. Without a mounted host key, outside the chart, it generates an ed25519 host key once and keeps it. The
configuration:

- listens on `0.0.0.0` at `workers.ssh.port` and allows only the worker user;
- accepts public keys from the mounted `authorized_keys` only: no password, keyboard interactive or PAM login, no
  root login;
- turns off TCP and agent forwarding, gateway ports, tunnels, X11 and user environment files;
- sets `PATH` to `~/.local/bin`, `/opt/venv/bin` and the system folders, with `NPM_CONFIG_PREFIX` and
  `AGENTIHOOKS_HOME`, so `agentihooks` and the npm global tools resolve in an ssh shell, which runs `/bin/bash`, the
  worker user's login shell in the hive worker image;
- turns `StrictModes` off, because the keys sit on a read-only Secret mount the worker cannot own.

sshd starts before the credential step, so a pod with no mounted credential already serves the keys while it waits
for `hive add` to deliver `hive.env`. The pod fails before serving any agent when `sshd -t` rejects the configuration
or host key, or when sshd has exited one second after launch. An sshd that exits later is not restarted: restart the
pod.

**Container env file.** An ssh shell does not inherit the pod's environment. Each start writes
`$AGENTIHOOKS_HOME/hive-container.env` (mode 0600), which agentihooks loads with the other `*.env` files in its home,
so `agentihooks` commands in an ssh shell or herdr pane see the pod's settings. It copies the variables whose names
start with `AH_`, `AGENTIHOOKS_`, `HIVE_`, `SWARM_`, `LANGFUSE_`, `BRAIN_` or `AMYGDALA_`, plus `KB_ROUTER_TOKEN` and
`SSL_CERT_FILE`, each value shell quoted. It leaves out `SWARM_SECRET` and `AGENTIHOOKS_LEDGER_AGENT_TOKEN`, values
with a newline, and values with a single quote or backslash, which python-dotenv cannot read back; each of the last
kind logs `worker-hive: <name> holds a quote or backslash, which python-dotenv cannot read back; left out`.
`agentihooks hive setup` never copies this file into the hive settings record.

**Publishing the port.** The chart names the container port `ssh` and creates no Service for it. Publish it from the
deployment overlay, for example with a LoadBalancer on a LAN address:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: agentihooks-swarm-worker-hive-ssh
spec:
  type: LoadBalancer
  selector:
    app.kubernetes.io/name: agentihooks-swarm
    app.kubernetes.io/instance: <release>
    app.kubernetes.io/component: worker-hive     # worker-hive-<name> for a workers.hives entry
  ports:
    - name: ssh
      port: 2222
      targetPort: ssh
```

**Workstation setup.** Give the hive an ssh config host with the operator key whose public half is in
`authorized_keys`:

```text
Host anton-hive
  HostName <LoadBalancer address>
  Port 2222
  User worker
  IdentityFile ~/.ssh/<operator key>
```

Then check the login, save the machine in herdr and open a pane on it:

```bash
ssh anton-hive agentihooks balance            # the hive's accounts, read through hive-container.env
herdr machine add --label anton-hive anton-hive
herdr machine list                            # anton-hive is listed and enabled
```

`herdr machine add` prepares the remote herdr server over that ssh host before saving the machine, so a saved
machine has already been reached once. A pane opened on it runs in the pod as the worker user.

| Symptom | Check | Fix |
|---|---|---|
| The pod exits at start right after an `sshd -t` error in its log | The `host_key` value in the credential Secret | Store a valid OpenSSH private key under `host_key`. |
| `Permission denied (publickey)` | The key ssh offers (`ssh -v anton-hive`) against the Secret's `authorized_keys` | Add the operator public key to `authorized_keys` in the Secret's source. sshd reads the mounted file at each login, so the key works once the ExternalSecret and the kubelet have synced it. |
| `Connection refused` or a timeout | `kubectl get svc` for the ssh Service and its address; the pod's `ssh` port | Publish the port as above, or start the pod with `workers.ssh.enabled`. |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` after a restart | Whether the Secret holds `host_key` | Keep the host key in the Secret so restarts reuse it; then remove the old line with `ssh-keygen -R`. |
| `agentihooks` is not found, or runs without the hive's accounts, in an ssh shell | That the pod was started by the current start script, and the start log for a `left out` line | Restart the pod on a current image; move a value with a quote or backslash out of a copied variable. |
| `herdr machine add` fails while `ssh anton-hive` works | Whether `HOME` differs from the passwd home: herdr's health check calls ssh without `-F`, so ssh reads the passwd home's config | Put the host in `~/.ssh/config` of the passwd home. |

### Worker hive memory and placement

With the defaults, every worker hive pod has no node pin and no host network: the scheduler places it on any node
whose free memory covers its request, and the hive reaches the swarm over the network the node already has, so a new
node joins the pool with no chart change.

**Memory per agent.** The chart computes the container's memory from two values and the agent count:

```yaml
workers:
  maxAgents: 4
  memoryPerAgentMi:
    request: 700    # MiB the scheduler reserves per agent
    limit: 2048     # MiB the pod may use per agent before the kernel kills it
  resources:        # CPU and ephemeral storage only; memory here is refused
    requests:
      cpu: 100m
```

| `workers.maxAgents` | Memory request | Memory limit |
|---|---|---|
| 1 | `700Mi` | `2048Mi` |
| 3 | `2100Mi` | `6144Mi` |
| 4 | `2800Mi` | `8192Mi` |
| 8 | `5600Mi` | `16384Mi` |

Each `workers.hives` entry computes its own memory from its own `maxAgents`, or the inherited one. A
`memoryPerAgentMi` set on an entry replaces the whole map, so give both `request` and `limit`. Raise `request` when
agents on the hive measure above 700 MiB each, so the scheduler stops placing more agents on a node than it can hold;
raise `limit` when the pod log shows agents killed for memory while the node has room.

Inside the pod, admission counts the pod's memory limit as one more limit, at 850 MB per agent under 80 percent of that
limit ([Headroom](#headroom)); `hive list` prints it as `pod-limit-mb`. A launch refused with a `ram` detail that
names the pod, `pod <n> of <total> MB available`, means the agents already running use the pod limit: raise
`memoryPerAgentMi.limit`, or lower `maxAgents`.

**What pins a pod.** A hive pod is held to particular nodes only by these keys:

| Key | Effect |
|---|---|
| `workers.node` | Adds `kubernetes.io/hostname: <node>` to the pod's `nodeSelector` and uses that node's `hiveStorage` homes and tmp paths for its home and `/tmp` volumes. |
| `workers.nodeSelector` | Copied into the pod's `nodeSelector`. |
| `workers.affinity` | Copied into the pod's `affinity`. |

With all three empty, the defaults, the rendered pod has no `nodeSelector`, `affinity`, `nodeName` or `hostNetwork`.
`workers.tolerations` only lets the pod onto tainted nodes; it never limits where it goes. Check a values file for
pins before installing it:

```bash
helm template proof deploy/helm/agentihooks-swarm -f my-hives.yaml \
  | grep -nE '^ +(nodeSelector|affinity|nodeName|hostNetwork):'
```

No output means every worker hive in the release can schedule on any node. The chart's other components set their
own affinity in their own values keys; a match the grep shows outside a `-worker-hive` Deployment is theirs.

**Raising agents step by step.** Raise `maxAgents` on one hive at a time, so each step proves the new agents launch
before the next one:

1. Check the node room. The new request is `maxAgents` times `memoryPerAgentMi.request`; at least one node must have
   that much allocatable memory free:

   ```bash
   kubectl describe nodes | grep -A8 'Allocated resources'
   ```

2. Set the new `workers.maxAgents` (or the entry's `maxAgents`) in the release values and deliver it through the
   values repository, as every other chart change. The Deployment rolls the pod; its preStop drains the old hive first
   (see the drain above).
3. Confirm the pod scheduled and the hive took the new cap. A pod held `Pending` with `Insufficient memory` in
   `kubectl describe pod` means no node has room for the new request: lower `maxAgents` or add a node.

   ```bash
   agentihooks hive list   # tab separated; headroom fields abridged
   # anton-2  live  ui=no  workers=0 of 4 (cap 4)  excluded=master  ...  pod-limit-mb=8192  pending=0
   ```

4. Confirm agents use the room: within the next ticks `workers=<n> of <maxAgents>` rises as placement sends the hive
   agents, and each new agent claims its task on the ledger. Then take the next step.

### Sizing a pinned hive to its node

A hive held to one node by `workers.node`, `workers.nodeSelector` or `workers.affinity` can only grow into that
node's memory. Size it from the node's budget and the agents' measured use, in this order:

1. Read the node's allocatable memory and what the pods other than the hives already request, and the node's
   current use:

   ```bash
   kubectl describe node <node> | grep -A6 -E '^Allocatable|Allocated resources'
   kubectl top node <node>
   ```

   The service reserve is the requests of every pod on the node except the worker hives, rounded up. It covers the
   other pods the node runs, such as a database, so a hive raise never starves them.

2. Measure what one agent uses on the hive today: the pod's memory divided by its running agent count.

   ```bash
   kubectl top pods -A | grep worker-hive
   agentihooks hive list   # workers=<running> of <ceiling>
   ```

3. Pick `memoryPerAgentMi.request` at or above the measured mean, and `memoryPerAgentMi.limit` with room above it
   for the agents that run larger than the mean. Then pick the largest `maxAgents` for which every hive limit on the
   node plus the service reserve stays inside allocatable:

   ```text
   sum over the node's hives of (maxAgents x memoryPerAgentMi.limit) + service reserve <= allocatable
   ```

   The limit is the bound, not the request: the chart multiplies both into the pod only, and no agent has a memory
   cgroup of its own. Past the pod limit the kernel OOM killer ends a process in the pod cgroup; when that is the
   hive's main process, the container restarts and every agent on the hive ends.

4. Check CPU against the pod's CPU limit. Inside the pod, admission refuses a launch unless 0.5 CPU fits under 80
   percent of the pod's CPU limit, measured on the pod's load ([Headroom](#headroom)). Launches stop once pod load
   nears that line, so the hive reaches `maxAgents` only while its agents average about 0.8 times the CPU limit
   divided by `maxAgents`. A higher `maxAgents` with too small a CPU limit leaves the hive at a ceiling below its
   cap, with launches refused for `cpu`.

5. Render the values and read the pod's memory, then deliver them through the values repository and confirm the new
   cap and pod limit, as in the raising steps of [Worker hive memory and placement](#worker-hive-memory-and-placement).

**Worked example: a stable hive on a 92 GiB node.** One node runs a stable hive, a small dev hive and the swarm's
services. Measured before the raise:

| Reading | Value |
|---|---|
| Node allocatable memory (`kubectl describe node`) | 94322 MiB, 16 CPU |
| Requests of pods other than the two hives | 23876 MiB, kept as a 24576 MiB (24 GiB) service reserve |
| Stable hive pod (`kubectl top pods`) | 12103 MiB with 16 agents, about 756 MiB per agent |
| Dev hive limit | 8184 MiB |
| Stable hive pod CPU limit (`workers.resources.limits.cpu`) | 7 CPU |

The stable hive went from 28 agents at 1024 MiB request and 2048 MiB limit to 48 agents at 1024 MiB request and 1280
MiB limit. The lower per agent limit still leaves 1.7 times the measured mean, and pays for twenty more agents in the
same pod limit budget:

```yaml
workers:
  maxAgents: 48
  memoryPerAgentMi:
    request: 1024
    limit: 1280
```

| | Before | After |
|---|---|---|
| `maxAgents` | 28 | 48 |
| Pod memory request | `28672Mi` | `49152Mi` |
| Pod memory limit | `57344Mi` | `61440Mi` |
| Hive limits plus reserve | 57344 + 8184 + 24576 = 90104 MiB | 61440 + 8184 + 24576 = 94200 MiB |

94200 MiB fits the node's 94322 MiB allocatable. The render shows the new pod memory, limit first, then request; a
`-storage` Job, when the hive renders one, follows with its own `64Mi` and `16Mi`:

```bash
helm template proof deploy/helm/agentihooks-swarm -f stable-hive.yaml -s templates/worker-hive.yaml \
  | grep -E '^ +memory: [0-9]+Mi'
#           memory: 61440Mi
#           memory: 49152Mi
```

After the pod rolls, `hive list` shows the new cap and pod limit:

```bash
agentihooks hive list   # tab separated; headroom fields abridged
# <stable hive>  live  ui=no  workers=16 of 48 (cap 48)  ...  pod-limit-mb=61440  pending=0
```

Pod memory admission does not hold this hive below 48: 48 agents at the measured 756 MiB use about 36300 MiB, under
the 80 percent line of 61440 MiB. CPU can: launches stop once pod load reaches 80 percent of the 7 CPU limit, which at
48 agents is an average of about 0.12 CPU each, so raise the CPU limit with `maxAgents` when `hive list` shows a
ceiling below the cap.

**Hold the budget in a test.** When the values live in a repository with a test suite, record the measured
allocatable and service reserve there and assert the budget, so a later raise that no longer fits the node fails in
CI instead of at the kernel. An excerpt, where `stable` and `dev` are the two hives' `workers` values:

```python
NODE_ALLOCATABLE_MI = 94322
SERVICE_REQUESTS_MI = 24 * 1024


def test_hives_fit_the_node() -> None:
    """Both hive pod limits plus the service reserve fit the node's allocatable memory."""
    stable_memory = stable["maxAgents"] * stable["memoryPerAgentMi"]["limit"]
    dev_memory = dev["maxAgents"] * dev["memoryPerAgentMi"]["limit"]
    assert stable["maxAgents"] >= 48
    assert stable_memory + dev_memory + SERVICE_REQUESTS_MI <= NODE_ALLOCATABLE_MI
```

Re-measure the node and update both constants when the node's memory or its other pods change.

### Scaling worker hives

One hive credential is one hive. The hive id inside a member credential is fixed when the member is minted, and the
member's Redis user writes only that hive's own keys and the shared worker keys, so every pod that mounts the same
credential registers as the same hive with the same `max-agents`. Placement would then see one hive and one agent
limit however many pods run. The chart therefore renders each hive as exactly one pod and refuses two hives in one
release that share a credential Secret.

To run more hives, mint one member per hive and give each its own Secret:

```bash
agentihooks hive invite hive-a   # writes the member credential into secret/k8s/agentihooks-hive-hive-a
agentihooks hive invite hive-b
agentihooks hive invite hive-c
```

Each `hive invite` mints a new member with its own hive id. Sync each record into its own Kubernetes Secret through
ExternalSecrets, then list the extra hives under `workers.hives`:

```yaml
workers:
  replicas: 1          # 1 renders the base hive; 0 renders only the workers.hives entries
  maxAgents: 8
  credentialSecret:
    name: hive-a-member
    key: credential
  hives:
    - name: b
      credentialSecret:
        name: hive-b-member
        key: credential
    - name: c
      maxAgents: 4
      credentialSecret:
        name: hive-c-member
        key: credential
```

The example renders three Deployments, each with one replica: `<fullname>-worker-hive` (hive a, 8 agents),
`<fullname>-worker-hive-b` (8 agents, inherited) and `<fullname>-worker-hive-c` (4 agents), where `<fullname>` is
`<release>-agentihooks-swarm`, or the release name alone when it already contains `agentihooks-swarm`. Each pod sets
its own `max-agents` at start and its memory request and limit follow its own `maxAgents`. `agentihooks hive list`
shows three hives, each with its own max agents, and placement holds each to its own limit. An entry inherits every
other `workers` key; a key it sets, such as `credentialSecret` or `homeVolume`, replaces the whole value, so write the
complete map. Entries with a `workers.node` inherit it and get their own host folder, `<release>_<name>`. Installing
one release per credential, each with `workers.replicas: 1`, also gives one pod per credential; Deployment names and
host folders then derive from each release name, and no duplicate check runs across releases.

Check a values file before installing it; this command lists the worker hive Deployments the render produces,
and a refusal from the table below stops the render instead:

```bash
helm template proof deploy/helm/agentihooks-swarm -f my-hives.yaml | grep -E '^  name: .*-worker-hive'
```

`deploy/worker/chart-proof.sh`, run by the `contracts` job of the Worker hive pod workflow, renders one replica at
`maxAgents` 3 and asserts its `2100Mi` request, `6144Mi` limit and that the pod has no `nodeSelector`, `affinity`,
`nodeName` or `hostNetwork`. It also asserts that a missing credential, `workers.replicas=2` with one credential, a per
agent limit below its request and a memory value under `workers.resources` are each refused.

### Reaching a total agent count in stages

A release's worker hive capacity is the sum of each pod's `maxAgents`: the base hive's when `workers.replicas` is 1,
plus every `workers.hives` entry's own or inherited value. Placement holds each hive to its own ceiling, so the swarm
runs at most that sum of agents on the release's pods. The swarm's lane caps bound it too: a swarm set to
`max-eng-agents=4 max-ci-agents=2` launches at most six eng and ci agents however much hive room there is. A target
total, such as twenty agents, is a split of that sum across hives, each with its own member credential.

**Sizing the split.** Every pod's memory request is its `maxAgents` times `memoryPerAgentMi.request`, and each pod
must fit on one node. Fewer, larger hives need nodes with more free memory; more, smaller hives spread over smaller
nodes. With the defaults of 700 MiB request and 2048 MiB limit per agent, a split of twenty agents as 8, 8 and 4
renders:

| Hive | `maxAgents` | Memory request | Memory limit |
|---|---|---|---|
| base (`<fullname>-worker-hive`) | 8 | `5600Mi` | `16384Mi` |
| `b` (`<fullname>-worker-hive-b`) | 8 | `5600Mi` | `16384Mi` |
| `c` (`<fullname>-worker-hive-c`) | 4 | `2800Mi` | `8192Mi` |
| total | 20 | `14000Mi` | `40960Mi` |

The values that produce it:

```yaml
workers:
  replicas: 1
  maxAgents: 8
  memoryPerAgentMi:
    request: 700
    limit: 2048
  credentialSecret:
    name: hive-a-member
    key: credential
  hives:
    - name: b
      credentialSecret:
        name: hive-b-member
        key: credential
    - name: c
      maxAgents: 4
      credentialSecret:
        name: hive-c-member
        key: credential
```

Total the split before installing it; the render prints each pod's request:

```bash
helm template proof deploy/helm/agentihooks-swarm -f my-hives.yaml -s templates/worker-hive.yaml \
  | grep -E '^  name: .*-(worker-hive|storage)|^ +memory: [0-9]+Mi'
```

Each `-worker-hive` Deployment name is followed by its pod's memory limit, then its request. A hive with
`workers.node` set also renders a `-storage` Job, whose name line is followed by its `64Mi` limit and `16Mi` request;
leave those out of the total.

**Scaling in stages.** Reach the total one values change at a time, and prove each stage before the next. A stage is
one of: raising one hive's `maxAgents`, or adding one `workers.hives` entry. Toward the split above:

| Stage | Values change | Hive capacity after it |
|---|---|---|
| 1 | Base hive at `maxAgents: 8` | 8 |
| 2 | Add entry `b`, inheriting 8 | 16 |
| 3 | Add entry `c` at `maxAgents: 4` | 20 |

For each stage:

1. Mint the member for a new entry first with `agentihooks hive invite <name>` and sync it into its own Secret
   ([Scaling worker hives](#scaling-worker-hives)); an entry whose Secret does not exist leaves its pod unable to
   start.
2. Check node room for the new or raised pod's request, as in
   [Worker hive memory and placement](#worker-hive-memory-and-placement).
3. Deliver the values change through the values repository's pull request, as every other chart change. Adding an
   entry creates one new Deployment; raising an entry's `maxAgents` rolls only that hive's pod.
   Raising `workers.maxAgents` also raises and rolls every entry that inherits it. A hive with a hostPath or claim home rolls by Recreate: the old
   pod's preStop drains its agents before the new pod starts. A hive on the default `emptyDir` home rolls by
   RollingUpdate, so the new pod starts while the old one still drains.
4. Confirm the stage: every worker hive pod is `Running`, and `agentihooks hive list` shows each hive live with its
   cap, the `(cap <n>)` values summing to the stage's capacity. The number after `of` is the ceiling, which reads
   below the cap while the pod's headroom is short ([Headroom](#headroom)):

   ```bash
   kubectl get pods -l app.kubernetes.io/name=agentihooks-swarm | grep worker-hive
   agentihooks hive list   # tab separated; headroom fields abridged
   # <hive-a>  live  ui=no  workers=8 of 8 (cap 8)  excluded=master  ...
   # <hive-b>  live  ui=no  workers=5 of 8 (cap 8)  excluded=master  ...
   # <hive-c>  live  ui=no  workers=0 of 4 (cap 4)  excluded=master  ...
   ```

5. Raise the lane caps when the swarm's caps sit below the new capacity, so placement has agents to send. For stage 3,
   14 eng and 6 ci agents fill the 20:

   ```bash
   agentihooks swarm <slug> set max-eng-agents=14 max-ci-agents=6
   ```

6. Confirm agents use the new room: within the next ticks the new hive's `workers=<n>` rises as placement sends it
   agents. A pod held `Pending` with `Insufficient memory`, or a hive missing from `hive list`, stops the rollout at
   this stage: fix it before the next stage.

**The hive max agents setting.** Each pod writes `max-agents=<maxAgents>` at every start
([Worker hives in Kubernetes](#worker-hives-in-kubernetes), step 5), so the chart value is the cap a worker pod
keeps. `agentihooks hive set <id> max-agents=<n>` changes a pod's cap at once, and `max-agents=0` stops placement on
it, but either lasts only until the pod next starts; hold a hive at a stage by its `maxAgents` value. A hive outside
the chart, such as the workstation, keeps a hand-set cap: `agentihooks hive set <workstation id> max-agents=8` holds
it at eight agents while the worker hives carry the rest.

### Scaling refusals

| Render fails with | Cause | Fix |
|---|---|---|
| `workers.replicas above 1 is refused: one hive credential is one hive, so every replica would report the same hive id and agent limit; install one release per hive credential to scale` | `workers.replicas` is 2 or more. | Set `workers.replicas: 1` and add a `workers.hives` entry, or a release, per extra credential. |
| `workers.hives <name> needs its own credentialSecret: one credential is one hive` | The entry has no `credentialSecret.name`, or its Secret name is already used by the base hive (when `workers.replicas` is 1) or another entry. | Mint another member with `hive invite` and point the entry at its own Secret. Two keys of one Secret count as the same credential. |
| `workers.hives <name> sets replicas: each entry is one pod; remove the entry to stop it` | The entry sets `replicas`, even `1` or `0`. | Drop the key; remove the entry to stop that hive. |
| `workers.hives name "<name>" must be a lowercase DNS label of at most 20 characters` | The name has upper case, dots or underscores, starts or ends with a dash, or is longer than 20 characters. | Use lowercase letters, digits and inner dashes. |
| `workers.hives name <name> appears twice` | Two entries share a name. | Rename one. |
| `workers.hives <name> shares its home volume with another hive; give it its own homeVolume` | The entry's hostPath or claim home is the same path or claim as another hive's. | Give the entry its own `homeVolume`; `workers.node` alone does not clear this check, which runs on the inherited `homeVolume`. |
| `workers.maxAgents must be positive` | `workers.maxAgents`, or an entry's `maxAgents`, is 0 or below. | Set it to the number of agents the hive may run, 1 or more. |
| `workers.memoryPerAgentMi needs a positive request and a limit no lower than it` | `memoryPerAgentMi.request` is below 1, or `.limit` is below `.request`. An entry that sets only one of the two keys drops the other. | Set both keys, with `limit` at or above `request`. |
| `set worker hive memory with workers.memoryPerAgentMi, not workers.resources` | `workers.resources.requests.memory` or `.limits.memory` is set. | Remove the memory key from `resources` and size memory per agent with `memoryPerAgentMi`. |

If `agentihooks hive list` shows fewer hives than worker pods, two pods mount the same credential through a path the
chart does not check, such as two releases pointed at one Secret. Compare `credentialSecret.name` across the
releases and give each its own member.

### Release roll one hive at a time

`workers.rollout` makes a release roll take the hives down one at a time, each for a bounded drain, so the swarm keeps
every other hive serving while one restarts. It is off by default and is an ArgoCD feature: its hooks are ArgoCD sync
hooks.

```yaml
workers:
  rollout:
    enabled: true                       # default false
    namespace: default                  # where the shared Lease lives
    leaseName: agentihooks-hive-rollout # one Lease name for every hive release that must roll in turn
    drainSeconds: 1200                  # preStop drain while rollout is on, 0 to 1500
    release: ""                         # optional pod stamp; each new value rolls the hives
```

With `enabled: true` and at least one worker hive, the chart adds:

| Piece | What it does |
|---|---|
| PreSync Job `<fullname>-rollout-acquire` | Takes the Lease `<namespace>/<leaseName>` for the owner `<release namespace>/<release name>`. A Lease held by another owner and not expired is waited on, checked every 5 seconds, for up to 5400 seconds; the Job then fails with `another hive rollout owns the lease; no workload was changed` and the sync changes nothing. The Job sets the renew time once and nothing renews it, so a held Lease expires 3600 seconds after it was taken. |
| PostSync Job `<fullname>-rollout-release` | Frees the Lease only when every worker hive Deployment of the release is serving: replicas above 0, the current generation observed, and updated, ready and available replicas all equal to replicas. Otherwise it fails with `replacement hive is not serving; rollout ownership retained` and retries, up to 3 times. When it still fails, the sync fails. |
| SyncFail Job `<fullname>-rollout-abandon` | Frees the Lease after a failed sync, a failed PostSync release included, when this release still holds it. The next sync's PreSync takes it again. |
| Readiness probe | `python3 -m scripts.hive.rollout ready` every 5 seconds: ready while the marker `HIVE_READY_FILE` (`/tmp/hive-ready`) was touched in the last 60 seconds. The start script removes the marker, and `hive run` touches it after each successful heartbeat, so a pod is ready from its first beat and turns unready after 60 seconds without one. |
| Bounded drain | The preStop drain waits `workers.rollout.drainSeconds` instead of `workers.drainSeconds`, and the grace period is that value plus 30 seconds. A value outside 0 to 1500 fails the render with `workers.rollout.drainSeconds must be between 0 and 1500`. |
| Sync waves | Each worker hive Deployment of the release gets its own sync wave: wave 1 for the first worker hive, then one wave per further `workers.hives` entry, so ArgoCD rolls them in order inside one sync. |
| RBAC | A ServiceAccount `<fullname>-rollout`, a Role in the Lease namespace that may create Leases and get and update the named one, and a Role that may get the release's worker hive Deployments. |
| `release` stamp | Written to the pod annotation `agentihooks.io/release`; changing it changes the pod template and rolls the hives through the Lease. When it is empty, a chart whose `appVersion` is a release version stamps that `appVersion` on each worker hive whose `workers.image.tag` is `release`, so every release chart rolls those hives once. |

Each hook Job has 5500 seconds in total, its retries included, which holds the 5400 second acquire wait. The hook Jobs
run the hive worker image at the chart's `appVersion` tag. A chart whose `appVersion` is neither a
release version such as `2.17.467` nor `dev` fails the render; at `dev` the hooks use `workers.image.tag`.

Hives in separate releases roll in turn when they share one `namespace` and `leaseName`: the second release's
PreSync waits until the first release's PostSync finds its hive serving and frees the Lease. The hive drained during
its turn sends each agent one inbox order to hand off, so successors start on the hives that are still serving.

#### Antoncore hives

The antoncore swarm hives are one ArgoCD Application each, `swarm-hive-001` and `swarm-hive-003`, with values in
`k8s/values-overlays/<hive>/values.yaml`. Each takes the `agentihooks-swarm` chart from
`ghcr.io/the-cloud-clockwork/charts`, runs the hive worker image at tag `release` with
`pullPolicy: Always`, and sets `workers.rollout.enabled: true` (the chart default is false), keeping the other
rollout defaults:

```yaml
workers:
  image:
    tag: release
    pullPolicy: Always
  rollout:
    enabled: true
```

The release hive, `swarm-hive-001`, follows `targetRevision: '*'` and rolls at most once per automated sync window
onto the newest chart. Manual syncs remain allowed. The stable hive, `swarm-hive-003`, pins its chart and keeps its
daily manual drain then chart bump.

ArgoCD syncs the release hive inside its automated sync window and the stable hive after its manual bump. Pod
templates carry no chart version label
([#10072](https://github.com/the-cloud-clockwork/agentihooks/pull/10072)), but each leased worker hive on the `release`
tag carries the synced chart's `appVersion` as its `release` stamp, so each newly synced chart changes it once and rolls the hive
([#12360](https://github.com/the-cloud-clockwork/agentihooks/issues/12360)). A rolled pod pulls the current `:release`
image. Each roll goes through the shared Lease `default/agentihooks-hive-rollout`, one hive at a time. The overlays
set no `release` stamp and the Applications carry no image updater annotations, so no second writer changes the
template after the chart moved; antoncore's `test_hive_release_roll` guards that single writer behavior.

Antoncore [#1474](https://github.com/the-cloud-clockwork/antoncore/pull/1474) first wrote each release's `:release`
digest into `workers.rollout.release` through the ArgoCD image updater. On release v2.17.472 that rolled every hive
twice, once for the chart and once for the digest stamp, and
[#1500](https://github.com/the-cloud-clockwork/antoncore/pull/1500) removed the image updater from the hive
Applications.

The roll on release v2.17.472, across the three hives antoncore ran then and sampled every 15 seconds, took each hive
from down to serving again in 53 seconds to 6 minutes 42 seconds, and no sample showed more than one hive pod in a
state other than Running or Terminating. 17 of its 104 samples showed two hives not ready. In those samples one
replacement pod failed readiness on Redis socket timeouts. A replacement pod that cannot reach Redis stays unready,
because its readiness marker ages past 60 seconds. If its release Job then fails, the abandon Job frees the Lease and
the next hive can take its turn while that hive is still down.

Watch a roll:

```bash
kubectl get lease agentihooks-hive-rollout -n default -o jsonpath='{.spec.holderIdentity} {.spec.renewTime}{"\n"}'
for n in 001 003; do
  kubectl -n swarm-hive-$n get pods -l app.kubernetes.io/component=worker-hive \
    -o jsonpath='{range .items[*]}{.metadata.name} {.status.phase} {.status.containerStatuses[0].ready}{"\n"}{end}'
done
kubectl -n swarm-hive-001 get jobs | grep rollout
```

An empty holder means no roll is in progress. A holder naming `swarm-hive-<n>/swarm-hive-<n>` means that hive has its
turn.

#### Release roll troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A sync fails at PreSync with `another hive rollout owns the lease; no workload was changed` | Another release held the Lease for the whole 5400 second wait. | Read the holder with the `kubectl get lease` command above and fix that release's roll; the next sync retries. |
| A hive's `rollout-release` Job fails with `replacement hive is not serving; rollout ownership retained` and its sync fails | The replacement hive pod is not ready: no successful heartbeat in the last 60 seconds, often Redis unreachable from the pod. The abandon Job frees the Lease, so another hive may roll while this one is down. | Read the pod log for `hive Redis connection lost` (see [Redis restarts](#redis-restarts)) and fix the cause; the next sync retakes the Lease. |
| The render fails with `workers.rollout.drainSeconds must be between 0 and 1500` | The value is negative or above 1500. | Set a value from 0 to 1500. |
| The render fails with `appVersion must be a release version such as 2.17.467, or dev for an unreleased chart` | The chart's `appVersion` is neither form. | Package the chart with a release version, or keep `dev`. |
| Each release rolls a hive twice | A second writer changes the pod template after the chart sync, such as an image updater stamping `workers.rollout.release`. | Remove the second writer, so one sync carries every template change of the release. |

## Ledger Hives panel

The Swarm tab of the ledger page shows a Hives panel after Quota. It reads the hive settings API, edits hive settings
in place and moves the master to another hive.

### Panel

| Column | Shows |
|---|---|
| hive | The hive's name; hovering shows its id. |
| state | `live` while the last heartbeat is under ninety seconds old, else `quiet`. |
| ui, ephemeral | `yes` or `no`, editable. |
| excluded | Comma list, editable. |
| prefer | Comma list of `role:rank`, editable. |
| max agents | The hive's agent ceiling, editable, marked `cap` when the operator set a cap and `auto` when it is computed from the hive's own resources; empty with an `unknown` hint when there is no cap and no fresh reading. |
| agents | Working agents against max agents, such as `1/3`: the worker sessions the hive reported at its latest heartbeat, named by the row's `agents_at`, without its interactive master sessions; a row that is not live counts none. |
| slots | One line per slot: harness, account and the sessions the hive reports on it at its latest heartbeat. |
| master | The live master running on the hive, from any swarm, or `—`. |
| (no header) | The move master button, which moves this ledger's master to the hive. |

Placement compares its own load with max agents: the worker sessions the hive reports plus this runtime's
launching, working or suspect non-master agents on the hive that the hive has not reported yet. The panel shows only
what the latest heartbeat reported.

- The header count reads `<live> live · <n> hives`, or `—` when the read failed.
- The Quota header count ends with each hive's `<name> <agents>/<max agents>`, such as
  `2 accounts · probed 40s ago · laptop 1/3 · pod-a 0/4`.
- The panel reads `GET /api/v1/hives` at most every fifteen seconds while the Swarm tab is open. An empty table reads
  `Reading hives…` before the first answer, `No hives registered.` when the registry is empty, and
  `Could not read hives.` when the read failed.
- Swarm tab redraws skip the table while one of its fields holds focus; each fresh read still redraws it.
- The panel folds and keeps its folded state, its height and its column widths across reloads, like the other Swarm
  tab panels.

The panel lists no ephemeral hive that has been quiet for an hour.

### Quiet hives

A hive is quiet once its last heartbeat is older than an hour (`registry.QUIET_MS`).

- The `hive sweep` tick step marks a quiet ephemeral hive, other than one with `ui=yes`, draining, and deletes its
  registry records through the hive remove path once no agent record still runs on it.
- A quiet hive with `ephemeral=no` raises one `quiet hive` health finding in `agentihooks swarm <slug> status`.
- `scripts/packaging/hive-reproduce.sh` runs `agentihooks hive remove` on the member it invited when it exits;
  `HIVE_KEEP=yes` keeps it. When that remove fails, the script prints the remove command to run again; the sweep
  removes the hive only once its worker pod stops sending heartbeats.
- A hive that never sent a heartbeat carries no time to age from, so it is neither swept nor reported.

### Editing a hive

Change a field and leave it, or pick a new `yes` or `no`: the page sends that one field as
`PATCH /api/v1/hives/<id>` with the ledger's credential and its slug. The note line shows the write pending, then
done, or the server's error message, such as `role qa needs a UI, and this hive has ui=no`. The row then shows the
hive as the server stored it.

The page converts what was typed before it sends it:

| Field | Typed | Sent |
|---|---|---|
| excluded | `master, qa` | `["master", "qa"]` |
| prefer | `master:1,qa:2` | `{"master": 1, "qa": 2}` |
| max agents | `4` | `4`; an empty box sends `auto`, anything else other than digits is sent as text and refused |

Only the operator can edit. An agent's credential gets
`403 Hive settings need the operator`.

### Move master

The button sends the swarm control `master_move` with the hive id. The server runs
`agentihooks swarm <slug> master move --to <hive>`, so the move behaves as described in
[Workstation fallback](#workstation-fallback) and in the [swarm commands](swarm.md): the live master hands off and its
successor starts on that hive.

The button is off when:

- the Swarm tab has no swarm status yet, or another swarm control is still pending;
- the hive is quiet;
- this ledger's master already runs on that hive.

The command can still refuse a hive the button allows, such as one placement would not give the master; the
control then answers `502 request_refused` and the note line shows the server's answer.

```bash
curl -X PUT "http://127.0.0.1:8765/api/v1/ledgers/$SLUG/swarm/controls" \
  -H "Content-Type: application/json" -H "X-Ledger-Token: $LEDGER_TOKEN" \
  -d '{"action": "master_move", "hive": "workstation"}'
```

A hive id must start with a letter or digit and hold only letters, digits, dots, dashes or underscores, at most 63
characters; any other answers `400 request_refused` with `A master move needs a hive id`. A control from an agent's
credential answers `403 forbidden` with `swarm controls need the operator`.

### Hive settings API

| Method and path | Who | Answer |
|---|---|---|
| `GET /api/v1/hives` | no ledger credential needed | Every registered hive. |
| `GET /api/v1/hives/<id>` | no ledger credential needed | One hive. |
| `PATCH /api/v1/hives/<id>` | the operator | The hive after the change. |

Every answer is `{"data": ..., "revision": "<sha256 of data>"}`. A hive carries its stored record (`id`, `name`,
`ui`, `ephemeral`, `draining`, `excluded`, `master_swarms`, `prefer`, `max_agents`, `capabilities`, `harnesses`,
`slots`, `interactive`, `repos`, `heartbeat_at`, `version`) and three computed fields:

- `live`: `true` while the heartbeat is under ninety seconds old and the last finished beat under five minutes old.
- `agents`: the placement load described under [Panel](#panel).
- `master`: `{"slug", "name"}` of the live master on the hive, or `null`.

Each slot also gains `agents`, the sessions the hive reports for that harness and account.

```bash
curl -s http://127.0.0.1:8765/api/v1/hives/pod-a
```

```json
{
  "data": {
    "id": "pod-a", "name": "pod-a", "ui": "no", "ephemeral": "yes",
    "excluded": ["master"], "prefer": {}, "max_agents": 4, "agents": 1,
    "live": true, "master": null,
    "slots": [{"harness": "claude", "account": "work", "kind": "subscription", "cap": 2, "provider": "", "agents": 1}],
    "...": "..."
  },
  "revision": "9f2c..."
}
```

The PATCH body takes one or more of these fields and nothing else:

| Field | Type |
|---|---|
| `name` | string, 1 to 200 characters |
| `ui`, `ephemeral` | `"yes"` or `"no"` |
| `excluded` | array of lane role or profile names, each starting with a letter or digit followed by letters, digits, `.`, `_` or `-`; the server joins the array with commas before parsing, so an entry holding a comma becomes two names and padding spaces are dropped; `[]` excludes nothing |
| `prefer` | object of role to whole number rank |
| `max_agents` | integer, 0 or more, or `"auto"` to clear the cap |

The server checks the body against that schema, then applies it with the same rules as `agentihooks hive set`, so a
malformed `excluded` name, a `prefer` role the hive excludes, a rank below 1 or a negative max agents is refused.
The retired `roles` field is an unknown field and is refused. `master-swarms` and `capabilities` are set only with
`agentihooks hive set`. The credential is the ledger token in `X-Ledger-Token` and the ledger it belongs to in
`X-Ledger-Slug`.

```bash
curl -X PATCH http://127.0.0.1:8765/api/v1/hives/pod-a \
  -H "Content-Type: application/json" \
  -H "X-Ledger-Slug: $SLUG" -H "X-Ledger-Token: $LEDGER_TOKEN" \
  -d '{"excluded": ["master", "qa", "frontend"], "max_agents": 12}'
```

| Status | Code | When |
|---|---|---|
| 400 | `schema_invalid` | The body has no field, an unknown field or a wrong type (`Request does not match the resource schema`), or the hive registry refuses the value and names why. |
| 403 | `forbidden` | `Missing or wrong ledger credential`: no `X-Ledger-Slug`, an unknown ledger or a wrong token. `Hive settings need the operator`: the credential is an agent's. |
| 404 | `resource_missing` | `No such hive`. A PATCH never creates a hive; register one with `agentihooks hive add`. |
| 405 | `method_not_allowed` | Anything but GET on the list, or GET and PATCH on one hive. |
| 503 | `storage_error` | Redis is unreachable or a hive record is unreadable. |

### Limitations

- Masters do not count in the agents figure, matching max agents.
- Settings the hive reports itself (`harnesses`, `slots`, `interactive`, `repos`, `heartbeat_at`, `version`)
  cannot be changed through the API.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Could not read hives.` | The server answered an error, usually 503 with Redis down. | `curl` the list to read the error, then restore Redis. |
| An edit shows `Hive settings need the operator` | The page was opened with an agent's credential. | Open the ledger with the operator's credential. |
| An edit shows `prefer names qa, a role this hive excludes` | `prefer` named a role in the hive's `excluded` list. | Remove the role from `excluded`, or drop it from `prefer`. |
| move master stays off | The hive is quiet, the master is already there, or a control is pending. | Wait for the hive's heartbeat or the pending control. |
| A hive stays `quiet` | `hive run` is not publishing. | `systemctl --user status agentihooks-hive` on that machine. |

## Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| A launch waits with "no hive for `<role>` lists the repo `<repo>`" | `agentihooks hive show <id>`, the `repos` list | Clone the repo under `HIVE_REPO_ROOT` with an `origin` remote matching the swarm's repo name, or point `HIVE_REPO_ROOT` at the clones. |
| A launch waits with "no live hive takes" a role | `agentihooks hive list`: `stale` hives, roles | Restart `agentihooks hive run` (`agentihooks hive install` keeps it running), or add the role with `agentihooks hive set`. |
| A master launch on a hive reports "account reservation is full" | `agentihooks hive show <id>`: the slot's `cap` for the account | For zero capacity, check quota freshness, account access, quota left and the ceiling under [automatic quota refresh](#automatic-quota-refresh). Otherwise end a session on that account or wait for its five hour window to recover. |

## Related pages

- [Deployment modes](../reference/deployment-modes.md): where the controller, ledger and Redis run, and the
  regulated-site recipe.
- [Swarm](swarm.md): the master, seats, the minute tick and the swarm commands.
- [Claude account load balancing](load-balancing.md): subscription and api slots.
- [Hosted ledger auth](../reference/hosted-ledger-auth.md): hive tokens and edge mode.
- [Hive enablement record](../reference/hive-enablement-record.md): the first Anton worker hive join, where its
  time went, and the proposals that shaped this page.
