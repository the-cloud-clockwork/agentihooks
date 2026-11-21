---
title: Stored engine migration
parent: Reference
nav_order: 95
---

# Stored engine migration
{: .no_toc }

`python -m scripts.swarm.engine_migration` rewrites every stored swarm config so that its `engine` field reads
`events` and its `repo_origin` field holds a repository origin. It also deletes launch rows left behind by the
removed legacy launch path. Every swarm runs on the events engine (see
[Event driven engine](../pillars/swarm.md#event-driven-engine)). A config written before that change can still
carry `engine: legacy` or an empty `repo_origin` in Redis. This command moves those stored fields onto the events
engine.

1. TOC
{:toc}

---

## When to run it

Run it once against each swarm store that holds configs created before pull request #11141 made `events` the only
engine. Run it again after a swarm was reported as skipped and the cause is gone. It is safe to repeat: a swarm
already stored as `events` with an origin reports `unchanged`.

The command runs by hand only. No `agentihooks` subcommand, scheduled tick step, chart job or deploy hook calls it.

## Run it

From an agentihooks checkout, with the Python environment the swarm uses:

```bash
python -m scripts.swarm.engine_migration
```

The command takes no arguments. `--help` prints the usage line, and any other argument exits with status 2. It prints
one JSON object that maps each stored swarm id, in sorted order, to its outcome:

```json
{"proj-a": "migrated: github.com/example-org/proj-a", "proj-a-doctor": "migrated: github.com/example-org/proj-a: dropped 7 stuck launches", "proj-b": "unchanged", "proj-c": "skipped: live legacy agents"}
```

A second run over the same store, once the agents of `proj-c` have ended, prints:

```json
{"proj-a": "unchanged", "proj-a-doctor": "unchanged", "proj-b": "unchanged", "proj-c": "migrated: github.com/example-org/proj-c"}
```

### Which store it migrates

The command connects through `scripts.swarm.store.connect`, so it reads the same Redis settings as the tick and the
`agentihooks swarm` commands:

| Setting | Effect |
|---|---|
| `AGENTIHOOKS_HIVE_REDIS_URL` | Used when set, unless the process runs in local mode: `AGENTIHOOKS_DEPLOYMENT=local`, or neither `AGENTIHOOKS_DEPLOYMENT` nor `LEDGER_URL` set |
| `AGENTIHOOKS_SWARM_REDIS_URL` | Used otherwise |
| `redis://127.0.0.1:6379/0` | The default when neither URL applies; the database number is part of the URL |
| `AGENTIHOOKS_SWARM_KEY_PREFIX` | The key root, `agentihooks` by default; swarm keys start with `<root>:swarm:` |

The env files under `~/.agentihooks/` are loaded before the URL is chosen, so a value set there applies. Keep a URL
that carries a password in one of those files, never on the command line. The client connects with a 3 second
timeout and pings Redis before the first read. To migrate another store, point the setting at it for one run:

```bash
AGENTIHOOKS_SWARM_REDIS_URL=redis://127.0.0.1:6380/0 python -m scripts.swarm.engine_migration
```

## Outcomes

The command checks each swarm in this order and stops at the first match:

| Outcome | When | What it writes |
|---|---|---|
| `unchanged` | The stored `engine` field is `events` and `repo_origin` is not empty | Nothing |
| `unchanged: dropped N stuck launches` | As above, and the swarm has no agents but holds N stuck launch rows | Deletes those rows |
| `skipped: live legacy agents` | The stored `engine` field is not `events` and the swarm has agent records | Nothing; launch rows stay |
| `skipped: no repository origin` | No origin could be found (see [Repository origin](#repository-origin)) | Nothing; launch rows stay |
| `migrated: <origin>` | Otherwise | Sets `engine` to `events` and `repo_origin` to `<origin>` |
| `migrated: <origin>: dropped N stuck launches` | As above, and the swarm has no agents but held N stuck launch rows | Sets both fields, then deletes those rows |
| `skipped: <error>` | Loading or writing the config raised a swarm error | Nothing; launch rows stay |

- The busy check reads the stored `engine` field only. A swarm already stored as `events` with an empty origin is
  migrated even while its agents run, and its launch rows stay because it has agents.
- `skipped: no swarm <id>` means the swarm index lists an id whose config hash is gone. The other swarms still
  migrate.
- An update that `RedisStore.update` refuses, such as one with an invalid stored scaling value, reports
  `skipped: <refusal>`. The config is written before any row is deleted, so a refused swarm keeps every row.
- A migrated config is written through `RedisStore.update`, which rewrites every field of the config hash from the
  loaded config. Only `engine` and `repo_origin` change value.

## Repository origin

An origin is the lowercase `host/owner/name` of a Git remote, such as `github.com/example-org/proj-a`. The command
uses the first one it finds:

1. The swarm's stored `repo_origin`. A stored origin is never replaced.
2. For a Doctor swarm (template `doctor`), the origin of the swarm it watches, when that swarm is stored: its stored
   `repo_origin`, else the origin of its checkout.
3. The origin of the swarm's own checkout, the `repo` folder in its config.

A Doctor watches the swarm named by its peer record. Without a peer, it watches the stored swarm whose Doctor id
equals its own id. A Doctor id is the watched id with `-doctor` appended. An id that would pass 48 characters is cut
short and carries the first six hex digits of its SHA-256 before the suffix.

### Origin of a checkout

The command reads the `url` of `[remote "origin"]` from the checkout's Git config, following a worktree's `.git` file
to the shared Git folder. It drops the scheme, the user name, a trailing `.git` and a trailing slash, accepts `:` or
`/` after the host, and lowercases the result:

| Remote URL | Origin |
|---|---|
| `git@github.com:Example-Org/Proj-A.git` | `github.com/example-org/proj-a` |
| `https://github.com/example-org/proj-a` | `github.com/example-org/proj-a` |
| `ssh://git@git.example.com/team/tool.git` | `git.example.com/team/tool` |

A missing folder, a folder without Git config or a checkout without an `origin` remote gives no origin.

### Worked example

| Stored swarm | Template | Stored origin | Checkout origin | Result |
|---|---|---|---|---|
| `proj-a` | none | empty | `github.com/example-org/proj-a` | `migrated: github.com/example-org/proj-a` |
| `proj-a-doctor` | `doctor` | empty | `github.com/example-org/doctor` | `migrated: github.com/example-org/proj-a`, the watched origin |
| `proj-b-doctor` | `doctor` | `github.com/example-org/own` | any | `migrated: github.com/example-org/own`, the stored origin |
| `proj-c-doctor` | `doctor` | empty, and `proj-c` has no origin either | `github.com/example-org/doctor` | `migrated: github.com/example-org/doctor`, its own |
| `lost-doctor` | `doctor` | empty, watched swarm not stored | none | `skipped: no repository origin` |

## Stuck launch rows

The removed legacy launch path left two kinds of rows that nothing collects any more. When a swarm has no agent
records, the command deletes:

- the whole `<root>:swarm:<id>:launch-jobs` hash, whose agents are gone;
- the rows of `<root>:swarm:<id>:launches` whose JSON `state` is `acknowledging` or `launching`.

Rows in any other state, such as `started`, `waiting`, `pending` or `failed`, stay as launch history. The `N` in the
outcome is the number of launch job rows plus the number of deleted launch rows. A swarm with agents keeps all its
rows, and so does a skipped swarm.

The drop also runs for `unchanged` swarms. A swarm migrated earlier with stuck rows loses them on the next run and
reports plain `unchanged` after that.

## Legacy records on current releases

- `RedisStore.config` loads every stored config as `events`, whatever its `engine` field holds and when it has none.
  The command reads the raw `engine` field of the config hash instead, so a stored `legacy` value is still migrated
  although the loaded config reads `events`.
- Any write through `RedisStore.update`, such as a state change or a `swarm set`, rewrites the whole hash and so
  stores `engine: events`. A swarm written since may store `events` with an empty origin; the command fills its
  origin.
- `legacy` is refused everywhere: `agentihooks swarm <id> set engine=legacy` fails with `engine must be events`.
  There is no rollback to the legacy engine and no command that moves a swarm back.

## Doctor start

```bash
agentihooks doctor <id> start [--repo DIR] [--max-eng-agents N] [--max-ci-agents N] [--quote TEXT]
```

The first start of a Doctor creates the swarm `<id>-doctor` with template `doctor`. Its `repo` is `--repo`, else the
agentihooks checkout when it is a Git checkout, else the linked bundle folder. `swarm create` stores `engine: events`
and that checkout's origin. Start then sets the Doctor's origin to the watched swarm's origin when the watched swarm is
stored and has one, else to the Doctor's own. When neither exists, the config stays as created.

A later start of an existing Doctor does not change its engine or origin. To move an older Doctor, run the migration.

```bash
agentihooks doctor proj-a start
agentihooks doctor proj-a start --repo ~/dev/doctor-checkout --max-eng-agents 2 --max-ci-agents 1
```

The first form gives `proj-a-doctor` the origin of `proj-a`. In the second, when `proj-a` has no origin, the Doctor
takes the origin of `~/dev/doctor-checkout`. A watched id that has a ledger but no stored swarm is accepted, and the
Doctor takes its own origin.

## Checking a swarm

Read only. `agentihooks swarm` output reports the loaded engine, which is always `events`, so read the hash to see
what is stored:

```bash
P="${AGENTIHOOKS_SWARM_KEY_PREFIX:-agentihooks}:swarm"
URL="$AGENTIHOOKS_SWARM_REDIS_URL"
SLUG=proj-a-doctor
redis-cli -u "$URL" HMGET "$P:$SLUG:config" engine repo_origin template repo
redis-cli -u "$URL" HLEN "$P:$SLUG:launch-jobs"
redis-cli -u "$URL" HVALS "$P:$SLUG:launches"
redis-cli -u "$URL" SMEMBERS "$P:index"
```

On a hive outside local mode, use `AGENTIHOOKS_HIVE_REDIS_URL` when it is set. A migrated swarm answers `events` and a
non-empty origin. Never write these keys by hand.

## Limitations

- One store per run, the one the settings above choose.
- There is no dry run. Read the stored fields first with the commands above.
- A stored non-`events` swarm with agents stays skipped until its agents end; run the command again then.
- No command sets an origin by hand. A swarm whose checkout has no `origin` remote, and whose watched swarm has none
  when it is a Doctor, stays skipped.
- It writes only the config hash and the launch rows named above. Ledger data, bus streams and fencing state are
  untouched.

## Tests

`tests/swarm/test_engine_migration.py` and `tests/doctor/test_doctor_cli.py` cover the behavior under the
`engine-config-migration` feature map entry. The migration tests create configs and then overwrite the stored
`engine` field with `legacy`, the shape configs had before events became the only engine.

| Test | Checks |
|---|---|
| `test_every_stored_legacy_config_moves_to_events_with_an_origin_the_doctor_included` | Ordinary, Doctor, already migrated and stored origin swarms reach `events`, and a second run reports `unchanged` |
| `test_a_doctor_takes_the_origin_of_its_peer_and_a_watched_swarm_keeps_its_own` | A Doctor with a peer takes the peer's origin |
| `test_a_doctor_keeps_a_stored_origin` | A stored Doctor origin wins over the watched one |
| `test_a_swarm_without_any_origin_stays_legacy_and_is_reported` | No origin reports `skipped: no repository origin` and writes nothing |
| `test_a_doctor_takes_its_own_origin_when_the_watched_swarm_has_none` | The Doctor falls back to its own checkout |
| `test_a_swarm_with_legacy_agents_in_flight_is_left_for_the_master_with_its_launch_jobs` | Agents block the move and keep the launch jobs |
| `test_a_swarm_with_only_launch_jobs_whose_agents_are_gone_drops_them_and_migrates` | Launch jobs alone do not block the move and are dropped |
| `test_a_swarm_without_an_origin_keeps_its_launch_jobs_when_skipped` | A skipped swarm keeps its rows |
| `test_a_swarm_whose_config_update_is_refused_keeps_its_launch_jobs_and_launch_rows` | A refused update reports the refusal and keeps every row |
| `test_the_live_doctor_on_events_with_no_agents_drops_its_stuck_launch_rows_and_keeps_its_history` | Only `acknowledging` and `launching` rows go, and the count is in the outcome |
| `test_a_swarm_with_live_agents_keeps_its_launching_rows` | Agents keep the launch rows |
| `test_a_swarm_already_on_events_with_no_agents_drops_its_leftover_launch_jobs` | The drop runs for `unchanged` swarms |
| `test_a_legacy_swarm_with_no_agents_drops_its_stuck_launch_rows_when_it_migrates` | A migrated swarm drops its stuck rows |
| `test_a_stored_legacy_engine_with_an_origin_is_migrated_even_when_the_config_reads_events` | The raw stored field decides |
| `test_a_swarm_that_cannot_load_is_reported_and_the_rest_still_migrate` | `skipped: no swarm <id>`, and the rest migrate |
| `test_a_new_doctor_starts_on_events_with_the_watched_origin` | `start_on_events` takes the watched origin |
| `test_a_new_doctor_takes_its_own_origin_when_the_watched_swarm_has_none_or_is_not_stored` | `start_on_events` falls back to the Doctor's origin, or leaves the config alone |
| `test_main_migrates_the_connected_store_and_prints_each_outcome` | `main` prints the JSON outcome map |
| `test_start_creates_the_doctor_on_the_events_engine_with_the_watched_origin` | `doctor start` uses the watched origin |
| `test_start_puts_the_doctor_on_its_own_origin_when_the_watched_swarm_has_none` | `doctor start --repo` uses the Doctor checkout's origin |

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `Unreachable: Redis is unreachable (...); the swarm refuses to run without it` | The chosen Redis URL is wrong or the server is down | Check which URL the settings above select and that Redis answers; nothing was written |
| `skipped: live legacy agents` | The swarm stores a non-`events` engine and has agent records | Let the agents finish or retire them, then run the command again |
| `skipped: no repository origin` | No stored origin, no usable watched origin, and the checkout has no `origin` remote or its folder is gone | Add an `origin` remote to the swarm's checkout with `git remote add origin <url>`, then run again |
| `skipped: no swarm <id>` | The swarm index lists an id without a config hash | The rest migrated; the entry has no config to move |
| `skipped: <other message>` | `RedisStore.update` refused the rewritten config | Correct the setting the message names, then run again |
| `engine must be events` from `swarm set` | `legacy` or another value was set | Only `events` is accepted |
| `events engine requires a checkout with a Git origin or a stored repository origin` from `swarm set engine=events` | The swarm has no stored origin and its checkout has none | Add an `origin` remote to the checkout and set it again |
