### Changed

- herdr is enabled whenever its binary is installed, on `PATH` or in
  `~/.local/bin`. `agentihooks init` configures it and `init-agent` opens agents
  in it whatever `state.json` recorded; the recorded answer only decides whether
  a missing herdr is installed. `agentihooks herdr disable` is removed.

### Fixed

- A herdr pane whose launcher exported `AGENTIHOOKS_RESUME_LAUNCHER` resumes
  through that launcher. A `smith claude` gateway pane that herdr reopened with
  plain `claude --resume` came back not logged in.
- A swarm agent in a herdr pane reports a placeholder resume command, so after a
  restart herdr no longer reopens it with plain `claude --resume` on the default
  login while the swarm's reboot recovery reopens the same conversation again.
  The recovery reopens the agent in the pane herdr restored for it when that pane
  waits at a bare shell holding the agent's conversation.
- Recording a herdr choice keeps `herdr.resume_released`, so a later
  `agentihooks herdr enable` or `configure` no longer removes a
  `resume_agents_on_restore = false` set after the release.
