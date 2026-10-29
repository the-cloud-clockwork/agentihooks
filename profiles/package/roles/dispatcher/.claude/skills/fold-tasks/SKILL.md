---
name: fold-tasks
description: Folds open swarm ledger tasks that change the same code into one grouped task, so one agent delivers them in one pull request and the unfrozen task count drops. Code finds candidate groups, checks the fold plan and applies it with the ledger's task group command; the agent judges which candidates belong together. Use when the operator says "fold tasks", "fold-tasks", "merge duplicate tasks", "group these tasks", "too many open tasks", or when a master or dispatcher sees open tasks that touch the same code.
---

# Fold Tasks

`SLUG` is the ledger and `NAME` this session's agent name, as in every `agentihooks ledger` call. Every command below is `agentihooks ledger --slug SLUG --as NAME fold ...`. A fold makes the lead task carry each folded task's change as a numbered seam; the members leave the claim queue and return to it if the lead's agent stops.

## Steps

1. **Candidates.** Run `fold candidates`. It prints the pool size, the tasks left out by reason (frozen, in flight, planner chain, grouped, not code) and candidate groups: each task's card and every signal linking two tasks (shared territory, shared code names, title words). Done when the JSON is read. No group → report that nothing folds and stop.

2. **Judge.** Read each candidate's descriptions and territories, never titles alone. Keep a group only when its changes belong in one reviewable pull request; a shared broad folder alone is not enough. Drop an oversized member (a corpus for every detector, a whole new subsystem) rather than make a giant pull request; split a group when it holds two unrelated changes. Run `agentihooks scratch new` and write `plan.json` there:

   ```json
   {"groups": [{"lead": "ID", "members": ["ID"], "seams": ["one plain sentence per task, lead first"], "done": "plain sentence, or empty for the default"}]}
   ```

   Seams and done are plain words read by the operator: no task ids, no paths, no dashes. Name the command, hook or behaviour instead. Done when every kept group is in the plan.

3. **Check.** Run `fold check FOLDER/plan.json`. Exit 2 prints each problem with its group number; fix the plan and run it again. Done when it prints `"ok": true`.

4. **Ask.** One `AskUserQuestion` call: how many groups fold, with the options fold every group, fold only the true duplicates, fold none. Name each group in the question by its lead's title. When the question tool is refused, write nothing to the operator and end the run. Done when the operator decided or the run ended unasked.

5. **Apply.** Cut `plan.json` to the groups the operator chose and run `fold check` once more, then `fold apply FOLDER/plan.json`. It re-checks against the live ledger, then per group groups the tasks, rewrites the lead's description and territory, and comments each member's status, printing one JSON line per group. A refusal exits with the ledger's reason; groups already printed stand. Done when every chosen group printed its line or its refusal is reported.

6. **Report.** Groups made, tasks folded, tasks removed from the unfrozen count (members folded), and each refusal in plain words. Done when the report is sent.
