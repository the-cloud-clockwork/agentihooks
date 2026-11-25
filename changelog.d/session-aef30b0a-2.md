### Changed

- A Codex install into its own `CODEX_HOME` keeps its skills in that home's
  `skills` folder instead of the shared `~/.agents/skills`, so a separate
  Codex, such as an assistant's, no longer shows its skills to every other
  Codex on the machine. Installs without `CODEX_HOME` are unchanged.
