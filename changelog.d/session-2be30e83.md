### Fixed

- `agentihooks herdr configure` and `agentihooks init` no longer force herdr's
  `[session] resume_agents_on_restore = false`, so after a herdr server restart or
  a workstation shutdown every agent pane reopens its conversation again. The first
  configure after upgrading removes the `false` earlier releases wrote, once.

### Added

- A session started through the agentihooks router in a herdr pane reports a resume
  command to herdr at SessionStart, so herdr reopens it through
  `agentihooks claude --agentihooks-fallback-bare --resume <id>` or
  `agentihooks codex resume <id>` on an account with quota left instead of plain
  `claude --resume <id>`. Unrouted, API, gateway and swarm sessions report nothing.
- `AGENTIHOOKS_HERDR_RESUME=off` turns herdr's agent resume off for machines that
  want swarm reboot recovery alone to bring agents back.
