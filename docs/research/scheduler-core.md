# Scheduler core engine choice

AgentiHooks uses a small asyncio runner with Redis schedule and delivery state, and the maintained `croniter` pip dependency for cron expressions. APScheduler was evaluated with its Redis job store. It provides scheduling and coalescing, but its execution and persistence boundaries do not provide the durable signal delivery required here.

Evidence was retrieved from the primary sources below on 17 October 2026. Package versions and release dates are observations at that time, rather than a requirement to keep those versions indefinitely.

## Comparison

| Requirement | APScheduler 3.x with RedisJobStore | asyncio with Redis and croniter |
| --- | --- | --- |
| Maintained distribution | PyPI served APScheduler 3.11.3, uploaded 28 June 2026; its Redis extra depends on redis. [1] | asyncio is part of Python. PyPI served croniter 6.2.4, uploaded 10 July 2026; its only declared runtime dependency was python-dateutil. croniter identifies itself as a Pallets Community Ecosystem project. [2][3] |
| Timers and crons | Date and cron triggers are built in. A stopwatch limit can be represented by a date trigger. [4] | asyncio supplies delayed callbacks; croniter calculates the next datetime from a cron expression. Timer and stopwatch deadlines are stored explicitly. [3][5] |
| Durable entries | RedisJobStore stores pickled jobs in a Redis hash and their next run times in a sorted set. [6] | AgentiHooks stores schedules, named connections and pending deliveries as explicit Redis data; these domain records are required regardless of the wakeup engine. |
| Missed executions | `coalesce=True` combines missed executions into one; `misfire_grace_time=None` allows execution regardless of lateness. [4][7] | The runner materializes one pending occurrence for an overdue entry and advances a cron beyond the recovery time, preserving one recovery emission per overdue entry. |
| Single runner and external clients | The FAQ forbids sharing a persistent job store between scheduler processes and recommends a dedicated scheduler with remote access. [8] | One agentihooks supervisor owns the local runner. A per-machine Redis lease fences scheduling work; producers update durable entries. |
| Delivery crash boundary | `_process_jobs` submits a job to an executor and then advances or removes its stored schedule, without waiting for the callable to finish. [9] | A pending occurrence remains durable until its connected slots have acknowledged delivery. Stable delivery identities permit transport deduplication on retry. |
| Cron conventions | APScheduler 3.x explicitly warns that numeric weekday zero means Monday rather than traditional crontab Sunday. [10] | croniter documents the standard five fields and the default OR relationship between restricted day-of-month and weekday fields. [3] |

The asyncio column describes the selected AgentiHooks design where it refers to Redis records, delivery identities or supervision. Those are application responsibilities, not guarantees supplied by asyncio or croniter. Python documents that its event loop uses a monotonic clock and calls each registered delayed callback once within that event loop; that statement does not imply persistence across process death. [5]

## Decision and boundaries

Use asyncio only to drive repeated checks of durable Redis state. Persist absolute deadlines for restart recovery; a process-local event-loop timestamp cannot serve as a durable deadline. Use croniter for expression parsing and next-fire calculation, with an explicit timezone policy and timezone-aware datetimes when local calendar time is used. croniter documents timezone-aware initialization for daylight-saving behavior. [3]

The public model follows Qt's event model: an object emits a named signal, and consumers connect slots to that signal. Qt documents that one signal may connect to multiple slots and that a slot responds to a signal. QTimer demonstrates connecting `timeout` to slots and using a single-shot timer. This is an interface model; PySide is not a dependency. [11][12]

Create an entry, connect its named signal to every consumer, then call Entry.start(). Factories persist entries in the ready state; start atomically sets the stopwatch epoch and relative timer deadline after connections are complete, while preserving an explicit absolute deadline and calculating the next cron occurrence from activation. A zero delay or overdue absolute entry therefore cannot race registration. Timer entries emit once at their deadline. Stopwatch entries emit when elapsed time reaches their configured limit. Cron entries emit on their expression; after downtime, one recovery emission represents the overdue entry rather than replaying every elapsed cron interval. Named connections and pending slot deliveries survive runner restart.

Run one supervisor per machine, started by agentihooks. It owns and restarts a runner child. Startup serialization uses a different file lock from supervisor ownership, so the child can acquire its ownership before spawning returns. A machine-local file lock prevents duplicate supervisors; a per-machine Redis lease protects scheduling ownership and must be checked when claiming or advancing work. This design uses neither machine cron nor systemd timers nor at. The supervisor and lease are AgentiHooks responsibilities; APScheduler would still require ownership and process supervision.

The dispatch lease renews while slots run. Claiming, acknowledging and advancing watch the lease key and the entry together, so a lost owner cannot commit a state transition. Cancellation removes pending work and remains terminal even when a slot cancels its own cron. Redis atomically materializes the pending occurrence and removes the entry from the due set. Once its slots acknowledge delivery, the runner completes the entry or advances a cron beyond the recovery time. Pending delivery state is separate from schedule advancement, so a process death after claiming an entry does not discard its signal. Redis documents atomic Lua script execution, which can support these state transitions when all affected records share the same Redis transaction boundary. [13]

Inbox delivery uses a stable item identity derived from the occurrence and connection. BroadcastSlot publishes through an atomic SQLite receipt beside the existing broadcast storage. The broadcast reader merges active scheduled publications with ordinary messages; clearing or expiry retains a receipt tombstone so a retry cannot publish the occurrence again. Each adapter must recognize a retry after the transport accepted the delivery but before the scheduler acknowledged it. A custom slot that produces effects outside the same transaction must implement idempotency using the delivery identity; arbitrary external effects cannot become exactly once merely because a scheduler retries them.

APScheduler could provide useful trigger calculation and ordinary missed-run coalescing. Its Redis persistence alone does not close the executor crash gap: death after the schedule is removed but before execution loses a one-shot, while death after an effect but before advancement can repeat it. These are conclusions from the observed submit-then-update ordering, not an upstream claim of an exactly-once guarantee. [9] Adding the required journal, stable identities, supervisor and remote producer boundary would leave most domain logic custom while retaining a second persistence model. The smaller asyncio orchestration layer therefore fits this contract better.

## Sources

1. [APScheduler package metadata and release files](https://pypi.org/pypi/APScheduler/json).
2. [croniter package metadata and release files](https://pypi.org/pypi/croniter/json).
3. [croniter maintained project README: community maintenance, usage, cron fields, and DST](https://github.com/pallets-eco/croniter/blob/main/README.rst).
4. [APScheduler user guide: components, triggers, persistent jobs, missed executions and coalescing](https://apscheduler.readthedocs.io/en/3.x/userguide.html).
5. [Python asyncio event loop: scheduling delayed callbacks](https://docs.python.org/3/library/asyncio-eventloop.html#scheduling-delayed-callbacks).
6. [APScheduler RedisJobStore implementation](https://github.com/agronholm/apscheduler/blob/3.x/src/apscheduler/jobstores/redis.py).
7. [APScheduler Job contract: misfire grace time](https://github.com/agronholm/apscheduler/blob/3.x/src/apscheduler/job.py).
8. [APScheduler FAQ: sharing a job store between processes](https://apscheduler.readthedocs.io/en/3.x/faq.html#how-do-i-share-a-single-job-store-among-one-or-more-worker-processes).
9. [APScheduler BaseScheduler execution and update ordering](https://github.com/agronholm/apscheduler/blob/3.x/src/apscheduler/schedulers/base.py), `_process_jobs`.
10. [APScheduler CronTrigger crontab interpretation warning](https://github.com/agronholm/apscheduler/blob/3.x/src/apscheduler/triggers/cron/__init__.py), `from_crontab`.
11. [Qt for Python signals and slots](https://doc.qt.io/qtforpython-6/overviews/qtcore-signalsandslots.html).
12. [Qt for Python QTimer](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QTimer.html).
13. [Redis Lua scripting atomicity](https://redis.io/docs/latest/develop/programmability/eval-intro/).
