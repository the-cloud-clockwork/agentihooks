---
title: Tick step catalogue
parent: Reference
nav_order: 73
---

# Tick step catalogue

Every registered tick step, by group and name, with the lane it runs on, its budget floor and interval in seconds, the
steps it needs, the ports its effects name and its feature map id. `python -m scripts.tick.catalogue render` writes this
page from `scripts.tick.registry.steps()`, and `tests/test_docs_tick.py` fails when it no longer matches the registry.
How a step is added is in [Tick engine steps](tick-engine-steps.md#adding-a-step).

| Step | Group | Lane | Floor | Interval | Needs | Effects | Feature |
|---|---|---|---|---|---|---|---|
| failed panes | agents | slow | 2 | 120 | none | none | none |
| reap | agents | tick | 22 | 60 | spawn due | inbox, ledger, runtime | swarm-tick-agents-reap |
| recover outages | agents | tick | 2 | 60 | spawn due | none | none |
| strays | agents | slow | 2 | 120 | drained resume | none | none |
| close space | close | tick | 2 | 60 | drained resume | none | none |
| conversations | close | slow | 2 | 120 | none | none | none |
| handoff transfers | close | tick | 2 | 60 | none | none | none |
| session models | close | slow | 2 | 120 | none | none | none |
| settle | close | tick | 2 | 60 | spawn | none | none |
| heartbeat | closing | closing | 2 | 60 | none | none | none |
| last tick | closing | closing | 2 | 60 | none | none | none |
| status publish | closing | closing | 2 | 60 | health findings | none | none |
| step starvation | closing | closing | 2 | 60 | none | inbox | tick-step-starvation |
| dispatch seat | dispatch seat | tick | 2 | 60 | drained resume, sleeping | none | none |
| drained resume | drained resume | tick | 2 | 60 | master move fail over, master start watch, master wake, recover master, sleeping, spawn due, stopped branch | none | none |
| sleeping | drained resume | tick | 2 | 60 | spawn due | none | none |
| master down | master seats | tick | 2 | 60 | drained resume | none | none |
| master scale | master seats | tick | 2 | 60 | drained resume | none | none |
| seat alarm | master seats | tick | 2 | 60 | ledger state | none | none |
| tick master | master seats | tick | 2 | 60 | drained resume | none | none |
| master alarm | master watch | slow | 2 | 60 | none | none | none |
| night watch | master watch | slow | 22 | 120 | ledger state, sleeping | none | none |
| master stall | masters | slow | 2 | 60 | drained resume | none | none |
| master wake | masters | tick | 2 | 60 | ledger state, stopped branch | none | none |
| prompt stall | masters | slow | 2 | 60 | drained resume | none | none |
| retire idle master | masters | slow | 2 | 60 | spawn | none | none |
| automated cleanup | observe | tick | 2 | 60 | ledger state | none | none |
| execution reconcile | observe | tick | 2 | 60 | none | none | none |
| exits sweep | observe | tick | 10 | 60 | none | none | none |
| lab capacity | observe | slow | 3 | 60 | none | metrics | lab-capacity-reader |
| launch checks | observe | tick | 2 | 60 | spawn due | none | none |
| ledger state | observe | tick | 3 | 60 | none | none | none |
| master move fail over | observe | tick | 2 | 60 | stopped branch | none | none |
| master start watch | observe | tick | 2 | 60 | stopped branch | none | none |
| pane evidence | observe | slow | 2 | 120 | none | none | none |
| recover master | observe | tick | 2 | 60 | stopped branch | none | none |
| spawn due | observe | tick | 33 | 60 | automated cleanup, ledger state, stopped branch | none | none |
| track outages | observe | tick | 6 | 60 | spawn due | none | none |
| verify | observe | tick | 2 | 60 | spawn due | none | none |
| pass budget | pass setup | tick | 2 | 60 | none | none | none |
| spawn hold clear | pass setup | tick | 2 | 60 | none | none | none |
| bottleneck remedy | placement | tick | 2 | 60 | ledger state | none | none |
| preempt | placement | tick | 18 | 60 | drained resume, prerequisite reopen, reap, recover outages | none | none |
| rank pass | placement | tick | 7 | 60 | drained resume | none | none |
| spawn | placement | tick | 33 | 60 | launch checks, orphans, overrun pass, preempt, quiet pass, reap, track outages, verify | coordination, inbox, ledger, metrics, runtime | swarm-tick-spawn |
| codex hook order | placement acks | tick | 2 | 60 | sleeping | none | none |
| collect acks | placement acks | tick | 7 | 60 | drained resume | none | none |
| lost runner | placement notices | slow | 2 | 60 | drained resume | none | none |
| quota notice | placement notices | tick | 2 | 60 | drained resume | none | none |
| time left | placement notices | slow | 2 | 60 | drained resume | none | none |
| dev red reopen | reopen passes | tick | 2 | 60 | drained resume | none | none |
| fleet red notice | reopen passes | slow | 30 | 60 | spawn | none | none |
| prerequisite reopen | reopen passes | tick | 2 | 60 | dev red reopen | none | none |
| promotion refresh | reopen passes | slow | 2 | 60 | drained resume | none | none |
| release alert | reopen passes | slow | 2 | 60 | drained resume | none | none |
| config load | start | tick | 2 | 60 | command runner, reboot pass | none | none |
| stopped branch | start | tick | 2 | 60 | config load | none | none |
| dispatcher group | task passes | tick | 2 | 60 | drained resume | none | none |
| grouping release | task passes | tick | 2 | 60 | ledger state | none | none |
| run window | task passes | tick | 2 | 60 | drained resume | none | none |
| size pass | task passes | tick | 2 | 60 | ledger state | none | none |
| verification notices | task passes | tick | 2 | 60 | ledger state | none | none |
| orphans | tasks | tick | 2 | 60 | drained resume, reap | none | swarm-tick-events-orphans |
| command runner | wrapper commands | tick | 2 | 60 | none | none | none |
| doc items map | wrapper events | slow | 2 | 60 | drained resume, ledger state, tick view | coordination, ledger | tick-doc-items-map |
| doc report | wrapper events | slow | 2 | 600 | none | none | none |
| done recheck | wrapper events | slow | 2 | 60 | tick view | none | none |
| findings pass | wrapper events | slow | 2 | 60 | health findings | none | none |
| health findings | wrapper events | slow | 8 | 60 | tick view | none | none |
| ledger event pass | wrapper events | slow | 2 | 60 | tick view | none | none |
| merge pass | wrapper events | tick | 2 | 60 | tick view | none | none |
| overrun pass | wrapper events | tick | 2 | 60 | stall watch | none | none |
| priorities sweep | wrapper events | slow | 2 | 60 | tick view | none | none |
| progress checks | wrapper events | slow | 2 | 60 | tick view | none | none |
| progress heads | wrapper events | slow | 2 | 60 | tick view | source_control | tick-progress-heads |
| quiet pass | wrapper events | tick | 2 | 60 | none | none | none |
| stall watch | wrapper events | tick | 18 | 60 | page relay, tick view | none | none |
| waits end | wrapper events | tick | 3 | 60 | tick view | none | none |
| hive sweep | wrapper health | slow | 2 | 600 | none | coordination | hive-quiet-sweep |
| host pressure | wrapper health | slow | 2 | 120 | none | none | none |
| ledger probe | wrapper health | slow | 7 | 120 | none | none | none |
| ledger watchdog | wrapper health | slow | 2 | 120 | none | none | none |
| prompt backlog | wrapper health | slow | 2 | 120 | none | none | none |
| intent check | wrapper intent | tick | 8 | 60 | none | none | none |
| bottleneck status | wrapper metrics | slow | 2 | 120 | none | none | none |
| inbox wake | wrapper metrics | slow | 2 | 60 | page relay | none | none |
| knowledge warm | wrapper metrics | slow | 2 | 60 | none | host | tick-knowledge-warm |
| metrics record | wrapper metrics | slow | 29 | 600 | health findings | none | none |
| snapshot job | wrapper metrics | tick | 2 | 60 | none | none | none |
| dependency audit | wrapper phases | tick | 2 | 60 | none | none | swarm-ordering-dependency-audit |
| planning after phase | wrapper phases | tick | 12 | 60 | planning and phase pass | none | none |
| planning and phase pass | wrapper phases | tick | 12 | 60 | none | none | none |
| urgent hold | wrapper phases | tick | 2 | 60 | none | none | none |
| workflow runner | wrapper phases | slow | 2 | 60 | drained resume, ledger state, planning and phase pass | host | swarm-workflow-runner |
| hive ledger | wrapper probes | slow | 2 | 120 | none | none | none |
| inbox relay | wrapper probes | tick | 2 | 60 | none | none | none |
| launch slots | wrapper probes | tick | 2 | 60 | none | none | none |
| ledger health | wrapper probes | tick | 2 | 60 | none | none | none |
| outage probes | wrapper probes | tick | 2 | 60 | none | none | none |
| reboot pass | wrapper reboot | tick | 2 | 60 | none | none | none |
| refresh job | wrapper refresh | tick | 2 | 600 | none | none | none |
| outbox migration | wrapper relay | tick | 2 | 60 | none | none | none |
| page relay | wrapper relay | tick | 2 | 60 | none | none | none |
| tick view | wrapper view | tick | 3 | 60 | none | none | none |
