---
title: CI tool install pins
parent: Reference
nav_order: 44
---

# CI tool install pins

Every tool CI installs must be pinned to one exact version that a checked in
constraint file pins too. A walking test in the unit suite reads every
`pip install`, `uv pip install`, `uvx`, `uv tool` and `pipx` command that CI
can run and fails on any install that floats. A floating install lets a new
upstream release change CI without a commit: on 20 November an unpinned
`platformdirs` release broke the `pip-audit` run through `uvx`, and every pull
request's lint failed for hours.

The test is
`test_every_ci_tool_install_pins_an_exact_version_under_a_checked_in_constraint`
in `tests/test_ci_test_dependencies.py`, under feature map entry
`ci-tool-installs-pinned-walk`. Installs that floated when the test landed are
listed in `FLOATING_INSTALLS` in the same file. Three deliberate clean install
smokes are listed in `INTENTIONAL_SMOKES`, each with a reason.

## What the walk reads

The walk starts from these files:

- every `.yml`, `.yaml`, `.sh` and `.py` file under `.github/`: workflows,
  composite actions, scripts and step templates;
- every such file named `scripts/ci_*`, or under a `scripts/ci_*` folder, such
  as the step templates `scripts/ci_reuse_job.yml` and
  `scripts/ci_duration_provenance.yml`.

It then follows the scripts those files run, and repeats until no new file
appears:

| A YAML or shell file contains | The walk adds |
|---|---|
| A path ending `.sh`, optionally behind a `$VAR/` prefix | That script, found from the repository root or from the file's own folder |
| `python -m package.module` or `python3 -m package.module` | `package/module.py`, or `package/module/__main__.py` |
| `python path/to/file.py`, also `python3.12 -u path/to/file.py` | That file, from the repository root |

A path containing `..`, or one that does not exist, is skipped. Python files
are read for installs, but the scripts they start are not followed.

In YAML, every string `run:` value at any depth is read. `env:` maps apply to
the runs beneath them, so a workflow, job or step variable is known to the
step's commands.

In shell, a backslash at the end of a line joins it to the next. Lines are
split into commands at `&&`, `||`, `;`, `|`, `&` and parentheses, and
redirect targets are dropped. A line made only of `NAME=value` words, with or
without `export`, sets those variables for the commands that follow in the same
run or script. `NAME=value` words in front of an installer apply to that
command only.

## What counts as a tool install

The command word is matched by its last path segment, so `.venv/bin/pip` counts
as `pip`. The installer is found at any word of the command, so a wrapper such
as `bash .github/scripts/pypi-retry.sh "Install" -- uv pip install ...` is
still graded.

| Kind in the key | Matches |
|---|---|
| `pip install` | `pip`, `pip3` or `pip3.12` followed by `install`, including `python -m pip install` |
| `uv pip install` | `uv pip install` |
| `uvx` | `uvx` |
| `uv tool run`, `uv tool install` | `uv tool run`, `uv tool install` |
| `pipx run`, `pipx install` | `pipx run`, `pipx install` |

A command is graded only when it names something to install: a positional
target, or one of `--with`, `--from`, `--spec`, `--with-executables-from`,
`-e`, `--editable`, `--with-editable`, `-r`, `--requirement`,
`--requirements` or `--with-requirements`. These are not graded:

- `command -v uvx`, a bare `uvx`, or `pipx run` with nothing after it;
- `uv pip compile`, `pip download` and other subcommands;
- text inside one quoted word, such as
  `echo '::error::pin it on the pip install line'`.

For `uvx`, `uv tool run` and `pipx run`, only the first positional word is the
tool; the words after it are the tool's own arguments. When `--from` or
`--spec` names the package, the positional word is the command name, and the
option value is graded instead.

## The pass rule

An install passes when every package it names carries one exact version, and
a checked in constraint file passed to the same command pins that same version.

**Named packages** are the positional targets, the values of `--with`,
`--from`, `--spec` and `--with-executables-from`, and the values of `-e`,
`--editable` and `--with-editable` that are not paths. Each must parse as a
requirement with exactly one specifier, `==` or `===`, without a `*`
wildcard. `pyyaml==6.0.3` passes. `pyyaml>=6.0`, `PyYAML`,
`'pytest>=8.0,<9.2'` and `pytest==9.*` float.

**Path targets** name no package and need no version: a target starting with
`.`, `/` or `~`, containing `/`, ending `.whl`, `.tar.gz` or `.zip`, a
`file://` URL, or a direct reference such as
`'agentihooks[dev,all] @ file://$WHEEL'`. A path target still needs a
constraint, so its dependencies resolve to pinned versions. Any other URL,
such as `git+https://...`, floats.

**Variables** in arguments, written `$NAME` or `${NAME}`, are replaced with
values the walk knows: `env:` maps above the step, earlier assignments in the
same script, and assignments in front of the command. A variable it cannot
resolve stays as written, so `"semgrep==$SEMGREP_VERSION"` floats unless
`SEMGREP_VERSION` is set where the walk can see it.

**Constraint files** come from:

- the values of `-c`, `--constraint` and `--constraints`;
- the values of `-r`, `--requirement`, `--requirements` and
  `--with-requirements`, which then act as the constraint too;
- `-c`, `--constraint` or `--constraints` inside a `pipx --pip-args`
  string;
- the `UV_CONSTRAINT` variable for the `uv pip install`, `uvx` and `uv tool`
  kinds, and `PIP_CONSTRAINT` for the `pip` and `pipx` kinds. Several files
  separated by spaces are allowed.

Only these files count:

| Constraint | Counts when |
|---|---|
| `.github/test-requirements.lock` | Always |
| `.github/test-constraints.txt` | It holds the exact line `--constraint test-requirements.lock` |
| A file directly under `.github/ci-tools/` | Always; a file in a subfolder does not count |

The path may carry a prefix that ends in `/`, such as
`"$RUNNER_TEMP/workflow/.github/test-requirements.lock"`. The file must exist
at the matching path in the checkout. Any other file, such as a
`requirements.txt` or a temporary file, gives `no checked in constraint`.

The pins of a constraint file include the pins of a `-c` or `-r` file named
on one of its lines, resolved from the file's own folder, one level deep. So
`.github/test-constraints.txt` supplies its own pins plus those of
`.github/test-requirements.lock`.

**Every line of a constraint file must be an exact pin.** The walk reads each
file an install names, with `.github/test-requirements.lock` read in place of
`.github/test-constraints.txt`, and follows nested `-r` lines without limit.
Comments and trailing backslashes are ignored. It reports:

- a requirement line without one exact `==` pin;
- an `-e` or `--editable` line;
- a nested `-r` line whose file is missing.

**A requirements file passed with `-r`** must be `pyproject.toml` or one of the
accepted locks other than `.github/test-constraints.txt`. Any other `-r` file
fails with `requirements file <value> is not a checked in lock`.

## Python sources

Python files are parsed, never run. Every list or tuple display whose words
include `install`, `uvx`, `pipx` or `tool` is read as a command. Its items
resolve as follows:

| Item | Read as |
|---|---|
| A string literal | That string |
| A name, or `*name` | The string or list that name holds, when the module binds the name exactly once |
| `str(x)`, `Path(x)`, `os.fspath(x)` | `x` |
| `a / b` | The first words of `a` and `b` joined with `/` |
| Anything else | Its source text, which then floats if it names a package |

A name bound once at any level of the module resolves, as does a string
imported with an absolute `from package.module import NAME` from a module in
the repository. A name bound twice, rebound in a loop or shadowed by a
function parameter does not resolve.

A Python command carries no environment, so `UV_CONSTRAINT` and
`PIP_CONSTRAINT` are never seen there. Pass the constraint as a literal
`--constraint` argument:

```python
AUDIT_LOCK = ".github/ci-tools/audit.lock"

command = ["uvx", "--constraint", AUDIT_LOCK, "pip-audit==2.10.1", "--format", "json"]
```

## The floating install list

`FLOATING_INSTALLS` holds one key per floating install that existed when the
test landed. A key is the repository relative file, the kind and the
arguments exactly as written, unexpanded, joined with `shlex.join`:

```text
<path>: <kind> <arguments>
```

For example:

```python
FLOATING_INSTALLS: tuple[str, ...] = (
    ".github/workflows/access-audit.yml: pip install 'pyyaml>=6.0'",
    ".github/workflows/semgrep.yml: pip install 'semgrep==$SEMGREP_VERSION'",
    "scripts/ci_release.sh: pip install build",
)
```

The test counts keys. Several steps that run the same command each need their
own copy of the key. It then compares the measured keys, without the
intentional smokes, with the list in both directions:

- A floating install not on the list fails with
  `floating CI tool install, pin it: <key> (<reasons>)`.
- A listed key that no longer floats fails with
  `no longer floating, remove it from FLOATING_INSTALLS: <key>`.

The list only shrinks. A pull request that pins an install removes its key in
the same change, and a new install must be pinned before it merges. The test
holds the list equal to what it measures, so it cannot tell an added key from
an old one. Reviewers refuse a pull request that adds a key.

## Intentional install smokes

`INTENTIONAL_SMOKES` maps the key of an install that must resolve the way a
user's install does to the reason it stays unconstrained:

| Install | Reason |
|---|---|
| `.github/workflows/publish-pypi.yml: pip install --no-cache-dir 'agentihooks==$VERSION'` | Installs the release just published into a clean venv from the package index, as a user does, to prove it |
| `.github/workflows/test.yml: uv pip install --python '$RUNNER_TEMP/install-smoke-venv/bin/python' '$wheel[all]'` | Installs the built wheel into a clean venv with the resolution a user gets from the package index |
| `scripts/packaging/swarm-init-smoke.sh: uv pip install --quiet --python '$work/venv/bin/python' .` | Installs the checkout into a fresh venv with its natural resolution to prove a new user's first swarm init |

`test_each_intentional_install_smoke_is_a_measured_install_with_a_reason`
requires each key to match exactly one measured install, each install to be
reported as `no checked in constraint`, and each reason to be non blank. When
a smoke's command changes, its key changes with it: update the key in the same
pull request. A smoke that gains a constraint is no longer a smoke and must
leave the map.

## Pin an install

1. Find a lock in `.github/ci-tools/` that pins the tool, and use its version:

   | Lock | Pins, with their dependencies |
   |---|---|
   | `parser.lock` | `pyyaml==6.0.3` |
   | `audit.lock` | `pip-audit==2.10.1`, excluding `platformdirs` 4.14.8 |
   | `semgrep.lock` | `semgrep==1.177.0`, `pyyaml==6.0.2` |
   | `build.lock` | `build==1.6.1`, `setuptools==84.2.1`, `setuptools-scm==10.3.4`, `wheel==0.48.0` |
   | `browser-crypto.lock` | `playwright==1.63.0`, `orjson==3.13.0`, `cryptography==50.0.2` |
   | `lint-tools.lock` | `vulture==2.14`, `jsonschema==4.25.1`, `orjson==3.13.0`, `ruff==0.15.12` |

   Test tools such as `pytest`, `mutmut` and `coverage` are pinned in
   `.github/test-requirements.lock`.

2. Rewrite the install with the exact version and the constraint. A floating
   step:

   ```yaml
   - name: Install the parser
     run: pip install 'pyyaml>=6.0'
   ```

   becomes:

   ```yaml
   - name: Install the parser
     run: python -m pip install --constraint .github/ci-tools/parser.lock pyyaml==6.0.3
   ```

   A `uv` step can take the constraint from the environment instead:

   {% raw %}
   ```yaml
   - name: Install the test tools
     env:
       UV_CONSTRAINT: ${{ github.workspace }}/.github/test-constraints.txt
     run: uv pip install --system pytest==9.1.1
   ```
   {% endraw %}

3. Delete the install's key from `FLOATING_INSTALLS`, once for each step you
   pinned.

4. Push and read the unit suite in CI.

## Add or change a tool lock

The locks in `.github/ci-tools/` are generated by
`.github/scripts/lock-ci-tools.sh`. Each `lock` line names a lock and its
top level requirements:

```bash
lock parser pyyaml==6.0.3
lock lint-tools vulture==2.14 jsonschema==4.25.1 orjson==3.13.0 ruff==0.15.12
```

The script compiles each line with `uv pip compile --universal` for Python
3.12, so a lock pins every dependency of its tools as well.
`--exclude-newer` sets the upload date it resolves against, so a rerun ignores
releases published after that date. To add or bump a tool, edit its `lock`
line, or move the date to take newer releases, then regenerate every lock from
the repository root:

```bash
bash .github/scripts/lock-ci-tools.sh
```

It needs `uv` on `PATH` and writes `.github/ci-tools/<name>.lock`. A new lock
also needs its name in `CI_TOOL_SETS` in `tests/test_ci_test_dependencies.py`:
`test_every_ci_tool_constraint_line_is_an_exact_pin` requires the folder to
hold exactly those generated locks, each opening with the generator's header
and holding only exact pins without extras or URLs.

## Tests

| Test | Holds |
|---|---|
| `test_every_ci_tool_install_pins_an_exact_version_under_a_checked_in_constraint` | The repository's measured floating installs, without the smokes, equal `FLOATING_INSTALLS` |
| `test_the_install_walker_reports_a_planted_floating_install` | Each planted floating install in a temporary tree is reported exactly once, and its pinned form is not reported |
| `test_the_install_walker_passes_pinned_and_non_install_commands` | Pinned installs and commands that install nothing are not reported |
| `test_each_intentional_install_smoke_is_a_measured_install_with_a_reason` | Each smoke key matches one measured unconstrained install and carries a reason |

The planted cases cover workflows, composite actions, step templates, shell
scripts and Python modules reached from a run line, every installer kind,
versioned `pip`, `pipx --pip-args`, nested `-r` and `-e` lines, floating lock
lines, unresolved variables, remote URLs and Python names bound more than
once. Their fixtures write `.github/test-requirements.lock` and
`.github/ci-tools/plant.lock` with the pins the cases use.

The unit suite runs these tests in CI. Where the operator permits local test
runs, the [local test guard](local-test-guard.md) allows them with its opt in:

```bash
AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN=true python -m pytest tests/test_ci_test_dependencies.py -k pins_an_exact_version
```

## Troubleshooting

The failure lists each problem, then `Measured floating installs:` and every
measured key as a quoted line ready to paste. Each `pin it` line ends with its
reasons in parentheses, joined by `; `.

| Symptom | Check and action |
|---|---|
| `floating CI tool install, pin it: <key> (floating version <spec>; ...)` | The named package has no exact `==` version, or its version is an unresolved variable. Pin it as in [Pin an install](#pin-an-install), or set the variable in an `env:` map or an earlier assignment. |
| `... (no checked in constraint)` | The command passes no accepted constraint. Add `--constraint` with a lock from `.github/ci-tools/` or the test requirements lock, or set `UV_CONSTRAINT` or `PIP_CONSTRAINT` on the step. |
| `... (<spec> is not pinned in its constraint)` | The version differs from the one the constraint pins, or the constraint does not pin the package. Use the lock's version, choose the lock that holds the tool, or regenerate the locks. |
| `... (requirements file <value> is not a checked in lock)` | An `-r` file is not `pyproject.toml` or an accepted lock. Install from a lock in `.github/ci-tools/` or the test requirements lock. |
| `... (<lock> holds <line>)` | A constraint file, or a file it includes with `-r`, holds a floating line, an `-e` line or a missing include. Pin the line, or regenerate the lock with the generator. |
| `no longer floating, remove it from FLOATING_INSTALLS: <key>` | The install is now pinned, or its command changed. Delete the key. |
| Both messages appear, for an old key and a new one | A listed install was edited without pinning it, so its key changed. Pin the install and delete the old key. |
| The smoke test fails | A smoke's command text changed, it now runs more than once, or it gained a constraint. Update its key, or remove it from `INTENTIONAL_SMOKES`. |
| A Python command with an environment constraint fails | Python commands carry no environment. Pass `--constraint` in the argument list. |
| A Python name is printed instead of its value | The module binds the name more than once. Bind it once, or write the value in the list. |

## Limits

The walk does not read these forms, and none exists in the CI files it walks
today:

- shell strings in Python, such as `subprocess.run("pip install x", shell=True)`,
  and argument lists joined with `+`;
- `bash -c '...'` strings and backtick substitutions;
- installers held in variables, such as `$PIP install x`.

It also does not read `uv run --with`, `Dockerfile` installs, `.bash` files or
`Makefile` recipes.

No base graded gate refuses growth of `FLOATING_INSTALLS`; review holds it to
shrinking.

## Delivery evidence

[PR #13083](https://github.com/the-cloud-clockwork/agentihooks/pull/13083)
delivered the walk for
[issue #13074](https://github.com/the-cloud-clockwork/agentihooks/issues/13074).
With an empty `FLOATING_INSTALLS`, the
[red run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44967790002)
failed only the repository walking test and listed every measured floating
install. The
[green run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44969293786)
passed with the list filled from that output. After three review rounds, the
[review fix run](https://github.com/the-cloud-clockwork/agentihooks/actions/runs/44987882535)
passed all 65 test cases in the module; the final head changed only the
feature map wording.
