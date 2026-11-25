### Added

- `agentihooks init --target codex` writes bundle agents as Codex custom agents in `~/.codex/agents/<name>.toml`, which Codex offers as spawnable roles. A later install removes the files it wrote and no longer ships. An operator's own file of the same name is never touched.
- Codex now also gets what Claude Code loads from `~/.claude` when the operator placed it there by hand and no layer ships it: rules are compiled into `AGENTS.md` labelled `local`, skills are linked into `~/.agents/skills`, commands are copied there as skills, and agents become Codex custom agents.
