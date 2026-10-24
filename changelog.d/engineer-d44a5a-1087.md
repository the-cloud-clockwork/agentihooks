### Fixed

- A hosted ledger server now lands a master's relay that quotes words the
  operator typed in a master or planner pane on the workstation. The hooks
  mirror each swarm's recorded operator words into the shared swarm Redis, and
  relay verification reads them there when the server's own store holds none.
  Unrecorded quotes and words from other sessions are still refused.
