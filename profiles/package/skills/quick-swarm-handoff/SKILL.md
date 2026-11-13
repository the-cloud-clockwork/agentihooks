---
name: quick-swarm-handoff
description: >
  Hand a quick swarm seat to a successor that starts in the same herdr pane:
  write the seat's Handoff v2 into its seat folder, record lessons, then run the
  quick handoff command. Use when the operator says "hand off this quick seat",
  when a quick swarm member reaches HANDOFF PREPARATION or CONTEXT RECYCLE, or
  when a quick seat must be recycled.
---

# Quick Swarm Handoff

The quick swarm block names your seat folder. Everything goes there.

1. Write the handoff to the seat's handoff file named in the quick swarm block,
   in Handoff v2 form: the line `# Handoff v2`, then Intent, Done, Stopped at,
   Decisions and promises, Next and Read first, each once and in that order,
   None under an empty heading, and `<!-- handoff complete -->` last. Every Done
   bullet carries evidence or the word hypothesis. Read first lists addresses:
   a GitHub issue or pull request link, or `seat:<seat>` for a seat folder of
   this quick swarm. No file paths, line numbers or credential values in the
   body. Done when the file holds the document.

2. Put any lesson every later occupant of the seat needs into the seat's
   learned file, with the reason it holds.

3. Tick the plan's finished stages so the successor sees where the plan stands.

4. Submit:

   `agentihooks quick handoff`

   It checks the document and marks the seat pending. A refusal lists what to
   fix; fix it and run it again. Done when it prints that the handoff was
   accepted.

5. Stop. At your next stop this session ends with its pane kept, and the
   successor opens in the same pane with the seat's seed and your handoff as
   its opening prompt. If it does not, the next hourly quick tick round starts
   it.
