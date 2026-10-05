---
name: rig-doctor
description: Starts a Doctor crew on a working ledger, or resets and starts a demo dating app swarm with its Doctor. Use when the operator says "rig-doctor", "rig doctor", "watch this swarm with a Doctor", or "rig doctor stop".
argument-hint: "[LEDGER] [stop]"
---

# Rig Doctor

1. Run the script with the operator's arguments. Resolve this skill's directory
   from the loaded SKILL.md. Use the Python interpreter that has agentihooks
   installed; requires Python, agentihooks, git, authenticated gh, and a linked
   bundle. Install agentihooks with `uv tool install agentihooks`; the bundle's
   `agentihooks deps ensure` supplies git and gh.

   ```bash
   python3 "$SKILL_DIR/scripts/rig_doctor.py" LEDGER
   python3 "$SKILL_DIR/scripts/rig_doctor.py"
   python3 "$SKILL_DIR/scripts/rig_doctor.py" stop
   ```

   Run exactly one form: a ledger starts its Doctor; no argument resets the
   demo and executes the init-swarm ledger, task, create and start primitives;
   `rig doctor stop` maps to `stop`. `LEDGER stop` closes a specific Doctor.
   Completion: exit zero and Doctor status prints, or the stop command prints
   its closed summary. On failure use the script's remediation and rerun.

2. Return the printed ledger link and Doctor status or closed summary.
   Completion: the operator has the link and observed state.

The demo is owned by this skill under `$AGENTIHOOKS_HOME/doctor-demo-app`
(default `~/.agentihooks/doctor-demo-app`). Its previous copy is preserved in
`doctor-demo-archives`. The script refuses reset while its prior demo swarm
is running; finish or stop that swarm before repeating the demo. `stop`
closes only the Doctor, so the working swarm keeps running.

The private remote defaults to the authenticated gh user's `rig-doctor-demo`;
set `RIG_DOCTOR_DEMO_REPO=OWNER/REPO` for another private remote. The stored
[demo template](demo-template.json) is the only prompt source. Each run writes
its plan and seed, with Next.js frontend and FastAPI backend tasks for
localhost. Doctor rule proposals go to the linked bundle's global
`doctor.md` rule through an operator accepted pull request.
