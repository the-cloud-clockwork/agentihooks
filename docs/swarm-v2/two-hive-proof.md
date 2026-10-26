# SV2-MST-05: Two masters across two hives

G10 qualification requires a scratch ledger, two hives and two masters. Each master steers its phases and receives questions and follow ups addressed to its hive. They coordinate through the private masters channel. Unaddressed operator chat reaches the lead master.

## Hive ownership and ledger writes

The ledger writes that carry hive targets are `agentihooks ledger question add TEXT --hive ID` and `agentihooks ledger followup add TEXT --hive ID`. The stored item retains `hive`; its added event, priority notice and follow up escalation route to the live master owning that hive; later operator comments reach the front door lead, which hands them on over the masters channel. Configure ownership with `agentihooks swarm SLUG set hive.ID=SEAT`. Direct inbox writes use `agentihooks msg send SEAT TEXT`, addressed to the owning master seat. There is no direct hive inbox address.

Use an existing scratch swarm with two admitted masters. In these examples,
`$PROOF_SWARM` is its code generated name, `$LOCAL_HIVE` and `$ANTON_HIVE` are
its registered hive identifiers, and `$LEAD_AGENT` is the admitted lead master's
agent name. Run configuration commands as the operator or an authorized master;
setting ownership does not create hives or admit master executions.

```bash
agentihooks swarm "$PROOF_SWARM" set master-per=off masters=2
agentihooks swarm "$PROOF_SWARM" set \
  "hive.$LOCAL_HIVE=master@$PROOF_SWARM" \
  "hive.$ANTON_HIVE=master-2@$PROOF_SWARM"
agentihooks swarm "$PROOF_SWARM" status --json
```

For a fixed two master proof, `master-per=off` keeps the explicit `masters=2`
count. `master-per=hive` instead derives the count from the swarm's hives; see
the [swarm command reference](../pillars/swarm.md#commands). Phase assignment
and hive ownership serve different purposes: masters steer their assigned phases,
while the hive mapping routes hive targeted questions and follow ups.

```bash
agentihooks ledger --slug "$PROOF_SWARM" --as "$LEAD_AGENT" \
  question add "Which phase should this hive handle next" --hive "$ANTON_HIVE"
agentihooks ledger --slug "$PROOF_SWARM" --as "$LEAD_AGENT" \
  followup add "Verify the worker result before continuing" --hive "$ANTON_HIVE"
```

Use the returned item identifier when inspecting its history or making an
accepted decision. `--hive` selects ownership for the new item; it does not
impersonate the owner or grant the writer authority to answer it. Omit it for
ordinary lead routing.

The ledger master fence checks the same owner inside the locked write. A refused master changes no item and posts no decision. Accepted decisions retain their seat and revision. Replaying the same ledger operation leaves one answer and one channel post. Inbox delivery permits one receiver to claim each pending item; a separate priority notice is a distinct notification about that item.

## Coordination and duplicate handling

From each admitted master session, use the private channel for coordination:

```bash
agentihooks swarm "$PROOF_SWARM" masters-channel say "My phase is ready for the next hive"
agentihooks swarm "$PROOF_SWARM" masters-channel read
agentihooks swarm "$PROOF_SWARM" masters-channel read --all
```

`read` advances that seat's cursor; `--all` inspects retained posts. A successor
inherits the seat's cursor. Only occupied master seats can read or post. A post
notifies the other occupied master seats through their inboxes; the post text
remains in the private channel rather than a fleet broadcast. The operator's
unaddressed chat continues to reach the lead.

For a direct inbox message, address the owning master seat:

```bash
agentihooks msg send "master-2@$PROOF_SWARM" "Inspect the new hive question"
```

This creates a separate inbox message; it is not how a ledger question gains a
hive target. The proof uses the channel for master coordination and actual
ledger writes for hive routing. Check the ledger item's decision and its inbox
history together: an added event and a priority notice can both refer to one
item without being duplicate decisions. Concurrent receivers must not both
claim the same pending inbox item, and replay must not apply a decision twice.

## Worker browser configuration

[PR 4171](https://github.com/the-cloud-clockwork/agentihooks/pull/4171) fixes a
worker home startup refusal encountered while preparing this proof.
`worker_home.materialize` excludes MCP server names beginning with
`playwright-ext-`, case insensitively, before registering servers in the worker
home. These servers drive the operator's desktop extension browser and cannot
run inside a worker Pod. Other MCP servers and the browser role's
`playwright-cmd` headless browser retain their existing behavior. Local profile
installs retain their desktop extension servers.

No new setting enables this filter: it applies when the worker home is
materialized. A master that needs browser work inside a Pod uses its configured
headless browser role. An operator desktop login is not made available by the
worker home.

If the agent exits with `worker start refused: claude MCP server leaves the
execution root: /c`, inspect the worker agent container's retained startup
output and the released worker revision. The reported failure came from a
`cmd.exe /c` desktop extension entry; the execution root check remains active.
Update the code and deploy through CI and GitOps rather than editing the Pod or
weakening that check. A later registration `409` alone does not prove this
browser failure; inspect the preceding attempt's refusal and lease state.

## Evidence and qualification limits

The CI fixture uses two admitted master execution identities, separate hive ownership and a scratch ledger folder. Positive, concurrent delivery and replacement cases each run twice on independent fakeredis servers. The fixture exercises the production ledger operations, master fence, event relay, inbox store and private channel. It launches no agent session or cloud worker. This evidence does not qualify the live two hive rollout.

Untargeted items retain lead routing. If the hive owner is absent or unknown, the current lead receives the item. A decision already stamped by a master seat retains the existing decision fence across occupant replacement.

The live proof must satisfy the SV2-GIT-05 dependency and name the scratch ledger, both hive identities, both admitted master executions, each phase owner, operator chat receipt at the lead, channel exchanges, inbox delivery histories, rejected cross owner decisions and replay results. Anton changes use GitOps and on demand capacity. Retain observed commit and deployment evidence; keep the package incomplete until that proof exists.

The delivered worker home fix is narrower than G10 qualification. Its
[green focused proof](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39052518488)
and [green mutation preflight](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39052466377)
verify the filter, including mixed case names and browser role enabled and
disabled. They do not show two live masters completing the proof.

For live qualification, use a code generated scratch proof swarm on the in
cluster ledger. Point the main Anton controller at that swarm through its own
GitOps pull request with cap one, favoring Anton compute over workstation
memory. After recording the qualification evidence, remove both the scratch
proof swarm and the controller pointer through pull requests. Keep the proof
result separate from the routing fixture and worker startup fix; neither alone
establishes completed live qualification.

## Rollback and scratch cleanup

Rollback omits the hive option for subsequent writes and restores the previous code through Git. Preserve existing ledger items, inbox history, decision stamps and private channel cursors. Scratch fixtures own their temporary ledger folders and Redis instances; remove only those resources.
