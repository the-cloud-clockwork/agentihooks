---
priority: 1
description: Before the first edit on any task an agent runs a recon with scouter sub agents and writes recon.md with six headings; the tests that protect touched behaviour land in the same pull request
---

# Recon Before Edit

Applies to every agent that edits on a task: engineer, ci, frontend, documentation and any other role.

- **Order:** the still valid scouter check where the role runs one, then this recon, then the traced plan, then the first edit. An agent already past its first edit writes the recon before its next edit or push.
- **Sub agents do the recon.** Launch scouter sub agents of more than one kind: code areas and their callers; regression tests and the feature map; resources and production data sizes; what might break. The main agent only reads their results and writes the file from them; it never runs the recon itself. Do not enter plan mode.
- **Write `recon.md`** in the task work folder: at most 500 lines, these six headings in this order, each non empty:

```
## What will be built
## Code areas touched
## What might break
## Regression tests
## Resources needed
## Protection plan
```

- **Regression tests** lists the existing tests and feature map entries that cover each touched behaviour, names each touched working behaviour that has no test, and the tests and feature map entries the task adds for it.
- **Protect before you modify.** Those tests and entries land in the same pull request, written against the current code so they pass before the modification.
- **Resources needed** names the services, dependencies, Redis or database keys with their production sizes, CPU and memory requests, runners and images the change relies on or changes.
- A context handoff after the recon is fine; the successor reads `recon.md` first.
