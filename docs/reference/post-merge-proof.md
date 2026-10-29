# Post merge proof

A master or planner declares required post merge evidence on a code or CI task:

```bash
agentihooks ledger --slug example --as master task set task-name contract.after=yes
```

Set `contract.must`, `contract.check` and `contract.judge` to name the target,
measurement and reviewer. Completion requires nonempty `proof.command` and
`proof.output`, recorded through `swarm done --command ... --output ...` or task
proof fields. The judge checks that the recorded output meets the named target.

When the registered pull request merges with proof still missing, the task
reopens as `state=open, awaiting=verify`. Its pull request and handoff survive;
the released worktree is removed. A live engineer retains ownership until it
leaves. The task and its issue remain open until the evidence is recorded and
the task closes. Ordinary code tasks still close on merge.

Verification tasks do not consume an engineer seat while their measurement
window is pending. Set `contract.after_at` to the window's maturity time as a
string containing Unix milliseconds. The tick can claim the task at or after
that time. An absent or empty maturity holds until the master explicitly clears
`awaiting` with `task set task-name awaiting=`. No worker owns the released
verification wait, so idle and stale claim checks have no worker to retire.
