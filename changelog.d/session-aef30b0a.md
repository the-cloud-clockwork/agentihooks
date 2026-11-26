### Fixed

- Codex hooks run on native Windows. Codex wraps each hook in `cmd.exe /c`
  and never runs a command that starts with a quote, so the quoted wrapper
  path did nothing; hooks are now written as `cmd.exe /d /c "<wrapper>"` and
  `agentihooks init` replaces the quoted entries.
- Background hook jobs run on Windows: the worker loads its job file before
  deleting it, which Windows refuses for an open file.
- On Windows the hook skips its swarm, quota, operator, hive and lifecycle
  steps instead of failing in them on every call, which halves hook latency.
