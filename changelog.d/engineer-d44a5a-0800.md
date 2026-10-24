### Fixed

- The hourly lifecycle sweep and `agentihooks gc` reclaim Codex execution homes
  that no live process owns, in the user runtime folder and under
  `~/.agentihooks/cx`. A home is owned while its server socket answers or a live
  process has its working folder, an open file or an argument inside it; homes
  younger than ten minutes are left alone. An orphan home has its transcripts
  copied to the Codex sessions store before it is removed, and the report lists
  each home under `codex_homes`.
