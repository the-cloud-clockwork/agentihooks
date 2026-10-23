---
title: Process environment reads
parent: Reference
nav_order: 31
---

# Process environment reads under /proc

The credential guard refuses every read of a process environment file under
`/proc`, such as `/proc/<pid>/environ` or `/proc/self/environ`. That file holds
the full environment of a running process, including any token, password or
connection address with credentials that the process was started with. A value
read from it enters the transcript, and an exposed credential has to be
rotated. The guard already refused environment dumps such as `env` and
`printenv`; this refusal closes the same leak through the file the kernel
publishes for each process.

The guard runs at `PreToolUse` on every install target (Claude Code, Codex and
Copilot CLI) and checks the `Bash`, `Read`, `NotebookRead` and `Grep` tools.

## What is refused

A shell command is refused when it names a process environment file in any of
these forms:

| Form | Examples |
|---|---|
| A plain read with any reader | `cat /proc/self/environ`, `strings /proc/1/environ`, `grep -a REDIS /proc/1/environ`, `grep -c . /proc/1/environ` |
| A redirect into a command | `tr '\0' '\n' < /proc/12345/environ \| grep ^AGENTIHOOKS_SWARM`, `while read -d '' l; do echo $l; done < /proc/thread-self/environ` |
| An interpreter that opens it | `python3 -c "print(open('/proc/1/environ').read())"`, the same call inside a heredoc, or a path built with `os.path.join('/proc', '1', 'environ')` |
| A list of files fed to another command | `ls /proc/*/environ \| xargs cat`, `xargs -0 -n1 -a /proc/1/environ echo` |
| A variable or substitution for the process id or the file name | `cat /proc/$PID/environ`, `f=environ; cat /proc/1/$f`, `cat /proc/1/$(printf envi)ron`, `` cat /proc/1/`printf envi`ron `` |
| A glob for the file name | `cat /proc/1/envi*`, `cat /proc/1/envir?n`, `cat /proc/1/env[i]ron`, `cat /proc/1/*` |
| A glob or variable for the `proc` folder | `cat /p*c/1/environ`, `cat /pr?c/self/environ`, `cat /pr[o]c/1/environ`, `P=/pro; cat ${P}c/1/environ` |
| Quoting or escapes inside the name | `cat /proc/1/env''iron`, `cat /proc/1/env\iron` |
| A read after changing into a process folder | `cd /proc/1 && cat environ`, `cd /proc/1; cat *`, `pushd /proc/self && cat env*`, `cd -- /proc/1 && cat e*`, `cd /p*/1 && cat e*` |

A command whose working folder is already under `/proc` is checked the same
way: from `/proc/1`, `cat environ`, `cat envi*`, `cat $f` and `cat *` are
refused.

The `Read` and `NotebookRead` tools are refused for an absolute path under
`/proc` whose last part is `environ`, including `//proc/1/environ` and a
thread's file such as `/proc/1/task/1/environ`. The `Grep` tool is refused when
its `path` or `glob` names such a file.

## What still passes

Other files under `/proc` and files named `environ` elsewhere stay readable:

```bash
cat /proc/meminfo
cat /proc/*/status
ls /proc/1/status
test -d /proc/$pid && echo alive
python3 -c "import os; print(os.listdir('/proc/1'))"
cd /home/x/procs && cat e*
cat build/environ
```

From a `/proc/1` working folder, `cat status` passes. The `Read` tool passes for
a relative path such as `proc/1/environ` or `docs/environ`.

## The refusal and the leak free alternatives

A refused call stops with exit code 2 and this message, and emits the
`agentihooks.guardrail.credential_read_blocked` telemetry event:

```text
BLOCKED: reading a process environment file under /proc.
A value read here enters the transcript, which is indexed and permanent — an exposed credential has to be rotated, not deleted.

A variable name that is not a valid identifier can hold a secret, and a value that spans several lines prints its later lines as if they were names, even through an identifier filter. Only two listings pass. Valid names and only a count of malformed ones:
  awk 'BEGIN{for(k in ENVIRON) if(k ~ /^[A-Za-z_][A-Za-z0-9_]*$/) print k; else n++; print "malformed names: " n+0}'
A count (wc, or grep -c):  env | wc -l
To test one variable:  test -n "${VAR:-}" && echo set || echo unset
To use a value, reference the variable ("$VAR") so the shell expands it at execution instead of printing it.
```

The alternatives answer the usual questions without printing a value:

| Question | Command |
|---|---|
| Is one variable set in this shell? | `test -n "${REDIS_URL:-}" && echo set \|\| echo unset` |
| Which variable names does this shell define? | the `awk` listing in the message above |
| How many variables are set? | `env \| wc -l` |
| How do I pass a value to a command? | reference it, as in `curl -H "Authorization: Bearer $API_TOKEN" …` |

These commands describe the environment of the shell that runs them. The guard
offers no way to read the values of another process.

## Configuration

`CREDENTIAL_GUARD_ENABLED` is the only switch, read from the environment or
`~/.agentihooks/.env`. It defaults to `true`. The hook keeps the guard on only
for `true`, `1` or `yes`, ignoring case; any other value turns the whole
credential guard off, including every dotenv, shell rc and credential file
refusal:

```bash
# ~/.agentihooks/.env
CREDENTIAL_GUARD_ENABLED=true
```

Bypass mode (`disable controls`) does not lift the refusal; credential values in
the transcript are a hard floor.

## Troubleshooting

- **A command that reads another file is refused.** When a command mentions
  `/proc`, or runs from or changes into a folder under it, and also contains
  the word `environ` on its own, the guard refuses it whatever file it reads.
  `ls /proc && grep environ notes.txt` and `cat /proc/1/environ.bak` are both
  refused. Split the command so the part that names `/proc` runs on its own.
  The attribute `os.environ` does not count, because a dot precedes it.
- **A heredoc body is refused.** The check reads the whole command text,
  heredoc bodies included, before any other parsing. A script that only quotes
  the path in a heredoc is refused too; write it to a file with the editor tool
  and run the file.
- **The guard let a read through.** Check that `CREDENTIAL_GUARD_ENABLED` is
  unset or `true` in the session's environment and `~/.agentihooks/.env`.
  If it is on, the command hides the path in a way the text check does not see;
  report it with the exact command so a test can cover it.

## Source

Delivered by pull request
[#4991](https://github.com/the-cloud-clockwork/agentihooks/pull/4991) after a
19 October incident in which a shell read of an agent process's environment
file printed the swarm Redis connection address with its password. The check is
`proc_environ_verdict` in the credential guard, run first in its `Bash`
decision; the `Read`, `NotebookRead` and `Grep` refusal is the environment case
of its path classifier. Tests: `TestProcEnvironReads` in
`tests/test_credential_guard.py`.
