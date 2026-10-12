# SV2-MST-05: Two masters across two hives

G10 qualification requires a scratch ledger, two hives and two masters. Each master steers its phases and receives questions and follow ups addressed to its hive. They coordinate through the private masters channel. Unaddressed operator chat reaches the lead master.

The ledger writes that carry hive targets are `agentihooks ledger question add TEXT --hive ID` and `agentihooks ledger followup add TEXT --hive ID`. The stored item retains `hive`; its added event, priority notice, later operator comments and follow up escalation route to the live master owning that hive. Configure ownership with `agentihooks swarm SLUG set hive.ID=SEAT`. Direct inbox writes use `agentihooks msg send SEAT TEXT`, addressed to the owning master seat. There is no direct hive inbox address.

The ledger master fence checks the same owner inside the locked write. A refused master changes no item and posts no decision. Accepted decisions retain their seat and revision. Replaying the same ledger operation leaves one answer and one channel post. Inbox delivery permits one receiver to claim each pending item; a separate priority notice is a distinct notification about that item.

The CI fixture uses two admitted master execution identities, separate hive ownership and a scratch ledger folder. Positive, concurrent delivery and replacement cases each run twice on independent fakeredis servers. The fixture exercises the production ledger operations, master fence, event relay, inbox store and private channel. It launches no agent session or cloud worker. This evidence does not qualify the live two hive rollout.

Untargeted items retain lead routing. If the hive owner is absent or unknown, the current lead receives the item. A decision already stamped by a master seat retains the existing decision fence across occupant replacement.

Rollback omits the hive option for subsequent writes and restores the previous code through Git. Preserve existing ledger items, inbox history, decision stamps and private channel cursors. Scratch fixtures own their temporary ledger folders and Redis instances; remove only those resources.

The live proof waits for SV2-GIT-05. It must name the scratch ledger, both hive identities, both admitted master executions, each phase owner, operator chat receipt at the lead, channel exchanges, inbox delivery histories, rejected cross owner decisions and replay results. Anton changes use GitOps and on demand capacity. Retain observed commit and deployment evidence; keep the package incomplete until that proof exists.
