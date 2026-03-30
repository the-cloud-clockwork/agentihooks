# Skills

Place skill subdirectories here. Each skill is a directory containing a `SKILL.md` file.

## Format

Skills follow the [AgentSkills open standard](https://agentskills.io).

Example structure:
```
.claude/skills/
  my-skill/
    SKILL.md       ← skill definition (name, description, trigger, steps)
```

## Installation

Running `python agentihooks init` from the agentihooks root will
symlink each skill directory here into `~/.claude/skills/`, making it
available in every Claude Code session on this machine.
