## Summary

Brief description of what this PR does.

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
