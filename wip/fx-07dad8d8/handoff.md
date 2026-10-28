# Handoff v2

## Intent
Show by evidence where the Doctor active trace read spends its 15 second budget on the workstation timer, then file the fix on fx-07dad8d8-code.

## Done
- Observed: Langfuse saturates near ten requests per second for the active read's own requests: 1 worker 4.9 per s, 3 workers 8.2, 6 workers 9.8, 9 workers 8.0, 12 workers 7.1; p50 latency rises from 0.19 s to 1.50 s. Connection setup is about 0.03 s, so the cost is server time. Evidence: https://github.com/the-cloud-clockwork/agentihooks/tree/wip/fx-07dad8d8-probe
- Observed: from a hive with no local exporter cursors, 19 working bindings read in 7.2 to 7.6 s with 3 to 4 requests each under the 6 reader workers, while the concurrent history read kept one more request stream busy for its full 45 s. Hypothesis: on the workstation, local bindings add one observation identity page per hundred observations, so a pass near 21 bindings reaches the budget. Evidence: https://github.com/the-cloud-clockwork/agentihooks/tree/wip/fx-07dad8d8-probe
- Observed: between scans 60 s apart, 9, 15, 15 and 13 of 18 working bindings had an unchanged current trace (same id, updatedAt and observation count), yet each pass still spends its listing, freshness and count requests on them. Evidence: https://github.com/the-cloud-clockwork/agentihooks/tree/wip/fx-07dad8d8-probe
- Observed: every unread binding in the misses at 16:33, 17:03, 17:37, 18:29 and 18:46 UTC is budget spent with no request error, so workers were busy or queued, never refused. Evidence: workspace:fx-07dad8d8-cause/proof

## Stopped at
The workstation run of the probe was never made: this hive is stopping for a release, the ledger refuses its credential with 403 and both inbox requests to the Doctor master were cancelled.

## Decisions and promises
- Proposed fix for fx-07dad8d8-code: skip the freshness, count and identity requests for a binding whose current trace updatedAt equals the stored mark, read the per binding trace listings in one tagged listing call instead of one per agent, and keep the active read off Langfuse while the history read lists (or give history its own later slot), since adding workers lowers throughput past six.

## Next
1. On the workstation, run the probe: fetch origin wip/fx-07dad8d8-probe, then from the main checkout run the ws probe script from that branch with arguments rig-grade-swarm 3 60 and a scratch folder; it copies the live reader state and never writes it. Completion check: per binding requests, langfuse_s, id_pages, accepted_wall_s and listing_reused for three passes appended to the task proof.
2. Close fx-07dad8d8-cause with swarm done giving the root cause above, the probe output as evidence, and --filed naming the fix proposal on fx-07dad8d8-code. Completion check: done accepted.

## Read first
- workspace:fx-07dad8d8-cause/proof probe outputs and commands
<!-- handoff complete -->
