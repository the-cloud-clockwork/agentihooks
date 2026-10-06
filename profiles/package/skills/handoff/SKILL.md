---
name: handoff
description: Writes and reads Handoff v2 Markdown with a runtime envelope, derived seat recap and separate continuity and binding outcomes. Use when preparing or receiving a handoff, when HANDOFF PREPARATION or a recycle directive arrives, or when a restored seat awaits a resume or fresh decision.
argument-hint: "SLUG DOC"
---

# Handoff v2

Requires the installed `agentihooks` CLI. Use this format for every harness and
transfer reason: recycle, quota, succession, takeover, reopen, restore, inbox,
exit or operator. Follow the transfer command supplied by the runtime directive.

1. **Prepare.** At `AGENTIHOOKS_COMPACT_LIMIT` (thousands of tokens, default
   600), the runtime injects a preparation deadline twenty five minutes later.
   Write before that deadline or the hard gate, whichever comes first. The hard
   gate sits `AGENTIHOOKS_HANDOFF_MARGIN` (default 50) above the limit and
   remains the backstop. Create the document
   under the session's `~/scratchpad` task folder, or at the location the
   directive supplies. Done when the document is ready within that window.
2. **Write the body.** Use the template below, exactly one of each heading in
   order. Replace its guidance with short prose. An empty section says `None`;
   an unknown says `unknown` and names its gap and reconciliation action. Each
   Done bullet carries observed evidence (command and result, commit, PR, run
   or proof reference); label any unevidenced bullet `hypothesis`. Distinguish
   observed, reported, hypothesis and unknown. Decisions carry their reason,
   promises their recipient, and Next its completion check. Done when all six
   sections are filled and the completion marker is the last line.
3. **Submit.** The runtime builds the envelope: seat, agent, reason, task,
   phase, ledger, time, worktree, branch, PR and checks, open inbox items,
   claims and conversation id. Leave those facts out of the authored body.
   Credentials, file paths and line numbers never enter that body. In Read
   first, rank resolving addresses, one per bullet starting with its address,
   and name the question each answers: GitHub issue or PR URL,
   `ledger:SLUG/tasks/TASK`, `workspace:TASK/progress`, `workspace:TASK/proof`,
   or `recap:SEAT`. Use only existing references; `None` is valid when no
   reading is needed. For a swarm transfer run:

   ```bash
   agentihooks swarm SLUG handoff DOC --reason recycle
   ```

   Use the actual reason. The command and recycle gate check structure,
   resolving Read first addresses, prohibited content and Done evidence labels;
   they do not certify meaning. Fix a refusal and submit again. The seat recap
   is derived from Done, Stopped at and Next of this document; write no second
   recap and pass no `--recap`. After acceptance, stop as the directive requires.
   Done when the handoff is accepted and its runtime envelope is recorded.
4. **Restore continuity.** As successor, read the runtime envelope and body,
   then walk Read first in rank order before older recaps. Read the supplied
   culture, latest derived recap and learned notes; preserve each named gap.
   Confirm the first Next action exactly as written, using the transfer id
   supplied in priming:

   ```bash
   agentihooks swarm SLUG confirm-handoff TRANSFER --next "FIRST NEXT ACTION"
   ```

   Continuity records that reading and confirmation. Binding independently
   records a live session occupying the seat. Inspect both in the page's
   Handoff outcomes or `agentihooks swarm SLUG status --json`; a live binding
   alone does not confirm continuity. Done when continuity is confirmed and
   binding is live, or the named failure is recorded for a decision.
5. **Resolve recovery gaps.** Failed reboot resume leaves the seat awaiting
   decision. The master or operator explicitly chooses resume or fresh:

   ```bash
   agentihooks swarm SLUG restore-decision AGENT resume
   ```

   Use `fresh` only for that explicit choice. After a crash the runtime authors
   the recovery handoff and marks it recovery; preserve its unknowns. A learned
   note needs a `because` clause explaining its reason and keeps its maturity.
   Done when the chosen recovery has its continuity and binding outcomes
   recorded, or the seat remains visibly awaiting decision with its cause.

## Body template

```markdown
# Handoff v2
## Intent
Confirm the task intent from the ledger as it serves the phase and project, or flag drift.
## Done
- Observed result with its evidence, or hypothesis with the check still needed.
## Stopped at
Last completed step and exact state of any unfinished work.
## Decisions and promises
Decisions and reasons, open promises and recipients, questions and who must answer. None when checked and absent.
## Next
First action and its completion check, then at most four more actions.
## Read first
Ranked resolving addresses, each followed by the question it answers. None when no reading is needed.
<!-- handoff complete -->
```
