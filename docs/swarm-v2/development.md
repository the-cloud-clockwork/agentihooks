# Swarm v2 development and test environment

Work package SV2-FND-05. A test run, local or in CI, must not write the operator's live agent homes, brain vault,
Redis keys or deployment. This page is the contract every later Swarm v2 package builds on.

## Canonical Python and checks

- The interpreter is the shared workspace venv, `~/dev/tcc-ecosystem/.venv`. Call its binaries directly.
  Never `uv run --active` or `uv sync --active` against it: it is shared with the other editable repositories.
- Install or update: `uv pip install --python ~/dev/tcc-ecosystem/.venv/bin/python -e ".[all]"`.
- Lint and format fail independently and CI runs both:
  `~/dev/tcc-ecosystem/.venv/bin/ruff check .` and `~/dev/tcc-ecosystem/.venv/bin/ruff format --check .`.
- Locally, run only the test files a change touches, with at most two workers:
  `~/dev/tcc-ecosystem/.venv/bin/python -m pytest -n 2 <files>`. CI runs the full suite, sharded, and is the
  judge of it; many agents share the workstation.

## What every test gets

The autouse fixture `_isolate_real_user_paths` in `tests/conftest.py`, backed by `tests/swarm_v2_isolation.py`
and `tests/installer_isolation.py`, gives every test:

| Surface | Isolation |
|---|---|
| Agent homes | `HOME` and `Path.home()` point at a temporary home; `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `COPILOT_HOME`, `AGENTIHOOKS_HOME` and `HERDR_CONFIG_PATH` are unset, `XDG_CONFIG_HOME` points into the temporary home |
| Brain and vault | `AGENTIBRAIN_HOME` points into the temporary home; `VAULT_ROOT` is unset |
| Kubernetes | `KUBECONFIG` points at a path inside the test directory |
| Swarm session | every inherited `AGENTIHOOKS_*` variable is removed, so a run from an agent's shell matches CI |
| Redis | connections to port 6379 are refused and the swarm Redis URL is a missing socket; keys built by the `hooks` package (`hooks._redis`, the memory store, the event relay) carry the run prefix `agentihooks-test-<run id>`. The swarm, inbox and gate stores under `scripts/` keep their production `agentihooks:*` names and are tested against a per-test fakeredis |
| herdr | `herdr_host.binary()` and `herdr_setup.binary()` report no herdr, as in CI |
| Live writes | any write into a live root, from any code, aborts the test |
| Live programs | spawning a real `kubectl`, `helm`, `argocd` or `herdr`, one that resolves outside the test directory, aborts the test: directly, behind `env`, `timeout`, `nohup`, `sudo`, `exec` and similar wrappers, inside a shell `-c` command or substitution, or through `os.system`. `os.spawn*` raises no audit event and is not seen |

The live roots are read once, from the environment the run started with: the home names `.claude`, `.claude.json`,
`.codex`, `.copilot`, `.agentihooks`, `.agents`, `.agentibrain`, `.kube`, `.config/herdr`, `.bashrc`, `.local/bin`
and `.config/systemd/user`, plus every absolute path named by `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_HOME_DIR`,
`AGENTIHOOKS_CLAUDE_HOME`, `CODEX_HOME`, `COPILOT_HOME`, `AGENTIHOOKS_HOME`, `AGENTIBRAIN_HOME`, `VAULT_ROOT`,
`KUBECONFIG`, `HERDR_CONFIG_PATH` and `XDG_CONFIG_HOME/herdr`.

The operator's ledger folder and the shared ledger port are kept out by `tests/ledger_guard.py`.

## Fakes for Kubernetes and herdr

No default `kubectl` fake is installed: with no real binary on `PATH` (CI) the call fails as missing, and with one (a workstation) the call aborts. A test that needs either program writes an executable fake into its own `tmp_path` and puts that directory first on
`PATH`. The guard lets a program run only when it resolves inside the test directory; a fake that is a symlink to a
real binary resolves outside it and is refused. A test of herdr logic can instead patch `herdr_host.binary`.

## Fixture identities

`tests.swarm_v2_isolation.build(root, run_id=None)` returns an `Identities` record for packages that need a full
temporary installation:

- `claude_home`, `codex_home`, `brain_home`, `vault` (inside the brain home), `archive` and `kubeconfig`, all under
  `root`, with a fixture-only kube context and namespace `agentihooks-test-<run id>` whose server is unreachable;
- `redis_url`, a socket inside `root` on database 15, and `redis_prefix`, `agentihooks-test-<run id>`; the socket path, database index and prefix are the fixture's Redis boundary, and no ACL user is created;
- `traps`: a symlink to the real `~/.claude`, a `..` escape out of `root` and an environment value naming the real
  `~/.codex`, for rejection tests;
- `environ()`, the variables that steer the live resolvers into the fixture.

Checks, each of which aborts the test and counts `test_live_path_rejections_total` by dimension in
`swarm_v2_isolation.REJECTIONS`:

| Call | Refuses |
|---|---|
| `confine(root, label, path)` | a path that resolves outside `root` (`escape`) |
| `confine_environ(identities, environ)` | an environment whose steering values leave the fixture, or a foreign key prefix |
| `owned(identities, key)` | a Redis key outside the run prefix, so production names such as `agentihooks:swarm:*` are never touched (`redis`) |
| `sweep(redis, identities)` | a sweep by a run that does not own the prefix's owner marker (`redis`) |
| `remove(identities)` | a root whose `.fixture-run` marker names another run, or a live root (`cleanup`) |

Writes into live roots count under `write`, live programs under `program`. The autouse fixture records each test's nonzero counts as the junit property `test_live_path_rejections_total`, which `evidence/SV2-FND-05/generate_case_results.py` sums per case.

## Recovery

Every run and every `build` call takes a new run id, so lock and key names never repeat across runs. An interrupted
run leaves its own prefixed locks and its own temporary root; the next run neither sees nor consumes them, and a
sweep or removal deletes only resources whose owner marker matches the run that asks.

## Rollback

The package touches tests, fixtures and documentation only. Reverting its pull request restores the previous suite
guards; no stored state outside the repository depends on it.
