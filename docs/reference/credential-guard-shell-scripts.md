---
title: Environment dumps in shell scripts and awk
parent: Reference
nav_order: 94
---

# Environment dumps in shell scripts and awk

The credential guard reads the script a shell runs with `-c` and refuses it
when the script would print the environment, read a credential file or search
a tree that holds one. It also refuses an awk program that reads a value out of
`ENVIRON`. Before this change, both forms printed credential values while an
agent only meant to list variable names.

On 17 November three hive agent sessions printed credential values into their
transcripts while listing environment names. The delivering pull request
records that the guard was on in those pods and refused plain `env | grep`
forms in the same sessions. These three commands, shortened here, passed:

```bash
kubectl exec -n swarm-hive-001 $H -- sh -c 'env | grep -iE "ledger|swarm_url" | sed -E "s/(TOKEN|KEY|SECRET|PASS)[^=]*=.*/\1=<redacted>/"'
kubectl -n swarm-hive-001 exec "$P" -c worker-hive -- sh -c 'env | cut -d= -f1 | grep -i -E "ca|cert|ssl|ledger" | sort'
awk 'BEGIN{for(k in ENVIRON) if(k ~ /^(AGENTIHOOKS_(HIVE|SWARM)[A-Z_]*)$/) print k"="ENVIRON[k]}'
```

The guard checked environment dumps only in the command's top level text, and
unwrapped `kubectl exec` down to `sh -c` without reading the script. No check
read an awk program. All three are refused now.

The guard runs at `PreToolUse` on every install target (Claude Code, Codex and
Copilot CLI). This page covers `Bash` commands. Reads of `/proc/<pid>/environ`
are covered in [Process environment reads](credential-guard-proc-environ.md).

## Shell scripts run with -c

### Which shells

The shells are `sh`, `bash`, `zsh` and `dash`, matched by the last part of the
path, so `/bin/bash` counts. The guard finds the shell in these places:

- **The command itself**, after the prefixes `sudo`, `command` and `env`, a
  flag right after `sudo`, and `NAME=value` assignments. `sudo -E bash -c`,
  `FOO=1 bash -c` and `command bash -c` all count.
- **Every stage of a pipeline and every part of a command list** joined by
  `|`, `&&`, `||`, `;` or a new line. In `sh -c 'env' && ls` the first part is
  read.
- **The command a wrapper runs.** The wrappers are `kubectl`, `docker`,
  `podman`, `nerdctl` and `ssh`. A wrapper's command starts at its first word
  that names a reader such as `cat` or `awk`, the `source` or `.` command, or
  an interpreter such as `python3` or `sh`. `kubectl exec pod -- sh -c '…'`,
  `docker exec box bash -c '…'` and `ssh host sh -c '…'` all count.

### Which word is the script

After the shell, every word that follows a flag starting with a single `-`
and containing the letter `c` is read as a script. `-c`, `-lc`, `-ec`, `-ce`
and `-xc` all count, so `bash -euo pipefail -c 'env'` and `bash -x -c 'env'`
are read. A flag starting with `--`, such as `--rcfile`, does not count. A
shell with no `-c` flag, such as `bash script.sh`, has no script to read.

The rule fails closed: a word shaped like `-c` anywhere after the shell has its
next word read as a command, even when it is an argument to a script file.
`bash deploy.sh -c set` is refused as an environment dump.

### How the script is checked

The script is checked as a command of its own, with every check the guard runs
on a top level `Bash` command:

- environment dumps (`env`, `printenv`, `export`, `export -p`, `declare -x`,
  `set`) that do not end in a count
- `printenv` of a name that looks secret, and any part of the command, split
  at `&&`, `||`, `;` or a new line, that contains the word `echo` or `printf`
  and names such a variable with `$` anywhere in it; a name looks secret when
  it contains `PASSWORD`, `PASSWD`, `SECRET`, `TOKEN`, `API_KEY`, `APIKEY`,
  `CREDENTIAL`, `PRIVATE_KEY`, `ACCESS_KEY`, `SESSION_KEY` or `AUTH`, in any
  case; a name that appears only as the operand of a set test does not count
  (see [Set tests inside a script](#set-tests-inside-a-script))
- awk reads of `ENVIRON` values, described below
- reads of `/proc/<pid>/environ`
- reads of dotenv, shell startup and credential store files
- Secret reads with `kubectl get` and `kubectl describe`, and `gh` token prints
- recursive searches of a tree that holds a credential file

A script inside a script is read the same way, so `sh -c "bash -c 'env'"` is
refused.

| Command | First line of the refusal |
|---|---|
| `sh -c 'env \| sort'` | `BLOCKED: dumping the whole environment (env).` |
| `bash -lc 'printenv \| grep REDIS'` | `BLOCKED: dumping the whole environment (printenv).` |
| `zsh -c 'export -p'` | `BLOCKED: dumping the whole environment (export).` |
| `dash -ec 'set'` | `BLOCKED: dumping the whole environment (set).` |
| `bash -c 'env \| cut -d= -f1'` | `BLOCKED: dumping the whole environment (env).` |
| `bash -c 'cd /app; env'` | `BLOCKED: dumping the whole environment (env).` |
| `sudo bash -euo pipefail -c 'env \| cut -d= -f1'` | `BLOCKED: dumping the whole environment (env).` |
| `kubectl exec -n ns pod -- sh -c 'env \| cut -d= -f1 \| sort' 2>&1 \| head -30` | `BLOCKED: dumping the whole environment (env).` |
| `docker exec box bash -c 'printenv'` | `BLOCKED: dumping the whole environment (printenv).` |
| `ssh host sh -c 'env'` | `BLOCKED: dumping the whole environment (env).` |
| `bash -c 'printenv LEDGER_TOKEN'` | `BLOCKED: printing the value of LEDGER_TOKEN.` |
| `bash -c 'echo $DATABASE_PASSWORD'` | `BLOCKED: printing $DATABASE_PASSWORD to stdout.` |
| `kubectl exec pod -- bash -c 'cat /app/.env'` | `BLOCKED: reading /app/.env via shell.` |

A name stripping stage does not turn a dump into a name listing. Values can
span several lines, and `cut -d= -f1` prints a value's later lines as if they
were names. Only a count ends the dump: `wc`, or `grep` with `-c`.

## awk reads of ENVIRON values

An `awk`, `gawk`, `mawk` or `nawk` stage is refused when its text contains
`ENVIRON` followed by `[`, with any spaces or new lines between them. Any of
the four can be any stage of a pipeline, a path such as `/usr/bin/awk`, or the
command inside a shell `-c` script. Through a `kubectl`, `docker`, `podman`,
`nerdctl` or `ssh` wrapper that runs it directly, only `awk` is read, because
the wrapper's command starts at a reader word and `gawk`, `mawk` and `nawk`
are not reader words.

```bash
awk 'BEGIN{for(k in ENVIRON) if(k ~ /^AGENTIHOOKS_(HIVE|SWARM)/) print k"="ENVIRON[k]}'
gawk 'BEGIN{print ENVIRON["DATABASE_URL"]}'
mawk 'BEGIN { print ENVIRON ["REDIS_URL"] }'
nawk 'BEGIN{print ENVIRON["X"]}'
compgen -e | /usr/bin/awk '{print $1"="ENVIRON[$1]}'
kubectl exec pod -- awk 'BEGIN{print ENVIRON["DATABASE_URL"]}'
kubectl exec pod -- sh -c "awk '{print ENVIRON[\$1]}'"
```

Each of these is refused with
`BLOCKED: printing environment values (awk ENVIRON[...]).`

## Set tests inside a script

The set or unset test the refusal recommends passes inside a `-c` script, so
an agent can check whether a pod or container holds a variable:

```bash
kubectl exec -n ns pod-0 -- sh -c 'test -n "${GITHUB_TOKEN:-}" && echo set || echo unset'
kubectl exec pod-0 -- bash -c '[ -n "$GITHUB_TOKEN" ] && echo set || echo unset'
kubectl exec pod-0 -- bash -c '[[ -n "${LEDGER_TOKEN-}" ]] && echo set || echo unset'
docker exec box sh -c 'test -z "${API_KEY}" && echo unset || echo set'
```

A set test is a whole part of a single quoted `-c` script, split at `&&`,
`||`, `;` or a new line, that is `test`, `[` or `[[` with `-n` or `-z` and one
double quoted operand: `"$VAR"`, `"${VAR}"`, `"${VAR-}"` or `"${VAR:-}"`.
The `echo` and `printf` check leaves those parts out when it looks for a
secret looking name. Every other use of the name still counts, so these are
refused with `BLOCKED: printing $GITHUB_TOKEN to stdout.`:

```bash
kubectl exec pod-0 -- sh -c 'echo test -n "$GITHUB_TOKEN"'
kubectl exec pod-0 -- sh -c 'echo [ -n "$GITHUB_TOKEN" ]'
kubectl exec pod-0 -- sh -c 'test -n "${GITHUB_TOKEN:-}" && echo "$GITHUB_TOKEN"'
kubectl exec pod-0 -- sh -c 'test -n "${GITHUB_TOKEN:-}" && echo "$1"' _ "$GITHUB_TOKEN"
kubectl exec pod-0 -- sh -c 'test -n "$GITHUB_TOKEN" -a "$GITHUB_TOKEN" -eq 0 && echo set'
```

The first two print the test as text with the value in it. The last prints
the value in the shell's `integer expression expected` error. With xtrace on,
the shell prints each expanded command to stderr, `test -n` and its value
included, so the `echo` and `printf` check counts every name in a command that
holds a flag word with an `x`, such as `sh -xc` or `set -ex`, or the word
`xtrace`:

```bash
kubectl exec pod-0 -- sh -xc 'test -n "${GITHUB_TOKEN:-}" && echo set || echo unset'
kubectl exec pod-0 -- bash -c 'set -o xtrace; [ -n "$API_KEY" ] && echo set'
```

An unquoted operand, another expansion such as `"${VAR:+x}"` and a script in
double quotes are not read as a set test, so these are refused too:

```bash
kubectl exec pod-0 -- sh -c 'test -n $GITHUB_TOKEN && echo set'
kubectl exec pod-0 -- sh -c 'test -n "${GITHUB_TOKEN:+x}" && echo set'
kubectl exec pod-0 -- sh -c "test -n \"\$GITHUB_TOKEN\" && echo set"
```

## What still passes

```bash
awk 'BEGIN{for(k in ENVIRON) if(k ~ /^[A-Za-z_][A-Za-z0-9_]*$/) print k; else n++; print "malformed names: " n+0}'
awk 'BEGIN{for(k in ENVIRON) if(k ~ /LEDGER/) print k}'
awk -F= '{print $1}' names.txt
awk '{print MYENVIRON[1]}' data.txt
grep -n 'ENVIRON[' notes.txt
bash -c 'env | wc -l'
sh -c 'env | cut -d= -f1 | wc -l'
bash -c 'compgen -e'
kubectl exec -n ns pod -- sh -c 'ls /app'
bash script.sh
bash script.sh check env
bash --rcfile env
grep -c env notes.txt
python3 -c 'env'
```

An awk program that loops over `ENVIRON` and prints only the key passes, as
does one that never names `ENVIRON` with a subscript. `MYENVIRON[1]` is
another array, and `grep` naming `ENVIRON[` is not awk. `python3 -c` is not a
shell, so its argument is not read as a shell script.

## Listing environment names

| Question | Command |
|---|---|
| Which variable names does this shell define? | `awk 'BEGIN{for(k in ENVIRON) if(k ~ /^[A-Za-z_][A-Za-z0-9_]*$/) print k; else n++; print "malformed names: " n+0}'` |
| Which names does a pod's shell define? | the same `awk` listing as the pod's command: `kubectl exec -n ns pod -- awk '…'` |
| How many variables are set? | `env \| wc -l` |
| Is one variable set? | <code>test -n "${REDIS_URL:-}" && echo set &#124;&#124; echo unset</code> |
| Is one variable set in a pod? | <code>kubectl exec -n ns pod -- sh -c 'test -n "${REDIS_URL:-}" && echo set &#124;&#124; echo unset'</code> |
| How do I pass a value to a command? | reference it, as in `curl -H "Authorization: Bearer $API_TOKEN" …` |

The name listing prints only names that are valid identifiers and counts the
rest, because a malformed name can hold a secret.

## The refusal

A refused command stops with exit code 2 and emits the
`agentihooks.guardrail.credential_read_blocked` telemetry event. An awk read
prints:

```text
BLOCKED: printing environment values (awk ENVIRON[...]).
A value read here enters the transcript, which is indexed and permanent — an exposed credential has to be rotated, not deleted.

A variable name that is not a valid identifier can hold a secret, and a value that spans several lines prints its later lines as if they were names, even through an identifier filter. Only two listings pass. Valid names and only a count of malformed ones:
  awk 'BEGIN{for(k in ENVIRON) if(k ~ /^[A-Za-z_][A-Za-z0-9_]*$/) print k; else n++; print "malformed names: " n+0}'
A count (wc, or grep -c):  env | wc -l
To test one variable:  test -n "${VAR:-}" && echo set || echo unset
To use a value, reference the variable ("$VAR") so the shell expands it at execution instead of printing it.
```

A refused script prints the same message the guard gives at the top level,
with the first line naming what the script does.

## Configuration

`CREDENTIAL_GUARD_ENABLED` is the only switch, read from the environment or
`~/.agentihooks/.env`. It defaults to `true`. The hook keeps the guard on only
for `true`, `1` or `yes`, ignoring case; any other value turns off every
credential guard refusal, including these. The script and awk checks have no
setting of their own:

```bash
# ~/.agentihooks/.env
CREDENTIAL_GUARD_ENABLED=true
```

Bypass mode (`disable controls`) does not lift the refusal; credential values
in the transcript are a hard floor.

## Limits

The guard reads the command text. These forms pass because the text does not
show the script or the program. Passing the guard does not make one of them
safe for listing the environment; use the name listing above.

| Form | Example |
|---|---|
| A script held in a variable or built by a substitution | `sh -c "$CMD"`, `bash -c "$(printf env)"` |
| A script on standard input or in a file | `echo env \| sh`, a heredoc fed to `bash`, `bash script.sh`, `awk -f prog.awk` |
| A shell other than `sh`, `bash`, `zsh` and `dash` | `ksh -c 'env'`, `fish -c 'env'`, `busybox sh -c 'env'` |
| A command before the shell other than `sudo`, `command`, `env` or an assignment | `timeout 5 sh -c 'env'`, `nice bash -c 'env'`, `env -i sh -c 'env'` |
| A `sudo` option that takes a value | `sudo -u root bash -c 'env'` |
| `gawk`, `mawk` or `nawk` run directly by a wrapper | `kubectl exec pod -- gawk 'BEGIN{print ENVIRON["X"]}'`; `awk` is read there, and the others are read at the top level and inside a shell script |
| An environment dump with no shell | `kubectl exec pod -- env` |
| A traced script with no `echo` or `printf` | `kubectl exec pod -- sh -xc 'test -n "$API_TOKEN"'` prints the value in its trace |
| A container inspection that formats its environment | `docker inspect --format '…Env…' box` |
| An interpreter one-liner that prints the environment | `python3 -c 'import os; print(os.environ)'` |
| `printenv` of a name that does not look secret | `printenv DATABASE_URL`, which can hold a password inside the address |
| `declare -p` | `declare -p` prints every variable with its value |

## Troubleshooting

- **An argument to a script file is refused.** A word shaped like `-c` after
  the shell makes the guard read the next word as a command, so
  `bash deploy.sh -c set` is refused as `set`, and `bash deploy.sh -- -c set`
  is refused too. Run the script by path, as in `./deploy.sh -c set`, or use
  its long option, as in `bash deploy.sh --config set`.
- **A recursive search is refused inside a script but rewritten at the top
  level.** In Claude Code bypass mode the guard rewrites a top level recursive
  `grep` or `rg` to exclude credential files. Inside a `-c` script it never
  rewrites; it scans the tree and refuses the search when the tree holds a
  credential file. Run the search at the top level, or add the excludes the
  refusal names to the search inside the script.
- **A name listing through `cut` is refused.** `sh -c 'env | cut -d= -f1'`
  is refused because a value that spans several lines prints its later lines
  as names. Use the awk name listing, or end the pipeline with `wc -l` for a
  count.
- **An awk program that only mentions ENVIRON in a string is refused.** The
  check reads the stage text, not awk syntax: `awk '{print "ENVIRON[x]"}'` is
  refused. Search text with `grep` instead; `grep -n 'ENVIRON[' notes.txt`
  passes.
- **The guard let a dump through.** Check that `CREDENTIAL_GUARD_ENABLED` is
  unset or `true` in the session's environment and `~/.agentihooks/.env`. If
  it is on, compare the command with the limits table; for any other form,
  report the exact command so a test can cover it.

## Source and regression coverage

Delivered by pull request
[#12032](https://github.com/the-cloud-clockwork/agentihooks/pull/12032). The
cause was proven before the change: a replay of the three 17 November commands
through the guard's `evaluate` on dev at `1b19d6f0ef` returned no refusal for
each, as recorded in the pull request. The Proofs workflow run on the red test
head `0dc0d5e58` failed 18 cases, all new seams, and passed every pass control
(run 44318050064). The Tests workflow run on the pull request head `2c156cd2b`
was green (run 44328058746).

In `hooks.context.credential_guard`, `shell_scripts` returns the scripts a
shell stage runs with `-c`, through `unwrapped` for a wrapper's command, and
`script_verdict` checks each one with `decide_bash` before the stage's own
check. `awk_environ_verdict` is the awk check, run with the other top level
checks in `decide_bash`.

The three 17 November commands, in full, are rows of the guard case table in
`tests/test_credential_guard.py`. Regression tests:
`test_an_environment_dump_inside_a_shell_script_is_refused`,
`test_a_shell_script_that_prints_no_value_passes`,
`test_a_shell_script_keeps_the_credential_tree_scan`,
`test_an_awk_read_of_an_environment_value_is_refused` and
`test_an_awk_listing_of_environment_names_passes`. Feature map entries
`credential-guard-reads-shell-scripts` and
`credential-guard-refuses-awk-environ-values` record the behaviour. See
[Feature map](feature-map.md).

Set tests inside a script were delivered by pull request
[#12604](https://github.com/the-cloud-clockwork/agentihooks/pull/12604).
`untested_pieces` leaves the set test parts of each single quoted `-c` script
out of the text `echo_secret_verdict` reads. Regression tests:
`test_a_set_test_inside_a_shell_script_passes`,
`test_a_shell_script_that_prints_the_value_is_refused` and
`test_a_set_test_of_another_form_is_refused`, recorded by the feature map
entry `credential-guard-passes-set-tests-in-scripts`.
