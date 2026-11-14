---
name: launch-troubleshooting-crew
description: Launches the operator's brute force troubleshooting crew on a swarm: stop all other work, five scouts at opus max until every cause is proven, five fixers at opus xhigh working together, three critics at fable xhigh as master sub agents, then hardening with feature map, Semgrep, unit, smoke and anti regression tests. Counts, models and efforts are overridable on the call. Use when one problem has stayed unsolved for two hours, or when the operator says "launch the troubleshooting crew", "brute force this", "send the scouts" or "launch-troubleshooting-crew".
argument-hint: "<slug> [--scouts N] [--fixers N] [--critics N] [--<role>-model M] [--<role>-effort E]"
---

# Launch Troubleshooting Crew

The master runs this on its own swarm `SLUG`. A script does every deterministic step; you supply only the
problem statement, one hypothesis per scout and your judgement of the scouts' proofs. Run it by full path:

```
python3 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/skills/launch-troubleshooting-crew/scripts/crew.py STAGE SLUG [options]
```

A Codex master runs the same script under `"${CODEX_HOME:-$HOME/.codex}"/skills/`.

Every stage takes `--dry-run`, which prints each command and writes nothing. Overrides: `--scouts N`,
`--fixers N`, `--critics N`, `--scouts-model M`, `--scouts-effort E` (fixers and critics likewise) and
`--no-harden`. Defaults are the operator's order: scouts 5 opus max, fixers 5 opus xhigh, critics 3 fable
xhigh, hardening on. Later stages keep the layout saved at launch; flags given on a later call override it
for that call. Pass the operator's words with `--quote` when he asked for the crew; otherwise the
script quotes his standing order for this skill.

## When

- **Two hours on one problem.** One problem is still unsolved after two hours, or the operator asks.
- **One hour is troubleshooting mode first.** A blocker of one category lasting one hour puts you in
  troubleshooting mode and you fix it yourself (the bundle's troubleshooting mode rule). When that fix has
  not solved it by the two hour mark, launch this crew. Both triggers hold; the crew is the second step.

## Steps

1. **Plan.** Run `crew.py plan SLUG --problem "<problem>" --hypothesis "<h1>" ...` with one hypothesis per
   scout, each a falsifiable claim another scout does not hold, plus any overrides. Done when the printed
   stages match what the operator asked.

2. **Scout.** Run the same line with `scout` instead of `plan`. It saves the earlier focus and eng lane
   settings, sets the eng lane to the scout model and effort, adds one eng lane troubleshoot task per scout
   (an engineer in scouting mode: no code), focuses only the crew with focus only, which drops every other
   focus without deleting anything, records the order verbatim as a canon seat note and asks every agent
   working on another task to hand off at its next stop, so its task waits for the restore. While any crew
   task is not done, the swarm's focus claim gate holds every other open task back from a claim. Then set
   the printed `cron` with CronCreate (recurring, every thirty minutes, the printed prompt verbatim). Done
   when the scout tasks are focused and the cron is set.

3. **Judge the proofs.** Read each scout task's proof as it closes. A claim counts only when its evidence
   confirms it; a hypothesis is never a cause. While causes remain unproven, run `scout` again with new hypotheses: it adds
   scouts to the running crew and keeps the focus and lane saved at the first launch. Keep scouting until
   every cause is proven. Done when every
   scout task is done and you can state each proven cause in one sentence; run the fix stage in the same
   turn, so no gap opens in which other work claims seats.

4. **Fix.** Run `crew.py fix SLUG --cause "<proven cause>" ...`. It refuses while any scout task is open,
   then sets the eng lane to the fixer model and effort, adds the fixer tasks with every cause and focuses
   them. Done when the fixer tasks are focused.

5. **Critique.** Run `crew.py critics SLUG` and launch each printed critic as your own sub agent with its
   model and prompt, at most three at a time. Critics edit nothing; send each finding to the fixer task that
   owns it through `agentihooks msg send`. Done when every finding is fixed or ruled out by evidence.

6. **Harden.** Once the old fallback is removed and the tick and controller work, run `crew.py harden SLUG`;
   a crew launched with `--no-harden` skips this step. Done when the hardening task is merged.

7. **Restore.** Run `crew.py restore SLUG`. It unfocuses the crew tasks, focuses the earlier targets in their
   order, skipping any that are done or gone, puts the eng lane back and names the agents the launch
   stopped. Delete the cron. Done when the swarm
   status shows the earlier focus again.

Only follow ups that really come from the troubleshooting, at medium, high or critical priority, join the
crew while it runs; everything else waits for the restore.
