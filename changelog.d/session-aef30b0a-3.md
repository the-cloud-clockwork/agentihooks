### Fixed

- On Windows the session start no longer runs `agentihooks deps ensure`: the
  kick could not stop it there (`os.killpg` does not exist), so a dev-tool
  install kept running behind every session. The Notification hook also
  skips the swarm remote control notice on Windows instead of failing on
  `resource`.
