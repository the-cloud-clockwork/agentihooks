## Summary

Brief description of what this PR does.

## Assumptions

<!-- Fill from the task recon review details. -->

## What could break

<!-- Name the affected behavior and failure risks from recon. -->

## What will not break

<!-- Name the behavior the regression protections preserve. -->

## Features that may disconnect

<!-- Name callers, integrations and feature connections at risk. -->

## Dependencies

<!-- Name required changes, services or libraries; state none when absent. -->

## Resources

<!-- Name data size, CPU, memory and runner needs from recon. -->

## Why the fix

<!-- State the proven defect, or explain that this is a feature. -->

## Why the feature

<!-- State the behavior this delivers, or explain that this repairs existing behavior. -->

## Notes for the reviewer

<!-- Name the recon evidence, seams and risks to inspect first. -->

## Changes

- ...
- ...

## Test Plan

- [ ] Python files pass syntax check (`py_compile`)
- [ ] MCP server starts with changes: `python -m hooks.mcp`
- [ ] Hook system still works: `echo '{"hook_event_name":"SessionStart"}' | python -m hooks`

## Checklist

- [ ] README updated (if adding new category/tool)
- [ ] Changelog fragment added as `changelog.d/<branch>.md`
- [ ] `_registry.py` updated (if adding new category)
- [ ] `hooks_list_tools` in `utilities.py` updated (if adding new tools)
