# Agents

Place agent definition files here. Each agent is a single Markdown file with
YAML frontmatter.

## Format

```markdown
---
name: my-agent
description: What this agent does and when to invoke it
---

# My Agent

Instructions for the agent go here.
```

## Installation

Running `python agentihooks init` from the agentihooks root will
symlink each `.md` file here into `~/.claude/agents/`, making it available
as a subagent type in every Claude Code session on this machine.
