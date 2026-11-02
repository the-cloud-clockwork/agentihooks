---
name: workflow-maker
description: Interviews for a simple workflow, where profile seats work in a loop of stages joined by pass, retry and enhance edges, and writes it as block style YAML that agentihooks workflow validate accepts. A script picks the folder, the package workflows inside an agentihooks checkout and the linked bundle elsewhere. Use when a user asks to make, write, design or change a workflow, a stage loop, or a judge or scout loop.
---

# Workflow Maker

Requires the installed `agentihooks` CLI. Never pick the target folder, edit a
loaded workflow in place, or write keys outside the two primitives below.

1. **Interview.** Reuse answers already given. Ask for:
   - the **intent**: one sentence naming the outcome;
   - the **stages**: for each, an id, the seat `profile` (`scouter`, `judge`,
     `planner`, `engineer` or any role or overlay the user names), how many
     agents (`count`), what it reads (`input`: `case` and earlier stage ids),
     and its own loop cap (`iterations`);
   - the **edges**: for each stage, where `pass`, `retry` and `enhance` go
     (a stage id, `exit`, or none);
   - the **exit**: the `STAGE.pass` that ends the workflow, the overall
     `max_iterations` and `on_exhausted` (`block`, `escalate` or `keep-best`).

   Show `agentihooks workflow show simple-workflow` as the reference shape.
   Done when every stage is reachable from the head and the exit names a stage.

2. **Draft.** Write the YAML to a scratch file under the session working files
   folder, block style only (no `{}` or `[]` with items):

   ```yaml
   workflow:
     name: NAME
     intent: ONE SENTENCE
     head: FIRST_STAGE
     max_iterations: 3
     exit: LAST_STAGE.pass
     on_exhausted: block
   stages:
     FIRST_STAGE:
       profile: engineer
       tier: high
       effort: medium
       count: 1
       input:
         - case
       filters: []
       iterations: 1
       exit: null
       next:
         pass: LAST_STAGE
         retry: null
         enhance: null
   ```

   Workflow keys: `name`, `intent`, `head`, `max_iterations`, `exit`,
   `on_exhausted`. Stage keys: `profile`, `tier` (`high` or `low`), `effort`,
   `count`, `input`, `filters`, `iterations`, `exit`, `next`. Omitted keys take
   their defaults. Done when the draft holds only these keys.

3. **Write.** From the session's working repository run the script with the
   Python behind the `agentihooks` command:

   ```bash
   AH_PY="$(sed -n '1s/^#!//p' "$(command -v agentihooks)")"
   "$AH_PY" SKILL_DIR/scripts/write_workflow.py DRAFT_FILE
   ```

   `SKILL_DIR` is this skill's directory. The script validates the draft and
   writes `NAME.yaml` to `profiles/package/workflows/` when the working
   repository is an agentihooks checkout, and to the linked bundle's
   `workflows/` otherwise. It prints `{"written": PATH}`. It refuses an invalid
   or flow style draft, printing every error: fix the draft and rerun. It
   refuses an existing file: rerun with `--replace` only when the user asked
   to change that workflow. Without a linked bundle outside agentihooks it
   refuses; report it and offer `agentihooks bundle link BUNDLE_DIR`. Done
   when the script prints the written path.

4. **Validate.** Run `agentihooks workflow validate PATH` on the printed path,
   then `agentihooks workflow validate NAME` from the same repository. Done
   when both print `valid`. Report the path, then commit it through that
   repository's development workflow.
