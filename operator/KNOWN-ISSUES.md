# Known Issues

## Broadcast delivery inconsistency across sessions

**Status:** Open
**Date:** 2026-04-07
**Severity:** Medium

Some Claude Code sessions receive broadcast messages, others don't. In a test with 3 agents:
- 2 of 3 received and acknowledged an info broadcast
- 1 agent reported no message in recent context

### Possible causes
- **Channel mismatch** — the broadcast was published to a channel name that's not in the session's `AGENTIHOOKS_BASE_CHANNELS` env var. Global broadcasts (no `channel` field) reach everyone; tagged broadcasts only reach subscribed sessions. Check the message's `channel` field in `broadcast.json` against the session's subscription list (visible on statusline Line 3 as `channels:...`).
- Session registered after the broadcast was created (timing)
- `info` severity is one-shot — if the session was already marked in `delivered_to` from a prior delivery, it won't show again
- Hook not wired for that session (different profile, hooks disabled, `--bare` mode)
- `BROADCAST_ENABLED` env var set to false in that session's environment
- Session started from a directory where agentihooks hooks aren't installed

### Investigation steps
1. Check `~/.agentihooks/broadcast.json` — look at the message's `channel` field and the `delivered_to` array.
2. If the message has a `channel`, check the receiving session's subscription list — either look at the statusline `channels:` row in that session, or `cat ~/.claude/settings.json | jq '.env.AGENTIHOOKS_BASE_CHANNELS'`. If the channel isn't there, that's the cause — re-publish to a channel the session subscribes to, or extend the env via `.claude/settings.local.json`.
3. Check `~/.agentihooks/active-sessions.json` — is the non-receiving session registered?
4. Check the session's hook output — is broadcast injection firing on UserPromptSubmit?
5. Verify the session's settings.json has the hook_manager wired
6. Try `alert` severity (persistent, every turn) instead of `info` (one-shot) to rule out timing
