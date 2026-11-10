---
title: Credential file names in commands
parent: Reference
nav_order: 78
---

# Credential file names in commands

The credential guard refuses a command that reads a dotenv file, a shell
startup or history file, or a credential store. To decide, it looks for
credential file names in the command. A name counts only when it ends where a
file name ends: a letter, digit, underscore or hyphen right after it means it
is part of a longer word. So the guard refuses
`python3 -c "print(open('auth.env').read())"`, while
`python3 -c "auth.env_text(record)"` runs, because `env_text` is a Python
attribute, not the `auth.env` file.

The guard runs at `PreToolUse` on every install target (Claude Code, Codex and
Copilot CLI). This page covers how it matches file names in `Bash` commands.
[Process environment reads](credential-guard-proc-environ.md) covers files
under `/proc`.

## Credential file names

| Kind | Names |
|---|---|
| Dotenv | `.env`, `.env.<suffix>` such as `.env.local` or `.env.production-local`, and `<name>.env` such as `stack.env` or `auth.env` |
| Shell startup and history | `.bashrc`, `.bash_profile`, `.bash_login`, `.profile`, `.zshrc`, `.zprofile`, `.zshenv`, `.bash_history`, `.zsh_history` |
| Credential store | `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.pgpass`, `launch-grant`, `.aws/credentials` |

A name ending in `.example`, `.sample`, `.template`, `.dist` or `.tpl` is a
template: `.env.example` stays readable.

## Where the guard looks for names

- **Operands and redirects.** A reader such as `cat`, `head` or `grep` that
  takes a credential file as an operand, an option value or a `<` redirect
  target is refused: `cat auth.env`. The last part of each operand must be a
  credential file name; `cat auth.env_text` passes.
- **Interpreter one-liners.** For `python`, `python2`, `python3`, `node`,
  `ruby`, `perl`, `php`, `sh`, `bash`, `zsh` and `dash`, the guard first checks
  the operands, then searches the whole command text for credential file names.
  The boundary rule applies to that search: a name followed by a letter, digit,
  underscore or hyphen is skipped.
- **Internal errors.** When the guard itself fails, the hook refuses the call
  if the tool input contains a credential file name under the same rule, and
  lets it through otherwise.

## What is refused

```bash
python3 -c "print(open('auth.env').read())"
python3 -c "auth.env_text(open('/app/auth.env').read())"
python3 -c "print(open('/app/.env.production-local').read())"
python3 -c "print(open('.env').read())"
python3 -c "print(open('stack.env').read())"
python3 -c "print(open('/home/user/.bashrc').read())"
python3 -c "print(open('/home/user/.netrc').read())"
cat auth.env
```

The second command calls `env_text`, and it also opens `/app/auth.env`; the
helper call does not hide the real read.

## What passes

Code that only names an attribute, function or variable containing a
credential file name:

```bash
python3 -c "auth.env_text(record)"
python3 -c "print(auth.env_name)"
python3 -c "print(auth.netrc_path)"
python3 -c "print(cfg.bashrc_lines)"
node -e "load(cfg.env_file)"
cat auth.env_text
python3 -c "print(open('.env.example').read())"
```

A name that passes this check is not a promise that the command reads no
secret. Use names, counts and variable references as shown in the refusal
message, not values.

## The refusal

A refused command stops with exit code 2 and emits the
`agentihooks.guardrail.credential_read_blocked` telemetry event. The first line
names the matched path or operand; when the operand is a whole one-liner, the
line repeats it:

```text
BLOCKED: reading auth.env via shell.
BLOCKED: reading print(open('/app/.env.production-local').read()) via shell.
```

The lines after it explain why reading the file exposes its values. For a dotenv
file that exists, they list its key names, never its values, then a leak free
alternative for that kind of file.

## Configuration

`CREDENTIAL_GUARD_ENABLED` is the only switch, read from the environment or
`~/.agentihooks/.env`, and defaults to `true`. Turning it off turns off every
credential guard refusal; the boundary rule has no setting of its own:

```bash
# ~/.agentihooks/.env
CREDENTIAL_GUARD_ENABLED=true
```

Bypass mode (`disable controls`) does not lift the refusal; credential values
in the transcript are a hard floor.

## Troubleshooting

- **An attribute or function call is refused.** Check what follows the
  credential file name in the command. `auth.env(record)` and `x = auth.env`
  are refused, because a bracket or the end of the text ends the name. Rename
  the attribute, or write the code to a file with the editor tool and run the
  file by path.
- **The refusal names the whole one-liner.** The operand check matched before
  the text search. The file it names is in the path's last part, after the
  last `/`.
- **A real read passed.** Check that `CREDENTIAL_GUARD_ENABLED` is unset or
  `true`. If it is, the command hides the file name in a way the text check
  does not see; report the exact command so a test can cover it.

## Source

Delivered by pull request
[#5467](https://github.com/the-cloud-clockwork/agentihooks/pull/5467) after the
guard refused an OpenBao repair of a worker hive member: it read the call
`auth.env_text(record)` as a read of the dotenv file `auth.env`. The boundary
sits at the end of `SENSITIVE_FRAGMENT` in the credential guard, which the
interpreter text search and the internal error check both use. Tests: the
`python calls env helper`, `python references env attribute`,
`python references credential attribute` and `python reads ...` cases in
`tests/test_credential_guard.py`.
