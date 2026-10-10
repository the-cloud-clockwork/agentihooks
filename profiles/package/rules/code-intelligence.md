---
priority: 2
description: Serena through the agentihooks router — activate the worktree by absolute path, edit code by symbol
---

# Code Intelligence — Serena Through the Router

Every session reaches Serena (`mcp__serena__*`) at one endpoint, the agentihooks Serena router, which runs one Serena per worktree.

- First Serena call of a session, and again after moving to another worktree: `activate_project` with the worktree's absolute path. Done when it answers `Active project: <path> (worktree)`.
- Code a language server covers (the repo's `.serena/project.yml`): read with `get_symbols_overview` / `find_symbol`, edit with `replace_symbol_body`, `insert_after_symbol` / `insert_before_symbol`, `rename_symbol`, `replace_content`. Config, docs, data and new files: built-in Read / Edit / Write.
- `read_file` and `search_for_pattern` are listed only to a client whose endpoint names them, `/mcp?tools=read_file,search_for_pattern`, as the Codex engineer does. Use them to read text a symbol lookup returns only by name, such as a constant's value.
- A refusal names its fix. `No project is active` → activate (the router may have restarted). `primary checkout: … edits are refused` → `wt.sh new` (the `worktree` skill), then activate that worktree.
- Every edit result ends `(applied in <root>)`: a root other than your worktree means re-activate before the next edit.
- Python is read and edited by symbol in every worktree; work done directly on the base branch is the one exception.
- Work the operator asks to do directly on the base branch (`WT_BASE_BRANCH`, default `dev`) in the primary checkout edits with built-in Read / Edit; the router refuses the primary checkout.
- Sub-agents work in the parent's worktree and leave `activate_project` to the parent.
- Serena runs only through the router: `agentihooks serena status` lists the backends, `wt.sh done` releases one. Registering another Serena server or running `serena start-mcp-server` by hand is out.
