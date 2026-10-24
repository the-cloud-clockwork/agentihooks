---
title: Redis command map and grant check
parent: Reference
nav_order: 45
---

# Redis command map and grant check

`python -m scripts.swarm.redis_commands` prints, for each swarm consumer, the
Redis commands its code can send and the scheduler keys it writes. antoncore's
`Swarm Redis privileges (Redis 7)` CI job builds this map from agentihooks
`dev` and fails when a swarm Redis user does not grant a command or key one of
its consumers uses. The failure names the consumer, the command, the user and
the code that sends it.

The check exists because of an incident on 12 October. An antoncore change
narrowed the swarm Redis users, and the controller then crash looped in Anton
because its user lacked `INCRBY`. No check compared the narrowed users with the
commands the code sends before the change was deployed.

## The command map

The map is built from the source code, so it changes when the code changes.
No hand-kept list of commands is involved.

| Consumer | Redis role | Entry points |
|----------|-----------|--------------|
| `controller` | `controller` | `scripts.swarm.controller:main` |
| `tick` | `controller` | `scripts.swarm.scheduled_tick:main`, `scripts.swarm.cli:cmd_tick` |
| `ledger server` | `ledger` | `scripts.swarm_ledger:run`, `scripts.swarm_ledger.ledger_server:main` |
| `worker` | `worker` | `scripts.swarm_v2.worker.start:main` |
| `hooks` | `worker` | `hooks.hook_manager:main` |

The role is the key under `redis.roles` in the swarm chart values. antoncore
uses it to find the Redis user that consumer connects as.

### How commands are found

1. Every module under `scripts` and `hooks` is parsed. Each function, method
   and module body becomes one unit.
2. Starting at the consumer's entry points, the map follows every unit the
   entry points can reach. That includes functions named directly, imports
   (including imports inside a function), the module body of every module
   reached, methods called on `self` or `cls`, methods called on a name bound
   to a known class constructor, and methods whose name reached code reads on
   an object of unknown class. It also includes the dunder methods of every
   class reached.
3. In each reached unit, the map records the commands sent by:
   - a redis-py client method. The method is matched to the command it sends
     by calling it on a client that records commands instead of sending them.
     For example, `incr` sends `INCRBY` and `blpop` sends `BLPOP`;
   - a `PubSub` method on a name bound to `<client>.pubsub()`, for example
     `subscribe` sends `SUBSCRIBE`;
   - `execute_command` with a string literal as its first argument, for example
     `execute_command("XINFO", ...)` sends `XINFO`;
   - a Lua script string that calls `redis.call('<COMMAND>', ...)`;
   - the helpers `lock` (the lock's own commands, its Lua scripts and
     `SCRIPT LOAD`), `register_script` (`EVALSHA`, `SCRIPT LOAD`),
     `transaction` (`WATCH`, `MULTI`, `EXEC`) and pipeline `execute`
     (`MULTI`, `EXEC`).
4. A receiver counts as a Redis client only when its type traces back to a
   client or pipeline constructor: `Redis(...)`, `StrictRedis(...)`,
   `from_url(...)` or `<client>.pipeline()`, a function under `scripts` or
   `hooks` that returns one (resolved through imports, its own class, or a
   name only one function defines), a parameter annotated `Redis`,
   `StrictRedis` or `Pipeline`, or a chained call on a pipeline. A name a
   function binds is typed from that binding in that function only, so
   `items = []` keeps `items.append` out of the map even where another
   function holds a client named `items`. An attribute, or a parameter the
   function does not rebind, counts when the same name holds a client
   elsewhere. Loop targets, `*args` and `**kwargs` never count.

### Scheduler keys

Each consumer also carries the keys the machine scheduler writes, computed by
`Scheduler.key` for the given deployment mode with the machine name `probe`
and no hive id:

| Deployment | `scheduler entries` | `scheduler wake` |
|------------|---------------------|------------------|
| `distributed`, `compose` | `agentihooks:scheduler:probe:entries` | `agentihooks:scheduler:probe:wake:probe` |
| `local` | `agenticore:<instance>:scheduler:probe:entries` | `agenticore:<instance>:scheduler:probe:wake:probe` |

A real machine writes the same keys with its own machine name in place of
`probe`. The grant check matches these keys against each user's key
patterns. For the `controller` user, `~agentihooks:*` covers them and
`~agentihooks:swarm:*` does not.

## Usage

Print the map for the deployment Anton runs:

```bash
cd ~/dev/tcc-ecosystem/agentihooks
~/dev/tcc-ecosystem/.venv/bin/python -m scripts.swarm.redis_commands --deployment distributed > redis-commands.json
```

`--deployment` takes `local`, `compose` or `distributed` and defaults to
`distributed`. The command reads only source files. It opens no Redis
connection and needs no credentials. On a workstation it takes a few seconds.

The output is one JSON document:

```json
{
 "consumers": {
  "controller": {
   "commands": {
    "BLPOP": ["scripts.scheduler.process_timer:Timer.wait"],
    "INCRBY": ["scripts.inbox.store:InboxStore._move_address", "..."]
   },
   "keys": {
    "scheduler entries": "agentihooks:scheduler:probe:entries",
    "scheduler wake": "agentihooks:scheduler:probe:wake:probe"
   },
   "role": "controller"
  }
 },
 "deployment": "distributed"
}
```

Each command lists the units that send it as `module:function` or
`module:Class.method`, sorted. The excerpt shows one consumer. The real map
lists all five.

Read the map:

```bash
# Commands one consumer sends
jq -r '.consumers.controller.commands | keys[]' redis-commands.json

# Which code sends one command
jq -r '.consumers.controller.commands.INCRBY[]' redis-commands.json

# Command count per consumer
jq -r '.consumers | to_entries[] | "\(.key) \(.value.role) \(.value.commands | length)"' redis-commands.json
```

On `dev` at the time of writing, the command count per consumer was
controller 58, tick 57, hooks 48, ledger server 43 and worker 0: the
worker's earlier `GET`, `MULTI` and `EXEC` came from dictionaries and a SQLite
cursor read as clients, and the worker sends no Redis command. The
controller's `INCRBY` senders include `scripts.swarm.lease:acquire`, the lease
acquire behind the 12 October crash loop.

## The antoncore grant check

The `swarm-redis-authz` job in antoncore's `CI Validate` workflow, named
`Swarm Redis privileges (Redis 7)`, runs on every pull request into antoncore
`dev`, every merge group and every push to `dev`. It has no path filter, and
the workflow's final gate needs it to succeed. The job:

1. Checks out agentihooks at `dev` and installs the redis-py version pinned in
   `docker/swarm-node/requirements.lock`. The command map depends on that
   version, because each method's command is read from the installed
   redis-py.
2. Reads `deployment` from `k8s/values-overlays/agentihooks-swarm/values.yaml`
   and runs `python -m scripts.swarm.redis_commands --deployment <deployment>`.
   The map is uploaded as the `redis-commands` artifact for one day.
3. Runs `tests/unit/swarm_redis_authz` with `SWARM_REDIS_COMMANDS` pointing to
   the map. The step fails when any test is skipped, so a missing map cannot
   pass as green.

`tests/unit/swarm_redis_authz/grants.py` reads the grants:

- **Users.** Each `user` line of the `users.acl.template` entry in the
  `agentihooks-swarm-redis-tls` ConfigMap gives that user's root permissions.
  These are `on`/`off`, `+command`, `-command`, `+command|subcommand`,
  `+@all`/`allcommands`, `-@all`, `~pattern`, `allkeys` and `resetkeys`.
  Selectors in parentheses are ignored.
- **Role to user.** For each `redis.roles.<role>` in the overlay, the check
  follows the named ExternalSecret and key to the connection URL template and
  takes the user name from `redis://<user>:` or `rediss://<user>:`.

`test_consumer_grants.py` then checks:

| Test | Passes when |
|------|-------------|
| `test_the_map_was_published_for_the_deployment_anton_runs` | The map's deployment equals the overlay's, and every consumer role is a chart role |
| `test_every_consumer_command_and_scheduler_key_is_granted_to_its_redis_user` | No consumer command or scheduler key is refused |
| `test_a_planted_command_removal_from_the_controller_user_is_refused` | Removing `+incrby`, and separately `+blpop`, from `swarm-controller` produces a refusal for that command |
| `test_a_planted_key_narrowing_of_the_controller_user_is_refused` | Narrowing `swarm-controller` from `~agentihooks:*` to `~agentihooks:swarm:*` refuses the scheduler entries key |

The planted cases show that the check can fail. `INCRBY` is the grant behind
the 12 October crash loop. `BLPOP` and the scheduler entries key are the two
missing grants fixed by antoncore pull request 954. agentihooks pull request
4832 keeps the hosted controller's scheduler keys under `agentihooks:`.

### Refusal lines

A failing run prints one line per refusal:

```text
controller issues INCRBY but Redis user swarm-controller does not grant it; sent by scripts.inbox.store:InboxStore._move_address
controller writes its scheduler entries key agentihooks:scheduler:probe:entries outside the keys of Redis user swarm-controller
```

A command line names the first sending unit. Look up the rest in the
`redis-commands` artifact.

## Configuration

| Setting | Where | Effect |
|---------|-------|--------|
| `--deployment` | `redis_commands` flag | Deployment mode used to compute the scheduler keys. Default `distributed` |
| `deployment` | antoncore `k8s/values-overlays/agentihooks-swarm/values.yaml` | The mode the CI job passes as `--deployment` |
| `redis.roles.<role>.name`, `.key` | Same overlay | The ExternalSecret and key holding each role's Redis URL, and through it the user name |
| `users.acl.template` | antoncore `k8s/manifests/agentihooks-swarm/redis.yaml` | The user grants that the check reads |
| `SWARM_REDIS_COMMANDS` | antoncore CI step environment | Path to the map. The grant tests skip without it, and the step fails on any skip |

A new consumer needs an entry in `CONSUMERS` in `scripts.swarm.redis_commands`
with its role and entry points. A new role also needs a matching
`redis.roles` entry in the overlay.

## Limits

- `execute_command` with a computed first argument, and a Lua script passed
  as anything but a string literal containing `redis.call('...')`, add no
  command. Write the command name as a literal.
- The map follows imports under `scripts` and `hooks`. Code reached only
  through another installed package is not traced.
- Method resolution follows names, so the map can list a command that a
  consumer never sends at run time. It may over-report commands, but every
  traced call path is included.
- A redis-py method that no sample call completes raises `LookupError` naming
  the method, so the map fails to build. It never drops the command without
  warning.
- The ACL reader raises `ValueError` on category rules other than `+@all` and
  `-@all` (such as `+@read`) and on `%` key permission rules, because it does
  not model them.
- Only root permissions are checked. Grants inside selectors do not count.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `<consumer> issues <COMMAND> but Redis user <user> does not grant it` | The user in `users.acl.template` lacks the command, or denies it with `-command` | Add `+<command>` to that user's line in antoncore, or remove the code path from that consumer in agentihooks |
| `<consumer> writes its <label> key <key> outside the keys of Redis user <user>` | No `~pattern` on the user matches the scheduler key | Widen the user's key pattern, or keep the scheduler key under the user's prefix |
| `test_the_map_was_published_for_the_deployment_anton_runs` fails | The overlay's deployment changed, or a consumer role has no `redis.roles` entry | Pass the overlay's deployment, or add the role |
| Step fails with `N skipped` | `SWARM_REDIS_COMMANDS` was unset or the map step did not run | Read the `Publish the Redis commands each agentihooks consumer issues` step log |
| `ValueError: ... category rule ... is not modelled` | The ACL template uses a category rule | List the commands one by one |
| `LookupError: no sample call of ...` | A redis-py upgrade changed a method signature so that no sample call completes | Extend the samples in `scripts.swarm.redis_wire` |
| A new command appears and turns antoncore red with no antoncore change | The job checks out agentihooks `dev`, so a merged agentihooks change that sends a new command fails the next antoncore run | Grant the command in antoncore. The map artifact names the code that sends it |

## Delivery evidence

- agentihooks pull request 4917 added the map, and pull request 5039 made each
  unit's syntax tree walk once.
- antoncore pull request 964 added the grant check and the planted red cases.
- On 23 October, antoncore `dev` run 39912385457 (job 120278104620) built the
  map from agentihooks `c62851062b` and passed `tests/unit/swarm_redis_authz`
  with `53 passed`.
- On the same day, the map built locally from agentihooks `dev` listed
  `INCRBY` and `BLPOP` for the controller and finished in 6.9 seconds.
