---
title: For loop word list gate
parent: Reference
nav_order: 93
---

# For loop word list gate

`python -m scripts.ci_for_substitution` fails the pull request lint job when a
shell `for` loop takes its word list from a command substitution. Errexit
(`set -e`) never sees a command fail in that position, so a script that should
stop runs the loop over whatever output the failed command printed, often
none, and carries on. The gate reads every tracked bash script, one with a
`.sh` name or a bash shebang, and the bash and sh run blocks of every workflow
and action file.
[Pull request 11926](https://github.com/the-cloud-clockwork/agentihooks/pull/11926)
added the gate for shell scripts and rewrote thirty loops that broke the rule.
[Pull request 11964](https://github.com/the-cloud-clockwork/agentihooks/pull/11964)
extended it to workflow and action run blocks and rewrote nine more.

## Why errexit misses the failure

Bash ignores the exit status of a command substitution that expands inside a
`for` loop's word list. A failed command there does not stop the script, even
with `set -euo pipefail`:

```bash
set -euo pipefail
for node in $(kind get nodes); do   # kind fails: the loop runs zero times
  docker exec "$node" true
done
echo "the script reports success"   # reached, and the script exits 0
```

A plain assignment does pass the failure to errexit, so capturing the output
first stops the script at the failed command:

```bash
set -euo pipefail
nodes="$(kind get nodes)"           # kind fails: the script exits here, non zero
for node in $nodes; do
  docker exec "$node" true
done
```

## What the gate refuses

Every loop below is refused. Each one is a case in
`tests/fixtures/ci_for_substitution/red.sh`:

```bash
for count in $(seq 3); do :; done                 # $( ) substitution
for count in `seq 3`; do :; done                  # backtick substitution
for path in "$(dirname "$0")"/*.sh; do :; done    # substitution inside a quoted word with a glob
for name in "${NAMES:-$(hostname)}"; do :; done   # substitution inside a parameter default
for line in $(< /etc/hostname); do :; done        # file read substitution
for word in first \
  $(printf 'second\n'); do :; done                # substitution on a continued line
if true; then for job in $(jobs -p); do wait "$job"; done; fi   # loop nested on one line
```

The gate parses each file with tree-sitter-bash. It refuses a loop when any
word in its word list holds a command substitution at any depth. A `select`
loop has the same word list node, so `select choice in $(ls); do ...; done` is
refused too.

## What passes

The loops in `tests/fixtures/ci_for_substitution/clean.sh` pass:

```bash
for count in {1..3}; do :; done                              # brace range
for ((count = 1; count <= limit; count++)); do :; done       # arithmetic loop
for value in $((limit + 1)) "$limit" "${NAMES:-default}"; do :; done   # arithmetic expansion, parameters
here="$(dirname "$0")"
for path in "$here"/*.sh; do
  printf '%s %s\n' "$path" "$(basename "$path")"            # a substitution in the body is fine
done
pids="$(jobs -p)"
for job in $pids; do wait "$job"; done                       # output captured by an assignment first
for word in "$@"; do :; done                                 # positional parameters
for item; do :; done                                         # implicit "$@"
```

Text that only looks like a loop also passes. A heredoc body, or a quoted string
such as `printf '%s\n' "for item in \$(date); do"`, holds no loop the parser can
see.

## How to rewrite a refused loop

| Refused | Rewrite | Notes |
| --- | --- | --- |
| `for _ in $(seq 60); do` | `for _ in {1..60}; do` | A brace range needs literal bounds |
| `for _ in $(seq $((drain + 90))); do` | `for ((poll = 0; poll < drain + 90; poll++)); do` | Use an arithmetic loop when a bound is a variable. `{1..$n}` does not expand: it yields the single word `{1..3}` |
| `for attempt in $(seq "$attempts"); do` | `for ((attempt = 1; attempt <= attempts; attempt++)); do` | Keeps the same counter values |
| `for job in $(jobs -p); do` | `pids="$(jobs -p)"` then `for job in $pids; do` | The assignment reports a failure to errexit |
| `for cand in "$(dirname "$0")/../.venv/bin/python" ...; do` | `here="$(dirname "$0")"` then `for cand in "$here/../.venv/bin/python" ...; do` | Capture once, reuse in every word |
| `for tag in "$digest" "$(printf 'b%.0s' $(seq 40))"; do` | `sha_tag="$(printf 'b%.0s' $(seq 40))"` then `for tag in "$digest" "$sha_tag"; do` | A substitution outside the word list is not graded |

Use a plain assignment for the capture. `local out="$(cmd)"`,
`export out="$(cmd)"` and `declare out="$(cmd)"` return the builtin's own
status, so they hide the failure just as the loop did. Declare the variable on
one line and assign it on the next: `local out` then `out="$(cmd)"`.

## Which files it grades

The gate lists files with `git -C <root> ls-files`, so an untracked file is
never graded.

| Input | Graded when | Not graded |
| --- | --- | --- |
| Shell script | The name ends in `.sh`, or the first line is a bash shebang such as `#!/usr/bin/env bash`, `#!/bin/bash -eu` or `#!/usr/bin/env -S bash -e` | A file without `.sh` whose shebang names another shell, such as `#!/bin/sh` or zsh, a symlink without a `.sh` name, and `tests/fixtures/ci_for_substitution/red.sh`, which is excluded by name |
| Workflow | A `.yml` or `.yaml` file directly under `.github/workflows` | A workflow in a subfolder of `.github/workflows`, and any other YAML file, including `tests/fixtures/ci_for_substitution/red.yml` |
| Action | A file named `action.yml` or `action.yaml` anywhere in the tree | |

The shell script test reuses `language()` from `scripts.no_comments`, the same
classification the comment gate uses.

{% raw %}
### Run blocks in workflows and actions

The gate walks the whole YAML document and reads every `run` key whose value is
a scalar. It grades the run text when the step's shell is bash or sh:

- The step's own `shell` wins. Without one, the job's `defaults.run.shell`
  applies; without that, the workflow's `defaults.run.shell` applies. Without
  any of them, the shell is bash, as GitHub's default on Linux runners is.
- A shell counts when it is one of these:
  - `bash` or `sh`, with or without arguments, such as `bash -e {0}`.
  - A path that ends in `bash` or `sh`, with or without arguments, such as
    `/bin/bash` or `/bin/bash --noprofile --norc -eo pipefail {0}`.
  - A GitHub expression, such as `${{ matrix.shell }}`, which is known only at
    run time.
- A step whose shell is `python`, `pwsh` or any other program is skipped.
- A blank shell, such as `shell: " "`, falls back to the inherited shell, as an
  absent one does.

Before parsing, the gate overwrites every `${{ ... }}` expression in the run
text with underscores and keeps its newlines. An expression reads as one plain
word, so `for name in ${{ matrix.names }}; do` passes, and an expression can
hide no loop.

Each refusal names the workflow file and a line:

- For a literal (`|`) or folded (`>`) block, the loop's own line in the file.
  The gate reads the block from the file's lines, indentation included, so a
  folded block is graded as it is written, line by line.
- For a plain or quoted run text, whose lines YAML may fold or escape, the line
  where the run text starts.

## Where it runs

The step `Refuse a command substitution as a for loop word list` runs in the
`lint` job of the Tests workflow:

```yaml
- name: Refuse a command substitution as a for loop word list
  if: ${{ !cancelled() }}
  run: |
    cd "$RUNNER_TEMP/grader"
    python -m scripts.ci_for_substitution --root "$GITHUB_WORKSPACE"
```

- The `lint` job runs on pull requests and admitted manual dispatches, and is
  skipped on `merge_group` and `push` events and when the
  [CI lab](ci-lab.md) holds an authoritative verdict for it.
- The [protected grader checkout](grader-gate-steps.md#the-protected-grader-checkout)
  always grades the head tree under `--root`, so a pull request cannot loosen
  the gate by editing its own copy. A grader without the module fails the step
  closed. An edit to the gate grades pull requests after it merges into `dev`.
- `if: ${{ !cancelled() }}` makes the step report even after an earlier lint
  step fails.
- A red step fails the `lint` job, which `Gate — Required` needs, so the pull
  request cannot enter the merge queue.
{% endraw %}

The [pre push gate](prepush-gate.md) does not run it. Run it by hand before
pushing a shell or workflow change.

## Run it locally

From the repository root, with a Python environment that has the package
installed and its base dependencies `tree-sitter-bash` and `pyyaml` from
`pyproject.toml`:

```bash
python -m scripts.ci_for_substitution --root .
```

A clean tree prints one line and exits 0. The counts are the tracked files the
gate graded, so they change with the tree:

```text
For loop word lists: <shells> shell files, <workflows> workflow files, 0 problems
```

A refused loop adds one GitHub annotation line per finding and exits 1. Shell
scripts come first, then workflow and action files, each group in
`git ls-files` order with lines in source order. A tree holding one refused
shell loop and one refused loop in a workflow's literal block prints:

```text
For loop word lists: 1 shell files, 1 workflow files, 2 problems
::error::deploy/wait.sh:2: a for loop word list holds a command substitution, so errexit never sees that command fail; capture its output in an assignment before the loop, or count with a brace range or an arithmetic loop
::error::.github/workflows/wait.yml:8: a for loop word list holds a command substitution, so errexit never sees that command fail; capture its output in an assignment before the loop, or count with a brace range or an arithmetic loop
```

The only option is `--root`, the git checkout to grade. It is required.

| Exit | Meaning |
| --- | --- |
| 0 | Every graded file is clean |
| 1 | At least one refused loop, or the tree holds no tracked shell file or no tracked workflow file |
| 2 | `--root` is missing or an option is unknown |

A `--root` that is not a git checkout makes `git ls-files` fail, and the gate
stops with a `subprocess.CalledProcessError` traceback.

## Limits

- The gate reads syntax. A loop inside a string handed to `bash -c`, `eval`,
  `ssh` or `kubectl exec ... sh -c` is a string to the parser and is not graded.
- A process substitution word list, `for path in <(cmd); do`, passes. It is a
  different node from a command substitution.
- No allowlist or inline suppression exists. Rewrite the loop.
- The gate only refuses the loop shape. It does not check that a capture is a
  plain assignment; see the note under
  [How to rewrite a refused loop](#how-to-rewrite-a-refused-loop).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `::error::<file>:<line>: a for loop word list holds a command substitution, ...` | The loop at that line takes its words from `$( )` or backticks | Rewrite it as shown under [How to rewrite a refused loop](#how-to-rewrite-a-refused-loop) |
| The line names the `run:` key, not the loop | The run text is a plain or quoted scalar | Find the loop in that run text, or turn it into a literal `\|` block so later refusals name the loop's own line |
| `::error::No tracked shell file found; the check graded no shell script` | `--root` holds no tracked `.sh` file or bash script | Point `--root` at the repository root |
| `::error::No tracked workflow file found; the check graded no run block` | `--root` holds no tracked file directly under `.github/workflows` and no action file | Point `--root` at the repository root |
| `subprocess.CalledProcessError` from `git ls-files` | `--root` is not a git checkout | Run it against a checkout |
| A new script with a refused loop passes locally | The file is untracked | `git add` the file, then run the gate again |
| A `#!/bin/sh` script with a refused loop passes | Without a `.sh` name, only a bash shebang marks a file as shell | Name it `*.sh` or give it a bash shebang |
| An edit to `scripts/ci_for_substitution.py` changes nothing in the pull request's lint run | The grader's copy runs | Expected. The edit grades later pull requests once it merges into `dev` |

## Tests and feature map

`tests/test_ci_for_substitution.py` holds the gate's tests and grades the
fixtures in `tests/fixtures/ci_for_substitution/`:

| Fixture | What the tests hold |
| --- | --- |
| `red.sh` | Seven refused loops, reported at lines 4, 8, 12, 16, 20, 24 and 29 in source order |
| `clean.sh` | No finding |
| `red.yml` | Nine refused loops across literal, folded and quoted run texts, an expression shell, a blank shell, a bash path step in a python job and a blank job default, reported at lines 7, 11, 17, 24, 27, 30, 33, 43 and 50 |
| `clean.yml` | No finding, including a python step, a python job default and a loop shaped `name` value |

Other tests hold the summary line and `::error::` format, the exit codes, the
tracked file set, a hundred loops far down a script reporting their exact
lines, an action run block that ends the file without a newline, the
expression blanking, the run text of the lint step, and that no workflow or
action in the repository holds a refused loop.

The feature map entry `ci-for-loop-word-list-workflow-run-blocks` in
`feature-map.json` pins the workflow and action input set and lists its seven
tests.

## Related pages

- [Grader gate steps](grader-gate-steps.md): the protected grader checkout and
  the lint steps that run only the grader's copy.
- [Pre push gate](prepush-gate.md): the local checks before a push, which do
  not include this gate.
- [Delivery tiers](delivery-tiers.md): which checks run on pull requests, in
  the merge queue and at promotion.
