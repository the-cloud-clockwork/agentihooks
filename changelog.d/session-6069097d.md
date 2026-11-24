### Added

- The swarm knows a new agent kind, `expert`, named `expert@<code>-<n>` with a numbered seat `expert-<n>@<swarm>`. Any launch or resume on the expert lane runs Claude Code on the Fable model at effort xhigh, or max when its lineage asks for it, whatever the swarm effort range, a lane setting or a saved handoff launch says, and an expert launch that lands on another harness is refused. No command starts an expert yet.
