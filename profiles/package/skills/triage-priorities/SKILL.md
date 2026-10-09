---
name: triage-priorities
description: Triages a swarm ledger's Priorities with the operator in the master pane. It lists every priority with its item state and clears the resolved ones, then puts the rest to the operator in rounds of at most four prompts. Each decision goes to the ledger as a relayed decision, a comment, a closed follow up or a task change, and its priority is cleared. Use when the operator says "triage priorities", "go through the priorities", "clear the priorities", "what is waiting on me", or when Priorities pile up on the master's ledger.
---

# Triage Priorities

The master runs this from its own pane, and the operator decides here; never send the operator to the page or to another pane. `SLUG` is the master's ledger. The scripts need `agentihooks ledger relay` (agentihooks with the relay command). They read the ledger file and write through `agentihooks ledger` as this session's agent name. Run them by full path in every call, because shell variables you set do not survive between calls. `CLAUDE_CONFIG_DIR` is set by the session itself: it names the master's rendered profile home, and is unset in the operator's own home:

```
"${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/skills/triage-priorities/scripts/list_priorities.py
"${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/skills/triage-priorities/scripts/apply_answers.py
```

## Steps

1. **Start.** Run `agentihooks scratch new` once and note the folder it prints; this run's plans go there. Done when the folder is noted.

2. **List.** Run `python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/skills/triage-priorities/scripts/list_priorities.py SLUG --clear-resolved --skip DECIDED...`, where DECIDED lists every priority id the operator already decided this run (none on the first pass). It prints `{"open": [...], "resolved": [...]}` and has already cleared the resolved ones: answered, done, out of scope, no longer waiting, or item gone. Exit 2 prints what to fix. Done when the JSON is printed. No open entry → step 6.

3. **Ask a round.** Take the next four open entries in printed order (AskUserQuestion takes at most four prompts per call); they come grouped as questions, approvals, blocked, follow ups, phases, tasks. Ask them in one `AskUserQuestion` call, one prompt per priority:
   - question: the ask in plain words, from `ask`, `item_text` and the latest `recent` lines;
   - header: the entry's `group`;
   - options: two or three decisions whose labels name the item, unique in the round ("Approve the queue merge", "Hold the queue merge"), then **Later**.
   Never write the prompts as chat text. When `AskUserQuestion` is refused because the operator is not present, say only one short line, type operator on to answer the questions here, or leave them in Priorities; keep every priority and end the run. Done when that one line is sent and every priority is still listed.
   Done when the operator has decided every prompt.

4. **Write the plan.** Write `plan-N.json` (N counts the rounds) in the noted folder: one entry per priority of the round, mapped by the table. `quote` is the operator's exact words from this round: the option label picked, or the text typed in its place. Done when every decision of the round has one entry.

   | Decision | Entry |
   |---|---|
   | Later | `{"priority": ID, "action": "keep"}` |
   | On a question, approval, blocked task or phase | `{"priority": ID, "action": "relay", "text": "the decision in a plain sentence", "quote": "the operator's words"}` |
   | On a follow up | `{"priority": ID, "action": "followup-done", "text": "the decision in a plain sentence", "quote": "the operator's words"}` |
   | Reopen, close or move a task | `{"priority": ID, "action": "task", "fields": {"state": "open"}}` |
   | Context only the master adds | `{"priority": ID, "action": "comment", "text": "plain words, no ids or paths"}` |

   A relay posts as the operator, marked relayed from the master pane; the agent that owns the item acts on it like any OPERATOR line, approvals included.

5. **Apply.** Run `python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/skills/triage-priorities/scripts/apply_answers.py SLUG FOLDER/plan-N.json`.
   - Exit 2: nothing ran; fix each printed line in the plan and run it again.
   - Exit 1: the entries with `"ok": false` failed and keep their priority; tell the operator which and the printed `error`. A refused relay means the quote is not the operator's recorded words, or the round is over an hour old: ask that prompt again.
   Done when it exits 0 or every failure is reported. Add the round's priority ids to DECIDED and go back to step 2.

6. **Report.** One line per group: decided, kept for later, failed. Name each kept item in plain words. Relayed approvals and blocked tasks are handed to their engineer, not finished: say so. Done when Priorities hold only what the operator kept, what failed, and what arrived during the run.
