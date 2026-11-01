### Changed

- The tick gains ledger and coordination adapters, ready for its steps to adopt: ledger tools over the ledger server's export and mutate routes and coordination tools over the swarm config, controller lease, named locks, launch capacity and swarm marks, each bounded by its deadline.
- A tick pass renews the controller lease once and keeps one lease beat for the whole pass instead of renewing and starting a beat thread on every ledger and runtime call, and a launch still checks the lease epoch before it starts; ledger writes in the pass carry the epoch of the ledger they write to.
