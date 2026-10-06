# Real time delivery into agent sessions: channels against async rewake

Swarm task rt2, issue the-cloud-clockwork/agentihooks#903. Measured live on 2026-10-06, Claude Code 2.1.291 to 2.1.292 (it auto-updated during the run), Opus 5.5 high, codex-cli 0.160.0, local Redis 7.4.7, WSL2.

## Verdict

- **Default: a custom agentihooks inbox channel.** An idle session had the item in model context 46 to 113 ms after `InboxStore.send` (n = 9 idle sends, including 3 sent in a burst and 1 after compaction). The first model output came about 3 s later. Nothing was lost across busy turns, compaction or restart. The model sees a tagged channel turn and answers through the `reply` tool, which closes the item done.
- **Claude fallback: the next tool call hook delivery and the tick wake we already run, with the async rewake hook as an optional extra.** Rewake matched the channel's latency only while a watcher was alive. It stranded an item for 98.5 s once every watcher had expired, and a fully idle session would have stayed stranded with nothing else firing. It frames every item as a "Stop hook blocking error".
- **Codex: `codex queue --thread <id> --message <text>`** called from a Redis subscriber. Codex has no channel or rewake equivalent. `codex queue` put the item in an idle session's context in 5.5 to 9.4 s, and the model acted at 10 to 12 s, without typing into the pane. Idle pickups landed on a 30 s cycle. A busy session got the item only after its turn ended: the next turn started at +59.3 s and the item was in context at +59.7 s.

## What was built (scratch only, nothing merged)

Work folder `probe/`, copied from `~/scratchpad/rt2/rig-grade-swarm-rt2/probe`:

| File | Role |
|---|---|
| `inbox_channel.py` | stdio MCP server on Python `mcp` 1.30. Declares `experimental.claude/channel`. Polls `InboxStore.pending_items(address)` every 100 ms and claims each item with `InboxStore.deliver`, so an item goes out once. Pushes `notifications/claude/channel` with `content` = item text and `meta` = `item_id`, `sender`, `sent_at_ms`. Tool `reply(item_id, text)` calls `InboxStore.reply`. |
| `rewake.py` | Async rewake hook body. Reads the hook JSON, polls the same address for up to `INBOX_REWAKE_LIFE_S` (90 s), claims with `deliver`, prints the items to stderr and exits 2. Exits 0 when its life runs out. |
| `send.py`, `state.py`, `reply.py` | Send an item, dump an item's history, and reply from Bash for the rewake probe. |
| `timeline.py` | Joins the server or hook log with the session transcript and reports each step in ms after send. |
| `a/mcp.json`, `b/settings.json` | Probe A (channel) and probe B (rewake) launch config. |

Launch commands:

```
# channel
agentihooks init-agent --agent claude --dir probe/a --prompt-file probe/a/prompt.txt -- \
  --mcp-config probe/a/mcp.json --dangerously-load-development-channels=server:inbox
# rewake: asyncRewake on SessionStart and Stop, timeout 100
agentihooks init-agent --agent claude --dir probe/b --prompt-file probe/b/prompt.txt -- \
  --settings probe/b/settings.json
# codex
agentihooks init-agent --agent codex --dir probe/x --prompt-file probe/x/prompt.txt
codex queue --thread <herdr agent_session id> --message "rt2 probe idle 1"
```

Each probe session was told to run `date +%s%3N` on an event and reply with that number. That gives a model-side timestamp next to the transcript timestamps.

## Timings (ms after `InboxStore.send`)

Columns: **push** is when the server or hook wrote the event. **queued** is the transcript `queue-operation enqueue`. **context** is the first transcript user or attachment row carrying the item. **model** is the first assistant row after it. **reply** is when the reply was written to Redis. Raw rows are in `probe/timelines.txt`.

### Channel

| Case | Item | push | queued | context | model | reply |
|---|---|---|---|---|---|---|
| idle | b9e4d59bf538 | 71 | 73 | 104 | 3223 | 10697 |
| idle | 09617c288682 | 74 | 76 | 96 | 3011 | 6079 |
| idle | a4be51433b67 | 84 | 86 | 107 | 3059 | 6072 |
| burst 1/3, idle | 040725a0c1dc | 52 | 58 | 110 | 3284 | 7341 |
| burst 2/3 | 5b9ea85270a0 | 39 | 46 | 46 | 6574 | 13182 |
| burst 3/3 | 641e7fff5787 | 47 | 114 | 113 | 6167 | 18292 |
| busy (sent during a 20 s `sleep`) † | 8594a85b5c2b | 84 | 86 | 86 * | 18708 | 70854 |
| after `/compact` | 14c107823dda | 48 | 50 | 66 | 3639 | 8142 |
| during `/compact` | 230394899e0c | 69 | 72 | 13414 | 19075 | 22234 |
| sent while the session was down | 7244e2298cbb | 25169 ** | 25172 | 25204 | 28751 | 32342 |

† Item 93a2f723a9aa, the instruction that started the busy turn, arrived while the session was idle and is left out of the table. Its timings are in `timelines.txt`.

\* For the busy case, the context row is the queued attachment. The transcript shows the event held in the queue until the running Bash call returned (19:03:50.29). It entered the next model request at 19:03:51.0, about 12.2 s after send, and the model output that saw it is the 18.7 s value. The model saw it mid turn, finished the task it had been given, then answered the probe.
\** The server died with the session (`/exit`). The item stayed `pending` in Redis. On `--resume` the server pushed the backlog 1.03 s after its process started (8 ms after its `ready` line). The 25 s is mostly downtime.

### Async rewake

| Case | Item | wake | queued | context | model | reply |
|---|---|---|---|---|---|---|
| idle | a94bea1f957d | 47 | 121 | 162 | 3330 | 8432 |
| idle | 2e611786cea3 | 10 | 127 | 153 | 3371 | 8197 |
| idle | 0215750c6445 | 44 | 105 | 125 | 3011 | 7351 |
| busy, caught by a watcher still alive from an earlier Stop | 41131c4a691d | 66 | 116 | 116 | 20539 | 25015 |
| **every watcher expired, session idle** | e019b4bb4556 | **98504** | 98535 | 98778 | 102135 | 106704 |
| sent while the session was down | 66e94bc51120 | 13896 | 14114 | 14159 | 17531 | 22136 |

e019b4bb4556 was rescued only because `/compact` fired `SessionStart` (hook log: arm at 1791313873639, wake 125 ms later). Without an operator or tool event, nothing re-arms a watcher in an idle session.

### Codex `codex queue`

From the saved rollout `probe/x/rollout.jsonl` (computed in `probe/x/timeline.txt`, send times in `probe/x/sends.txt`):

| Case | Command returned | Turn started | In context | Model ran the probe command |
|---|---|---|---|---|
| idle 1 | 195 ms | +4947 ms | +5547 ms | +10320 ms |
| idle 2 | 93 ms | +8932 ms | +9389 ms | +12314 ms |
| busy (sent during a 3 × `sleep 20` turn) | under 1 s | +59337 ms, 24 ms after the busy turn's `task_complete` | +59748 ms | +63110 ms |

In an idle session, queued turns started at 19:14:19.520, 19:14:49.517 and 19:15:19.515, exactly 30.0 s apart, whatever the send time. Hypothesis, not confirmed in Codex source: an idle session picks up queued messages on a 30 s cycle, so idle latency is anywhere from 0 to 30 s plus turn start.

## How the model sees each path

- **Channel:** a user turn with `origin.kind = "channel"`, `promptSource = "system"`, `isMeta = true`:
  `<channel source="inbox" item_id="b9e4d59bf538" sender="rt2-probe-sender" sent_at_ms="1791313339217">probe one, idle session</channel>`.
  The terminal shows `← inbox: probe one, idle session`. The server's `instructions` string reaches the model at connect, so the model knew to call `mcp__inbox__reply` with `item_id` without being told in the prompt.
- **Rewake:** a turn with `origin.kind = "task-notification"`:
  `<task-notification><summary>Stop hook feedback</summary></task-notification><system-reminder>Stop hook blocking error from command "Stop": Inbox item … </system-reminder>`.
  The item is framed as a hook error, and the model needs the reply route spelled out in its prompt or rules.
- **Codex queue:** a plain user turn (`› rt2 probe idle 1`), indistinguishable from typed input.

## Failure modes observed

### Channel

1. **The development channels flag is variadic.** `--dangerously-load-development-channels server:inbox` followed by the opening prompt makes Claude exit with `entries must be tagged: <the prompt text>`. Use the `=` form: `--dangerously-load-development-channels=server:inbox`.
2. **A warning dialog appears at every launch, resume included.** "WARNING: Loading development channels … 1. I am using this for local development / 2. Exit". It is shown in the terminal only, cannot be relayed, and blocks startup until answered. An unattended swarm launch must answer it, for example with a herdr `send-keys Enter` after `wait-output` on that text. This sits on top of the existing folder trust prompt.
3. **No delivery acknowledgement.** The documentation says that when a session has not registered the server as a channel, events are dropped silently and the server gets no error. Our probe marks an item `delivered` when it writes the notification, so an unregistered session would lose items. Production design: keep the item visible to the next tool call hook until the model replies or the item is closed. That makes the hook path the backstop, not a duplicate. This is a design recommendation; the silent drop itself comes from the documentation and was not reproduced.
4. **Protocol revision 2026-07-28.** Claude 2.1.292 sent `server/discover` with `protocolVersion` 2026-07-28 first, even with `MCP_PROTOCOL_NEGOTIATION` unset (`probe/c/init-default.log`). Python `mcp` 1.30 does not support that revision, so the client fell back to `initialize` 2025-11-25 and the channel registered. A server built on an SDK that speaks 2026-07-28 would not register as a channel (documented on the MCP page). Pin the server SDK to a version without that revision, or set `MCP_PROTOCOL_NEGOTIATION=legacy` in the swarm launch environment.
5. **Lifetime is tied to the session.** The server exits with Claude, and items wait in Redis until the next launch drains them. Nothing is lost, because Redis is the durable store.
6. **Compaction holds events.** They are queued and arrive when compaction ends (13.4 s here). None were lost.
7. **Preview limits from the documentation.** Channels need claude.ai or Console API key auth; Bedrock, Vertex and Foundry are out. `channelsEnabled` must be true where managed settings are deployed. `-p` and the Agent SDK ignore the development flag. The flag syntax and contract may change. These probes ran under Console API key auth with no managed settings, and the channel registered.
8. **A busy session waits for the running tool call.** Events join the next model request after the current tool returns. A long tool call delays them for its full duration.

### Async rewake

1. **It fires only when the script exits, and Claude re-arms it only on hook events.** Every Stop spawned a new watcher, and the logs show up to three alive at once. That is harmless only because `deliver` claims atomically. Once the last watcher's life runs out in an idle session, items strand: 98.5 s here, and indefinitely without an outside event. `timeout` is enforced on `asyncRewake` hooks (default 600 s), so a watcher cannot outlive it.
2. **Misleading framing.** Every item arrives as a "Stop hook blocking error".
3. **No reply channel.** Replies go through Bash or the CLI.
4. **Under `-p`, async hooks are killed at teardown** (documentation).

### Codex `codex queue`

1. Idle delivery took 5.5 to 9.4 s to reach context, with turn starts on a 30 s cycle. A busy session waits for the whole turn.
2. It needs the thread id, which herdr reports as `agent_session.value` (`herdr pane get`).
3. It does not type into the pane, so an operator draft is never mixed in. The herdr wake path types into the pane.

## Side observation: channel events and operator typing

While probe A was busy, the operator typed into its pane by mistake: a message with a screenshot about the Priorities block. The pane showed the text in the input box while two queued channel events were processed. The transcript records it as a `human` turn at 19:05:07, which the probe never answered. Channel events reached the model alongside that input without editing or submitting it; they never wrote into the input box. A herdr `send-text` wake would have typed into the same box. The operator's message and screenshot were relayed to the master and saved in this task's `operator-message/` folder.

## Recommendation for task rt1

1. Ship `scripts/inbox/channel.py` as an agentihooks MCP server, on an SDK pinned below revision 2026-07-28. Replace the 100 ms poll with a Redis `PUBLISH` from `InboxStore.send` and a subscriber per address. The measured push latency of 25 to 84 ms is almost all poll interval. Keyspace notifications are off on this Redis (`notify-keyspace-events` is empty).
2. Have every swarm Claude launch add `--mcp-config <inbox channel>` and `--dangerously-load-development-channels=server:inbox`, set `MCP_PROTOCOL_NEGOTIATION=legacy`, and have the launcher answer the development channels dialog.
3. Keep the next tool call hook delivery and the tick wake as the fallback. They cover sessions where the channel did not register (`-p`, org policy, preview withdrawn). Do not ship the rewake hook by default. If a swarm setting enables it, arm it on SessionStart and Stop with the maximum `timeout`, and accept that an idle session strands items once the last watcher expires.
4. For Codex panes, a Redis subscriber calls `codex queue --thread <id>` the moment an item lands. That replaces the minute tick wake for Codex and avoids typing into the pane.
