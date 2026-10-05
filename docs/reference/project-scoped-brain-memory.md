---
title: Project scoped brain memory
parent: Reference
nav_order: 7
---

# Project scoped brain memory: research and design

Research for agentihooks issue #450 (swarm task pm1, 2026-10-05). The operator's
problem: every session in every repo gets the same brain feed (hot arcs, recent
lessons, signals and one fleet wide guess at what the operator is doing), so a session in
one repo gets other repos' work and the brain reads as a mix of everything. The goal is
per project memory. A session opened in a repo or agent folder should know what was being
worked on there and that project's lessons and decisions, and get nothing unrelated.

This document answers the research questions with evidence and specifies the agentihooks
change (swarm task pm2). The last section is the text of the issue for the brain kernel
repo (swarm task pm3).

## Verdict

- **No feed entry carries a project.** Arcs do have a `project` field in their own
  frontmatter, but nothing that writes the feed copies it, and `/feed` takes no
  parameters. Lessons, signals, decisions and intent carry no project at all.
- **The arc `project` field is often wrong or missing.** It is the basename of the first
  session's working folder (`Path(cwd).name`), not the repo. Among 14 arcs whose true
  repo could be checked, 3 were wrong or missing (21%). The two wrong ones came from a
  subfolder and a scratch folder; worktrees hit the same rule.
- **agentihooks drops identity on the write path too.** A marker POSTed straight to
  `/marker` carries only `session_id` and `source`. The working folder never leaves the
  machine.
- **A session knows its identity exactly at hook time.** The hook payload `cwd`, git's
  common dir (which resolves a worktree to its repo), the swarm environment and the swarm
  config's `repo` are all available. The identity problem is on the brain side, not the
  session side.
- **agentihooks can scope today's data on its own,** with limits: it can filter and rank
  what it is fed and inject a per project block per session. It cannot fix what the
  kernel never recorded. A proper project field on every entry, per project intent and a
  per project feed need the kernel.
- **OpenRig keys memory by seat, not by project.** Its project catalog resolves a project
  from the working folder, but projects carry context and skills, not lessons. The part
  worth borrowing is the project resolution order, not a memory model.

## Q1. What project identity brain entries carry today

### The feed agentihooks reads

`HttpBrainSource.fetch` in the agentihooks brain adapter calls `GET /feed` with no
arguments. It flattens `hot_arcs`, `inject_blocks` and `entries` into `BrainEntry(id,
title, content, priority, ttl, severity, metadata)`. `_publish_entries` then reconciles
them into the shared broadcast channel `brain`. That channel is one fleet wide message
set. Whichever session triggers a refresh (at SessionStart, or every
`BRAIN_REFRESH_TOOL_CALLS` tool calls) republishes it for every session subscribed to
`brain`.

A live `brain_feed` call on 2026-10-05 at 12:17 UTC returned 6 entries. In every one,
`metadata` held only `id, title, priority, ttl, severity`.

| Feed entry | Kernel writer | What it contains | Identity carried |
|---|---|---|---|
| `hot-arcs-<date>` (prio 10) | `brain_keeper.write_hot_arcs_md` | A markdown table: Arc ID, Heat, Region, Status, What it is | None. The arc's own `project` is not rendered. |
| `inject` (prio 9) | `brain_keeper.write_inject_feed` | `**[target]** content` | The marker's `target` attribute (default `all`) |
| `signals` (prio 8) | `brain_keeper.write_signals_feed` | `- **[sev]** (source) text` | `source` is an agent name, not a repo |
| `operator-intent` (prio 7) | `brain_apply.write_intent` | One LLM paragraph, "What the operator is likely doing" | None. One fleet wide value per tick. |
| `lessons` (prio 7) | `brain_keeper.write_lessons_feed` | The newest 6 lessons fleet wide, date and body only | None. The header holding source and session id is discarded. |
| `last-tick-diff` (prio 5) | `brain_apply.generate_diff_report` | Edge, signal and merge counts, health score | None |
| `amygdala-active` (prio 100) | `amygdala.py` | Event-bus incident | Stream, event and source names |

Evidence:
- `services/brain-api/app/feed.py:feed_payload` buckets entries by matching their id and
  copies the feed file's flat frontmatter into `metadata`.
- `brain-api main.py:feed` is `def feed(_: None = Depends(require_token))`, with no query
  parameters.
- The MCP `brain_feed()` takes no arguments.

### Arcs

Arc frontmatter is written by `cluster.py:write_stub`: `cluster_id, title, region, status,
heat, source_sessions, project, created, signals, edges, synthesized`. Later passes add
`summary` and change `heat`, `region` and `status`. A `vault_read` of the arc
`2026-10-04-you-are-swarm-buildout-ci-12-…` shows `project: agentihooks` beside 65
`source_sessions`.

The project is recorded, but it is lost downstream:
- The hot arcs table does not render it.
- `embed_arcs.py` indexes arcs with metadata `region, heat, status, title, summary, path`
  only. A `brain_get_arc` call returns exactly those fields.
- `brain_apply.apply_merges` keeps arc A's frontmatter, so arc B's project is dropped.

Two other arc producers carry no real project: `amygdala.write_incident_arc` writes none,
and `reasoner_feedback.py` hardcodes `project: brain-keeper`.

### Lessons, signals, decisions, milestones

Markers are captured by agentihooks, not by the kernel. `brain_writer_hook.write_markers`
runs on Stop and SubagentStop, scans the transcript and POSTs each marker to `/marker`.
- **Direct path.** `_marker_request` sets only `attrs.session_id` and `attrs.source`
  (`AGENTICORE_AGENT_NAME` or `agent`).
- **Outbox path.** `_write_to_outbox` adds `project` from `CLAUDE_PROJECT_DIR`. The
  kernel's `outbox_drain.py` forwards it, but agentihooks' own `_drain_outbox` rebuilds
  the body without it.

Either way, `brain-api markers.py:write_marker` never reads `attrs.project`:
- **Lesson.** Goes to the shared daily `left/reference/lessons-<date>.md`, with the header
  `## <ts> — <source> — <session_id>`. A `vault_read` of `lessons-2026-10-05.md` confirms
  this.
- **Signal.** Becomes `amygdala/<stamp>-<sev>-<slug>.md` with `id, title, severity,
  source, created`.
- **Decision.** Becomes `left/decisions/ADR-NNNN-slug.md` with `id, title, created,
  source, status`.
- **Milestone.** Goes to `left/projects/<slug(source)>/BLOCKS.md` when that folder exists,
  otherwise to the daily note. It is keyed on the agent name, not the project.

The only path from a lesson back to a project is its `session_id`, and that path runs
through the arc whose `source_sessions` lists the session.

### Operator intent

`brain_tick.run_tick` phase 3 makes one LLM call over the whole vault: up to 250 arcs by
heat, edges, all signals, 10 sampled lessons and the previous intent, capped at 12,000
characters. `write_intent` writes the single file `brain-feed/intent.md` (`id:
operator-intent`). The prompt asks the model to "name the actual projects and systems
involved", but the output is one value for the whole fleet.

The live file on 2026-10-05 also ends mid sentence ("…live proof of the agentihooks inbox
system ("), which is a separate kernel defect.

### Search

None of the search surfaces can scope results to one repo:
- `kb_search`, `brain_search_arcs` and the embeddings `/search` filter only on `producer`
  (the content type, such as `brain-arc` or `brain-lesson`).
- `/vault/search` is a case-insensitive token match over files under a folder prefix. It
  returns at most 3 hit lines per file, so it is not a field filter.

`init.sql` already has a GIN index on `content_embeddings.metadata` that no query uses.

## Q2. What identity a session has at hook time

| Identity | Source at hook time | Example (this session) |
|---|---|---|
| Working folder | Hook payload `cwd`, on every event. `hook_manager` already passes `payload.get("cwd")` to enforcements, conditions, the inbox and auto-dev. | `/home/iamroot/dev/tcc-ecosystem/agentihooks` |
| Git repo | `git rev-parse --show-toplevel` for the checkout. `--git-common-dir` resolves a linked worktree to its repo's `.git`. Helpers already exist: `conditions.repo_root`, `project_bridge._repo_root` and `_git_dir`, and `serena_router.binding`. | Repo `agentihooks`. A worktree `~/dev/worktrees/agentihooks/<name>` resolves to the same repo through the common dir. |
| Worktree | Toplevel differs from the common dir's parent | `rig-grade-swarm-eng-46-pm1` |
| Remote | `git remote get-url origin` | `the-cloud-clockwork/agentihooks` |
| Swarm, task, seat | Env `AGENTIHOOKS_SWARM`, `AGENTIHOOKS_SWARM_TASK`, `AGENTIHOOKS_SWARM_LANE`, `AGENTIHOOKS_AGENT_NAME`. The swarm config records `repo` (`swarm.store`, set by `swarm create --repo`). | `rig-grade-swarm`, `pm1`, `eng`, `rig-grade-swarm-eng-46` |
| Profile | agentihooks state, the active profile chain | `anton` |
| Session id | Hook payload `session_id`. Claude Code stores the transcript under `~/.claude/projects/<cwd with / and . as ->/<session_id>.jsonl`. | |

The working folder is the strongest key. It is exact, present on every event, and maps to
a repo through git with no guessing. A swarm session can also be keyed by its swarm's repo,
and an agent folder by its own path.

There are sessions with no project: the home folder, scratch folders, and `~/dev/tcc-ecosystem`
itself (which is not a git repo).

The scope of the problem right now: `~/.agentihooks/active-sessions.json` at about 12:25
UTC showed 19 live sessions across 3 repos (agentihooks-bundle 8, agentihooks 8,
openrig 3). All 19 received the same brain channel.

## Q3. How arcs get their project today, and how often it is missing or wrong

Derivation, verified in the kernel source:
1. `extract.py:process_session` reads each Claude transcript under the mounted
   `~/.claude/projects`. It keeps the first `cwd` and `gitBranch` it sees, and sets
   `project = project_name(cwd)`, which is `Path(cwd).name or "unknown"`.
2. `cluster.py:group_sessions` sorts sessions by (project, start). It starts a new
   candidate when the project changes, the gap exceeds 120 minutes, or a compaction
   occurs.
3. `cluster.py:write_stub` writes `project: {group[0].get('project', 'unknown')}`, the
   first session's value.

Consequences, read from the code:
- **Worktree sessions get the worktree name.** `~/dev/worktrees/agentihooks/fix-x`
  becomes `fix-x`.
- **Subfolder sessions get the subfolder name.** A session started in
  `profiles/package` becomes `package`. A `kb_search` hit shows an arc with
  `project: package`.
- **Two repos with the same basename collide,** and the remote is never consulted.
- **Merging drops the merged arc's project.** `apply_merges` keeps only arc A's
  frontmatter.
- **Codex sessions never become arcs.** `extract.py` reads only the Claude projects
  folder.

Measured on a sample of live vault arcs against the true repo of their source sessions:

A sub-agent sampled 53 arc files with `vault_read` across `frontal-lobe/conscious`,
`frontal-lobe/unconscious`, `clusters` and `pineal`, dated 2026-04 to 2026-10-05. For each
arc it compared the `project` field with the true repo of its first source sessions. The
true repo came from the session's transcript folder under `~/.claude/projects`, which
encodes the working folder.

| Population | Arcs | Result |
|---|---|---|
| Knowledge arcs (references, ADRs, URL and repo scrapes) | 17 | No `project` and no source sessions, by design |
| Old "writer" format session arcs | 4 | No `project` field at all (the format predates it) |
| Current format session arcs, true repo found locally | 14 | **11 correct, 2 wrong, 1 missing: 21% wrong or missing** |
| Current format session arcs, transcript no longer on disk | 18 | Unverifiable. Local transcripts older than about 2026-09-18 are gone. |

The two wrong ones:
- `package` for a session in `agentihub/agents/video_analizer/package`; the true repo is
  agentihub.
- `m5-live-proof` for a session in `~/scratchpad/agentihooks/m5-live-proof`; the true
  owner is agentihooks. This arc is in today's hot arcs.

The missing one is a 14-tool-call agentihooks session whose arc has no `project` line.

Distinct values seen: `tcc-qitp`, `antoncore`, `agentihooks`, `agentihooks-bundle`,
`agentibridge`, `agenticore`, `tcc-toolbelt`, `package`, `m5-live-proof`.

The sample held no worktree session, so the worktree case is shown by the code (the
basename rule), not observed. Swarm agents come out correct because the swarm launches them
in the primary checkout (`swarm.runtime`, `init-agent --dir config.repo`), and the first
`cwd` is what counts.

The same sample shows how mixed the feed is. Of today's 10 hot arcs, the 5 with resolved
projects are 3 tcc-qitp and 2 agentihooks, and one more is labelled `m5-live-proof`. So a
session in openrig, which has no hot arc at all, was injected work from two other repos.

## Q4. What agentihooks can do alone, and what needs the kernel

| Capability | agentihooks alone | Needs the kernel |
|---|---|---|
| Know the session's project | Yes. Exact, from the cwd, the git common dir and the swarm repo. | No |
| Hot arcs of this project | Partly. For each of the 10 hot arcs the feed names, read the arc's `project` with `/vault/read` and keep the ones that match. Arcs outside the fleet's top 10 are invisible. Worktree-named arcs need an alias list. | A `project` that resolves to the repo on every arc, and a per project top N |
| Project lessons | Partly. Read recent `lessons-<date>.md` logs (14 days, one `/vault/read` each), split them by header, and attribute each lesson by its `session_id` through a local session to project index that agentihooks records at SessionStart. Works for sessions recorded on this machine since the index shipped. | `project` on every lesson, kept from the marker attributes |
| Project decisions | No: ADR files carry no session id | `project` in decision frontmatter |
| Per project intent | A derived line, not an inferred one: the project's hottest arc title or summary, and for a swarm session the ledger overview. The fleet intent is withheld unless it names the repo. | An LLM intent per active project |
| Signals | Keep fleet wide. A warning about shared infrastructure must reach everyone. | `project` on signals, so project signals can rank first |
| Search at session start | Weak. `/vault/search` is token matching with 3 snippets per file, so a hit for the repo name is not a field match. Each hit has to be confirmed by reading its frontmatter, which costs one read per hit. | A `project` metadata filter on `/search`, `kb_search` and `brain_search_arcs`, using the existing GIN index |
| Record identity on new markers | Yes. Send `project`, `repo`, `worktree` and `cwd` on both the direct and outbox paths. The kernel ignores them today, but they cost nothing and are ready when it reads them. | Store them |
| One feed per project | No. The feed is one global payload. | `GET /feed?project=<repo>` |

What agentihooks cannot do alone: give a project memory the kernel never attributed.
Decisions, signals, lessons from before the local index existed, Codex work, and arcs that
never reach the fleet's top 10 all stay out of reach until the kernel records and serves a
project.

## Q5. OpenRig

OpenRig, checked out at `~/dev/tcc-ecosystem/openrig` and read for this research, has no
automatic memory store and does not mine transcripts. Its memory is hand-written markdown
in a topology tree keyed by seat. Under
`topology.root/rigs/<rig>/seats/<seat>/` it keeps `RECAP.md`, `LEARNED.md` and `lore/`,
with rig (`CULTURE.md`), pod and instance levels above. Seat memory is read only with an
explicit seat grant (`buildRebuildPrimingChain`, and the context-pack route with
`rig=&seat=`). It is fed in at handover, rebuild or compaction.

Projects are a separate catalog, `$OPENRIG_HOME/workspace/workspace.yaml`
(`projects: [{id, root, rigs}]`). `rig context work-install` picks a project in this
order:
1. an explicit `--project` id;
2. the only declared project;
3. the project that lists the seat's rig;
4. the deepest project root containing the working folder;
5. the only project with no rigs;
6. otherwise it refuses with `project_required`.

A project contributes context files and skills, not lessons. Its queue views filter on an
exact `project:<id>` tag and exclude unscoped rows ("another project's matching work ID is
never a fallback").

agentihooks already copied the seat half: seats, recaps, learned notes and culture (swarm
tasks s1, s2, g7). Two things are worth taking for this work:
- **The resolution order.** Explicit, then swarm, then the deepest root containing the
  cwd, then none. Our version uses the git common dir in place of a catalog.
- **The rule that unscoped rows never leak into a scoped view.** Project memory filters
  strictly, and fleet wide content is labelled as fleet wide.

## Recommended agentihooks change (spec for pm2)

### Behaviour

1. At SessionStart the brain adapter resolves the session's **project identity** from its
   working folder.
   - Inside a git checkout it uses the repo name of the common dir, so a worktree counts
     as its repo, plus the remote slug.
   - A swarm session (`AGENTIHOOKS_SWARM` set) uses its swarm's `repo`.
   - A task folder `~/scratchpad/<repo>/<task>` (the `agentihooks scratch` layout) uses
     `<repo>`.
   - Anywhere else there is no project.
2. A session with a project gets a **Project memory** block, injected for that session
   only (not through the shared channel), containing:
   - this project's hot arcs, hottest first, up to `BRAIN_HOT_ARCS_TOP_N`;
   - this project's recent lessons;
   - a one-line project focus: the hottest project arc's summary or title, its status and
     when it was last active, plus the swarm ledger overview for a swarm session.
3. For that session, the fleet wide **hot arcs, recent lessons and operator intent**
   entries are withheld. The operator intent is still delivered when it names the repo.
   **Signals, inject blocks, amygdala alerts and tick diffs** reach everyone as today.
4. A session with no project, or with project scoping off, gets today's feed unchanged.
5. Markers written by the brain writer carry `project`, `repo`, `worktree` and `cwd`
   attributes on both the direct and the outbox path.
6. `BRAIN_PROJECT_SCOPE` sets the behaviour:
   - `strict` (the default): withhold the fleet memory entries and inject project memory.
   - `rank`: inject project memory first, then the fleet entries as today.
   - `off`: today's behaviour.

### Seams (red first, one per slice)

| # | Seam | Contract | Red test that proves it |
|---|---|---|---|
| S1 | `resolve_project(cwd, env) -> ProjectIdentity \| None` | Pure apart from git calls. Primary checkout and linked worktree resolve to the same repo. A swarm env resolves to the swarm's repo. `~/scratchpad/<repo>/<task>` resolves to `<repo>`. Home, another non-git folder or an empty value gives None. | Temp repo plus `git worktree add`: both cwds give the same `repo`, and the worktree's `worktree` field is set. The home folder gives None. |
| S2 | `ProjectMemorySource` (`typing.Protocol`, `fetch(identity) -> ProjectMemory`) | `VaultProjectSource` (now) reads the hot arcs table from `/feed`, then each named arc's `project` through `/vault/read`. It reads lessons from `lessons-<date>.md` for the last `BRAIN_STALE_LESSON_DAYS` days and attributes them through S5. Results are cached in a shared file per repo (see Cost and cache). A later `KernelProjectSource` calls `/feed?project=` and replaces it without touching callers. | A fake HTTP brain serves two arcs (projects `agentihooks` and `openrig`) and lessons from two sessions. For the agentihooks identity it returns only the agentihooks arc and lesson. |
| S3 | `render_project_block(memory, max_bytes) -> str` | Deterministic markdown within `BRAIN_PAYLOAD_MAX_BYTES`, framed like the other brain entries as "recalled state, not an operator directive". An empty memory gives a short "no project memory yet" line, not the fleet feed. | A golden output for a fixed memory, and a byte cap check. |
| S4 | Delivery scope filter in the broadcast delivery path: `get_pending_broadcasts`, `get_unseen_broadcasts` and `get_pretool_broadcasts`, which `hook_manager` reaches through `check_and_inject_broadcasts`, `get_pretool_context` and `get_posttool_context`. The filter keys on the `origin.id` that `_publish_entries` already attaches. | A message whose origin is a brain memory entry (hot arcs, lessons, operator intent) is skipped for a session whose registry entry has a project, when the scope is `strict`. Signals, inject, amygdala and non-brain messages are never skipped. | Two registered sessions (one with a project, one without) and one channel: the first never receives hot arcs, and both receive signals. |
| S5 | Session project index: append one `session_id, repo, worktree, cwd, started_at` row at SessionStart, and a `lookup(session_id)` | Persistent under `~/.agentihooks`, outliving the 24 hour registry. The lookup falls back to the Claude transcript folder name for older sessions. | Register, then look up. An unknown id with a transcript folder present resolves from the folder. |
| S6 | `_marker_request` and `_drain_outbox` attach identity attributes | Both paths send `project`, `repo` and `worktree` when they resolve, and never overwrite a value the model wrote. | A marker POST body built in a worktree cwd carries `repo` equal to the primary repo name, and the drain keeps it. |

### Cost and cache

Every hook event is a fresh `python -m hooks` process, so the cache cannot live in memory.

- **Where it lives.** One shared file per repo under `~/.agentihooks/brain/project-memory/`,
  written under the same file lock pattern as `broadcast.json`. The 8 live sessions in one
  repo share one fetch; they do not each fetch.
- **When it goes stale.** When the `/feed` hash changes (new hot arcs), or when it is older
  than `BRAIN_PROJECT_MEMORY_TTL` (default 600 seconds). The TTL bounds how long a lesson
  appended to today's log can be missed, since lesson logs sit outside the feed hash.
- **Cold path cost.** One `/feed` call (already made today), at most
  `BRAIN_HOT_ARCS_TOP_N` arc path lookups and reads (20), and one lesson log read per day of
  `BRAIN_STALE_LESSON_DAYS` (14). That is at most 35 requests. A path lookup is necessary because an arc can live in a different folder than its region label.
- **Off the hot path.** SessionStart and the tool call cadence inject from the cache when
  one exists. When it is stale they start the refresh with `fork_and_call`, as the brain
  writer already does, so a hook never waits on the 34 extra requests. A session in a repo
  with no cache yet gets its block at the next refresh cadence.
- **Hot path cost.** No network call beyond today's `/feed`.

Wiring:
- `inject_on_session_start` and `maybe_refresh_on_tool_call` call S1, then S2, then S3,
  and return the block for the calling session only.
- `register_session` stores the S1 result in the registry entry so S4 can read it.
- Codex keeps receiving context through PostToolUse, as it does today.

### Done when (from pm2)

- Tests for S1 to S6 are green.
- The PR is merged into dev.
- Two live sessions, one in agentihooks and one in openrig, show different brain context
  at start, with both injections pasted as proof.

### Out of scope for agentihooks

These are listed in the kernel issue below:
- correcting wrong arc projects;
- per project LLM intent;
- a project field on decisions and signals;
- Codex extraction;
- project filters on search.

## Issue text for the brain kernel repo (pm3)

Title: **Project scoped memory: a project on every entry, per project intent, per project feed**

> **Problem.** Every agent session, in every repo, receives the same brain feed: the
> fleet's 10 hottest arcs, the newest 6 lessons, all signals and one fleet wide guess at
> what the operator is doing. A session in one repo gets other repos' work, and the brain
> reads as a mix of everything. Opening a project should bring that project's memory:
> what was being worked on there, its lessons and decisions, and nothing unrelated.
>
> **What agentihooks does (agentihooks #450 research, pm2 change).** It resolves each
> session's project from its working folder. A worktree counts as its repo through the
> git common dir, and a swarm session counts as its swarm's repo. agentihooks then:
> - injects a per session project memory block, built from the current feed, each hot
>   arc's own `project` frontmatter and the lesson logs, with lessons attributed by
>   session id;
> - withholds the fleet wide hot arcs, lessons and intent from sessions that have a
>   project, while signals, inject blocks and amygdala alerts still reach everyone;
> - sends `project`, `repo`, `worktree` and `cwd` on every marker.
>
> That is the limit of what a client can do. Memory the kernel never attributed stays out
> of reach.
>
> **What the kernel must add:**
> 1. **Correct project on arcs.** `extract.py:project_name` uses `Path(cwd).name`. In a
>    sample of 14 checkable arcs, 3 (21%) were wrong or missing: `package` for an
>    agentihub subfolder, `m5-live-proof` for a scratch folder, and one with no project.
>    Worktree sessions hit the same rule. Resolve the repo per session before
>    grouping, from the git common dir or the remote, and store `repo`, `cwd` and
>    `git_branch`. `group_sessions` already splits on project, so a cluster holds a single
>    value, and only per-session resolution fixes it. When `apply_merges` joins arcs from
>    different projects, keep both rather than arc A's alone. Backfill existing arcs.
> 2. **Keep project on markers.** `markers.py:write_marker` drops `attrs.project`. Write it
>    into the lesson header, signal and decision frontmatter and the milestone route, and
>    let the `/marker` body carry `repo` and `worktree`.
> 3. **Project in the search index.** Add `project` to the `embed_arcs.py` and lesson
>    metadata. Add a `project` filter to embeddings `/search`, `kb_search` and
>    `brain_search_arcs`. The GIN index on `metadata` already exists.
> 4. **Per project feed.** Add `GET /feed?project=<repo>` returning that project's top N
>    arcs, lessons, decisions and signals, plus the fleet wide signals. Render a project
>    column in the global hot arcs table now; it is a one-line change.
> 5. **Per project intent.** Replace the single `operator-intent` with one intent per
>    project active in the last 72 hours, for example one LLM call returning a map and
>    written to `brain-feed/projects/<repo>/intent.md`. Keep the fleet intent as the
>    no-project fallback. The current `intent.md` is also cut mid sentence.
> 6. **Codex sessions.** `extract.py` reads only the Claude projects folder, so Codex work
>    never becomes an arc.
>
> **Contract agentihooks will switch to.** Once item 4 ships, agentihooks replaces its
> client-side source with `/feed?project=` behind the same interface, and nothing else
> changes on the agent side.

## Sources

- **agentihooks:**
  - brain adapter: `HttpBrainSource`, `_publish_entries`, `maybe_refresh_on_tool_call`;
  - broadcast delivery: `get_pending_broadcasts`, `register_session`;
  - brain writer: `_marker_request`, `_write_to_outbox`, `_drain_outbox`;
  - git helpers: `conditions.repo_root`, `project_bridge`;
  - swarm config: `repo`;
  - `active-sessions.json`.
- **agentibrain-kernel:**
  - brain-api: `feed.py`, `main.py` (`/feed`, `/vault/search`), `markers.py`,
    `vault_reader.search_vault`;
  - brain-ops: `extract.py`, `cluster.py`, `brain_keeper.py`, `brain_apply.py`,
    `brain_tick_prompt.py`, `embed_arcs.py`, `outbox_drain.py`;
  - embeddings: `db.search`, `init.sql`;
  - `compose.yml`, plus the brain-ops Helm values.
- **Live brain** (MCP brain tools, 2026-10-05):
  - `brain_feed`;
  - `brain_get_arc` on two hot arcs;
  - `vault_read` of an arc, `lessons-2026-10-05.md`, `brain-feed/intent.md` and
    `brain-feed/README.md`;
  - `kb_search` and `brain_search_arcs` on project scoping, which surfaced the April
    decision that deferred a project identity resolver.
- **OpenRig:** `rebuild-priming-chain.ts`, `seat-recap-store.ts`, `cwd-resolution.ts`,
  `routes/context-packs.ts`, and the docs `project-workspace.md`, `lore-routing.md`,
  `instance-layout.md`, `knowledge-maturity.md` and `agent-startup-guide.md`.
