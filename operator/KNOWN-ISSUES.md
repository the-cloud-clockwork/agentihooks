# Known Issues

## Broadcast delivery inconsistency across sessions

**Status:** Open
**Date:** 2026-04-07
**Severity:** Medium

Some Claude Code sessions receive broadcast messages, others don't. In a test with 3 agents:
- 2 of 3 received and acknowledged an info broadcast
- 1 agent reported no message in recent context

### Possible causes
- Session registered after the broadcast was created (timing)
- `info` severity is one-shot — if the session was already marked in `delivered_to` from a prior delivery, it won't show again
- Hook not wired for that session (different profile, hooks disabled, `--bare` mode)
- `BROADCAST_ENABLED` env var set to false in that session's environment
- Session started from a directory where agentihooks hooks aren't installed

### Investigation steps
1. Check `~/.agentihooks/broadcast.json` — look at `delivered_to` array for the message
2. Check `~/.agentihooks/active-sessions.json` — is the non-receiving session registered?
3. Check the session's hook output — is broadcast injection firing on UserPromptSubmit?
4. Verify the session's settings.json has the hook_manager wired
5. Try `alert` severity (persistent, every turn) instead of `info` (one-shot) to rule out timing
