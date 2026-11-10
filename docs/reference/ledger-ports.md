---
title: Ledger ports and adapters
parent: Reference
nav_order: 78
---

# Ledger ports and adapters
{: .no_toc }

The ledger package, `scripts.swarm_ledger`, reaches the swarm (`scripts.swarm`, `scripts.swarm_v2`), the hive
(`scripts.hive`) and the hooks (`hooks`) only through three interfaces it owns: `Swarm`, `Hive` and `Hooks` in
`scripts.swarm_ledger.ports`. Adapters under `scripts.swarm_ledger.adapters` implement them, and the composition root
`scripts.swarm_ledger.composition` binds them when a ledger entry point starts. A unit test refuses any ledger module
outside the adapters that imports one of those packages.

1. TOC
{:toc}

---

## Layout

| Part | Module | Role |
|---|---|---|
| Ports | `scripts.swarm_ledger.ports` | The `Swarm`, `Hive` and `Hooks` protocols, the `BOUND` table, `bind()` and the accessors `swarm()`, `hive()` and `hooks()` |
| Adapters | `scripts.swarm_ledger.adapters.swarm`, `.hive`, `.hooks` | One class per port; each property imports the real module when it is read and returns it |
| Other adapters | `scripts.swarm_ledger.adapters.hive_ledger`, `.safety_nudge` | Re-export the hive ledger gateway and `report` from `scripts.swarm.hive_ledger`, and record a joining master in the safety nudge registry; the ledger CLI imports these two directly |
| Composition root | `scripts.swarm_ledger.composition` | `adapters()` builds one adapter per port; `wire()` binds them |
| Import check | `tests/swarm_ledger/test_ledger_boundary.py` | Fails CI when a ledger module outside `adapters` imports the swarm, hive or hooks packages |
| Port tests | `tests/swarm_ledger/test_ledger_ports.py` | Prove each adapter satisfies its protocol and that ledger code calls whatever is bound |

## The three ports

Each protocol lists the members the ledger reads. A member is a module handed out by the adapter, so ledger code
keeps the call shape it used before: `ports.swarm().store.connect(environ)` in place of
`from scripts.swarm import store`.

| Port | Members | Adapter delegates to |
|---|---|---|
| `Swarm` | `naming`, `store`, `commands`, `lifecycle`, `freeze`, `effort_range`, `claim_hold`, `idle`, `lease`, `model_catalog`, `default_models`, `quota_refresh`, `operator_mail`, `overlays`, `verdicts`, `phase_state`, `affinity`, `templates`, `stack_env`, `stack_init`, `front_door`, `ledger_writer`, `master_fence`, `masters`, `architecture`, `operator_auth`, `task_specs`, `authority`, `ledger_cutover` | Modules under `scripts.swarm` and `scripts.swarm_v2` |
| `Hive` | `auth`, `registry`, `launch`, `reports`, `headroom`, `herdr_stats` | `scripts.hive.<member>` |
| `Hooks` | `account_sessions`, `config`, `conditions`, `filters`, `operator_words`, `classifier`, `profile_chain`, and the method `redis()` | `hooks.context`, `hooks.config`, `hooks.filters.check`, `hooks.classifier` with its `code_rules` and `runner`, and `hooks._redis.get_redis()` |

The swarm adapter also hands out `context_view`, which the brain context route reads; the `Swarm` protocol does not
list it, so a fake swarm bound in a test needs it only when the test reaches that route.

Adapter properties import on read, so importing the ledger package loads none of the swarm, hive or hooks modules
until a ledger call needs one. The one exception is `adapters.hive_ledger`, which the ledger CLI module imports at
module level and which loads `scripts.swarm.hive_ledger` with it.

## Binding

`ports.bind(**adapters)` stores adapters in `BOUND` by port name. `ports.swarm()`, `ports.hive()` and `ports.hooks()`
read `BOUND` at call time, never at import, so a binding made after a module was imported still takes effect.

When a port is read and its name is missing from `BOUND`, the accessor fills every missing port from
`composition.adapters()` and keeps any port already bound. A test that binds only `hooks` still gets the real swarm
and hive adapters.

These entry points call `composition.wire()` before they run:

| Entry point | Where it wires |
|---|---|
| `agentihooks ledger` and its `new`, `storage`, `serve`, `watch`, `chat` and `proof` tools | `scripts.swarm_ledger.run()` |
| `python -m` or script runs of the ledger CLI, `new_ledger`, `ledger_server` and `ledger_hook` | Each module's `__main__` block |
| The duplicate finder subprocess | `ledger_duplicates` `__main__` block |
| The hosted ledger storage migration | `storage_migration.hosted` `__main__` block |

Other `__main__` blocks in the package, such as `chat_ledger`, `watch_ledger`, `ledger_health` and the storage
migration's `__main__`, do not wire. Run through `agentihooks ledger` they are wired by `run()`; run directly they read
ports through the fallback above and get the real adapters.

Some API modules under `scripts.swarm_ledger.api` copy port members into module constants when they are imported:
`api.hives` keeps `registry`, `HiveError` and `SwarmError`, `api.proofs` keeps `naming`, `templates` and `CODEX_TRANSPORTS`. A module
imported before a `bind()` keeps the members it copied, and the later binding does not replace them. A test that binds
a fake for those members binds it before the API module is first imported.

## Binding a fake in a test

Reset `BOUND` for the test, bind a stand-in for the one port the code path reads, and call the ledger function:

```python
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_authority, ports


@pytest.fixture
def unbound(monkeypatch):
    monkeypatch.setattr(ports, "BOUND", {})


def test_the_ledger_connects_through_the_bound_swarm_store(unbound):
    ports.bind(swarm=SimpleNamespace(store=SimpleNamespace(connect=lambda environ=None: ("store", environ))))

    assert ledger_authority.connect({"SWARM": "x"}) == ("store", {"SWARM": "x"})
```

The stand-in needs only the attributes the path reads. Ports left unbound fall back to the real adapters.

## Adding what the ledger needs

When a ledger module needs another swarm, hive or hooks module:

1. Add a property to the adapter class in `scripts/swarm_ledger/adapters/<port>.py` that imports the module inside
   the property and returns it.
2. Add the member to the matching protocol in `scripts.swarm_ledger.ports`.
3. Call it from ledger code as `ports.<port>().<member>`.

```python
# scripts/swarm_ledger/adapters/swarm.py
@property
def naming(self):
    from scripts.swarm import naming

    return naming
```

```python
# a ledger module
from scripts.swarm_ledger import ports


def _block_author(by: str) -> bool:
    lane_of = ports.swarm().naming.lane_of

    return by == "operator" or lane_of(by) == "master"
```

A module from a package other than the three stays importable directly; the import check covers only the swarm, hive
and hooks packages.

## The import check

`test_the_ledger_imports_no_swarm_hive_or_hooks_module_outside_its_adapters` walks every `.py` file under
`scripts/swarm_ledger` outside `scripts/swarm_ledger/adapters` and fails with the list of offending files. A name is
foreign when it matches `scripts.swarm`, `scripts.swarm_v2`, `scripts.hive` or `hooks`, alone or followed by a dot.
The check reads:

- `import` statements, aliased or not, such as `import scripts.hive.auth as auth` and `import hooks`;
- `from` imports, such as `from scripts.swarm.store import SwarmError`;
- `importlib.import_module("...")` and `__import__("...")` calls with a constant name.

Lookalike names pass: `scripts.swarmish` and `hookshot` are not foreign, and `scripts.swarm_ledger` imports are
always allowed. A second test requires `swarm.py`, `hive.py` and `hooks.py` in `adapters`, each importing a swarm,
hive or hooks package.

The detector reuses the `dotted` and `imported` helpers from `tests/swarm_ledger/test_storage_composition.py`; a
change to what counts as an import is made there. `from scripts import swarm` is detected as well, since `from`
imports are read as the module plus each imported name.

The check runs in the test suite in CI. Local test runs are blocked by default; push and read the result on the pull
request.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `test_the_ledger_imports_no_swarm_hive_or_hooks_module_outside_its_adapters` fails naming a ledger file | That file imports a swarm, hive or hooks module, including through `importlib.import_module` | Add the module as an adapter member and call it through `ports` |
| `AttributeError` on a member of a bound fake in a test | The stand-in lacks an attribute the code path reads | Add that attribute to the stand-in, or leave the port unbound to use the real adapter |
| A test sees the real adapter after `bind` | `BOUND` was not reset, or the value was read through another port | Use the `unbound` fixture and bind the port the code path reads |
| `isinstance(adapter, ports.Swarm)` is false | The adapter lacks a member listed in the protocol | Add the property to the adapter class |
| A test's fake is ignored by an API module constant such as `api.hives.SwarmError` | The API module copied the port member into a constant when it was first imported, before the test bound the fake | Bind the fake before the API module is first imported, or patch the module constant |
