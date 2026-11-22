---
title: Read only roles
parent: Reference
nav_order: 95
---

# Read only roles

The `qa` and `reviewer` package roles read code and report what they find. They
never change the code they review. Three `PreToolUse` conditions hold them to
that: one for the edit tools, one for Serena's write tools and one for shell
commands. Each condition refuses a write outside the seat's own folders, and the
refusal tells the agent to send the change to its author.

The shell condition is the newest of the three. Before it, a read only seat could
still write through the shell with a redirect, `sed -i` or `git commit`.

## Who is gated

| Role | Edit tools | Serena writes | Shell writes |
|---|---|---|---|
| `qa` | refused | refused | refused |
| `reviewer` | refused | refused | refused |
| `master` | refused, except in the master hotfix window | refused, except in the master hotfix window | not guarded |
| `planner` | refused | refused | not guarded |
| `dispatcher` | refused | refused | not guarded |

The `engineer`, `cicd`, `documentation` and `judge` roles carry none of these
conditions.

The conditions sit in each role's profile layer, so they run in every session
whose profile chain includes the role, inside a swarm or outside one. The swarm
environment only adds the task folder to the folders the seat may write.

## The three conditions

Each gated role keeps its shims in `profiles/package/roles/<role>/.claude/conditions/`.
The `qa` and `reviewer` folders carry these three:

| Condition file | Matcher | What it judges |
|---|---|---|
| `pre-edit+write+notebookedit-no_code_edits.py` | `Edit`, `Write`, `NotebookEdit` | every path in `file_path`, `notebook_path`, `relative_path` and `file_paths` |
| `pre-mcp__serena-no_serena_writes.py` | every Serena tool | the path of the seven Serena write tools; Serena reads pass |
| `pre-bash-no_shell_writes.py` | `Bash`, and the Codex shell tools after normalization | the command text |

Each shim runs `main(role)` from `profiles/package/roles/_guards/no_code_edits.py`,
with the role taken from the name of the folder the shim sits in.
`deny_reason` sends a `Bash` payload to the shell reader,
`hooks.context.read_only_shell.write_reason`, and judges every other tool by its
paths.

The Serena write tools are `replace_symbol_body`, `insert_after_symbol`,
`insert_before_symbol`, `rename_symbol`, `replace_content`, `replace_in_files`
and `safe_delete_symbol`.

See [Conditions](../hooks/conditions.md) for the filename and matcher grammar,
the layer order and the exit code contract.

## Folders a read only seat can write

| Folder | Open when |
|---|---|
| `~/scratchpad` and everything below it | always |
| `~/.agentihooks/swarm/<swarm>/tasks/<task>` | `AGENTIHOOKS_SWARM` and `AGENTIHOOKS_SWARM_TASK` are both set, both match `[A-Za-z0-9][A-Za-z0-9_-]*`, the task is not `master` or `master-<n>`, and no symlink sits on the folder's path |
| `/dev/null`, `/dev/stdout`, `/dev/stderr`, `/dev/tty` | shell targets only |

`$CLAUDE_CONFIG_DIR/plans` (by default `~/.claude/plans`) is open to `master`
and `planner` only, never to `qa` or `reviewer`.

Each target is resolved before it is compared with these folders:

- A relative path resolves from the session's working directory, the payload
  `cwd`.
- `..` is resolved, so `touch ~/scratchpad/../app.py` writes outside.
- Symlinks are followed. A link inside `~/scratchpad` that points into a
  repository is outside.
- `~` expands only when it starts the word unquoted. In `'~/scratchpad/a'`,
  `"~"/scratchpad/a` and `\~/scratchpad/a` the shell keeps a folder literally
  named `~` under the working directory, and the refusal names it
  `./~/scratchpad/a`.
- A target that holds a variable, a substitution, backticks, or a glob inside a
  segment that starts with a dot is known only at run time and is refused.
  `echo hi > "$HOME/scratchpad/a"` is refused with `writes $HOME/scratchpad/a`.
  Write `~/scratchpad/a` instead.

### Folder changes inside one command line

The shell reader follows `cd` and `pushd` from one simple command to the next:

| Command | Outcome |
|---|---|
| `cd ~/scratchpad && touch a` | passes |
| `pushd ~/scratchpad && touch a` | passes |
| `cd /etc; cat passwd > ~/scratchpad/a` | passes |
| `cd && touch a` | refused: a bare `cd` moves to the home folder |
| `(cd ~/scratchpad && touch a); touch b` | refused for `b`: the subshell's `cd` ends at `)` |
| `cd - && touch a`, `popd; touch a`, `cd "$D" && touch a` | refused: only run time knows the folder |

A `cd` in a pipeline or in the background moves nothing, so
`cd ~/scratchpad | true; touch a` is refused for `a`.

## What the shell condition refuses

The shell reader splits the command line into simple commands, including the
commands inside `$(...)`, backticks, `<(...)`, `bash -c` scripts and `eval`, and
the substitutions inside unquoted heredoc bodies. It refuses the first simple
command that writes outside the allowed folders.

### Redirects

Output redirects into a file outside the allowed folders are refused: `>`, `>>`,
`>|`, `&>`, `>&file` and numbered forms such as `2> err.log`. A redirect into
another file descriptor, such as `2>&1`, writes no file and passes.

### File write commands

| Command | Target it names |
|---|---|
| `mv`, `rm`, `rmdir`, `touch`, `mkdir`, `chmod`, `chown`, `truncate`, `tee`, `unlink` | every operand |
| `cp`, `ln`, `install`, `rsync` | the `-t` or `--target-directory` value, otherwise the last operand |
| `sed`, `perl` with `-i` or `--in-place` | each edited file, the script left out |
| `dd` | the `of=` value |
| `sort` | the `-o` or `--output` value |
| `uniq` | the second operand |
| `patch` | files named only at run time, unless `--dry-run` |
| `gzip`, `gunzip`, `bzip2`, `bunzip2`, `xz`, `unxz`, `zstd`, `unzstd` | every operand, unless `-c`, `-l`, `-t`, `--stdout`, `--to-stdout`, `--list` or `--test` streams the output |
| `tar` | files named only at run time, for any of the `x`, `c`, `r`, `u` or `A` modes or their long forms |
| `unzip` | files named only at run time, unless `-l`, `-t`, `-p`, `-v`, `-Z` or `-z` |
| `curl` | the `-o` or `--output` value other than `-`; files named only at run time for `-O`, `--remote-name` or `--remote-name-all` |
| `wget` | the `-O` or `--output-document` value; files named only at run time when it has none; nothing for `-O-` or `--spider` |
| `find` | the `-fprint`, `-fprint0`, `-fls` or `-fprintf` value; `-delete` is refused on its own |

A program is matched by its base name, so `/bin/rm app.py` counts as `rm`.

### git and gh

`git` runs only these subcommands:

`status`, `log`, `diff`, `show`, `fetch`, `rev-parse`, `rev-list`, `merge-base`,
`blame`, `grep`, `ls-files`, `ls-tree`, `ls-remote`, `cat-file`, `describe`,
`shortlog`, `for-each-ref`, `show-ref`, `name-rev`, `range-diff`, `diff-tree`,
`cherry`, `version` and `help`.

These subcommands pass only in their listing forms:

| Subcommand | Listing forms |
|---|---|
| `branch` | no arguments, or only `--show-current`, `-a`, `-r`, `-v`, `-vv`, `-l`, `--list`, `--all`, `--remotes` |
| `tag` | no arguments, `-l`, `--list` |
| `remote` | no arguments, `-v`, `get-url`, `show` |
| `config` | `--get`, `--get-all`, `--get-regexp`, `--list`, `-l` |
| `stash` | `list`, `show` |
| `worktree` | `list` |
| `reflog` | no arguments, `show` |
| `submodule` | no arguments, `status` |

Every other subcommand is refused with `runs git <subcommand>`, in any folder:
`git -C ~/scratchpad/x commit -m x` is refused too. The global options `-C`,
`--git-dir`, `--work-tree` and `--namespace` skip their value before the
subcommand is read. `-c` and `--config-env` are refused, because a
configuration value can run a program. `--output` is refused anywhere in a git
command, because it writes a file.

`gh` refuses `gh pr checkout`, `gh pr co` and `gh repo clone`, with or without
`-R`, `--repo` or `--hostname`. Every other `gh` command passes, including
`gh pr view`, `gh pr diff`, `gh pr comment` and `gh pr review`.

### Wrappers, prefixes and hidden commands

- Leading assignments (`FOO=1 rm app.py`) and the keywords `if`, `then`, `else`,
  `elif`, `do`, `while`, `until`, `!` and `{` are skipped.
- The wrappers `sudo`, `env`, `nice`, `timeout`, `flock`, `stdbuf`, `ionice`,
  `command`, `nohup`, `exec`, `time`, `setsid` and `builtin` are skipped with
  their option values, so `sudo rm app.py` and `timeout 5 rm app.py` are judged
  as `rm app.py`.
- `env -C`, `env --chdir`, `env -S` and `env --split-string` are refused,
  because they hide the folder or the command.
- A program named by a variable, backticks or a glob is refused:
  `$EDITOR app.py` gives `runs a command named by $EDITOR`.
- A shell (`bash`, `sh`, `zsh`, `dash`) with only options reads its script from
  its input and is refused: `cat x | bash`, `sh -s` and `bash <<EOF`.
- The script of `bash -c`, or of any short option cluster with `c` such as
  `bash -lc`, is judged as a command line. So are the words of `eval`, the
  commands of `find -exec`, `-execdir`, `-ok` and `-okdir`, and the command
  `xargs` runs.
- A `{}` placeholder, and the arguments `xargs` adds, are known only at run time,
  so `find . -exec rm {} +`, `ls | xargs rm` and
  `ls | xargs -I{} cp {} ~/scratchpad/` are refused.

## What still passes

- Every command that writes no file: `cat`, `grep`, `rg`, `ls`, `sed` without
  `-i`, `jq`, `gh pr view`, `gh pr diff`, `curl -s URL`, `wget -O- URL`,
  `tar -tzf`, `unzip -l`, `find . -exec cat {} +`, `ls | xargs cat`.
- The git reads above, `git fetch` included, and a bare `git`.
- Writes into the allowed folders: `cp app.py ~/scratchpad/`,
  `git log --oneline | head -5 > ~/scratchpad/log.txt`,
  `gzip -dc a.gz > ~/scratchpad/a`, `mv ~/scratchpad/a ~/scratchpad/b`.
- Writes to the devices: `echo hi > /dev/null`, `make 2>&1 >/dev/null`.
- A bare `tee`, and `sudo -v`.
- Text inside single quotes: `echo '$(rm app.py)'` runs no command.

## Refusal texts

A refused call exits 2 and prints one line on stderr, which the harness hands
back to the agent.

| Tool | Text |
|---|---|
| Bash | `<role> cannot write through the shell: the command <phrase>. Reads, ~/scratchpad and the task folder stay open; send the change to its author.` |
| Edit, Write, NotebookEdit, Serena writes | `<role> cannot edit outside ~/scratchpad; send the change to its author.` |
| Any tool, when the guard fails | `<role> cannot evaluate the coding window; edits stay closed.` |

The edit text names only `~/scratchpad`, but the task folder is open to edits
as well.

The shell `<phrase>` is one of:

| Phrase | Cause |
|---|---|
| `writes <target>` | the last target outside the allowed folders |
| `writes files named only at run time` | a placeholder, `xargs`, `patch`, `tar`, `unzip`, `curl -O` or `wget` without `-O` |
| `runs git <subcommand>` | a git subcommand outside the read set |
| `runs git with -c configuration` | `git -c` or `--config-env` |
| `writes a file with git --output` | `--output` on a git line |
| `runs gh pr checkout`, `runs gh pr co`, `runs gh repo clone` | the gh commands that write a checkout |
| `runs env -C, which hides the folder or the command it runs` | `env -C`, `--chdir`, `-S` or `--split-string`; the option is named as typed |
| `runs a command named by <word>` | a program named by a variable, backticks or a glob |
| `deletes files with find -delete` | `find -delete` |
| `feeds a script to <shell> on its input` | a shell with only options |

## Fail closed and fail open

- Any error inside `deny_reason` refuses the call with the `cannot evaluate`
  text. A malformed payload, such as a `Bash` call with no `tool_input`, is
  refused this way.
- A call with no path is refused: an `Edit` payload without `file_path` gets
  the edit refusal.
- A shim that crashes before it reaches `main`, or that times out, is skipped
  under the [exit code contract](../hooks/conditions.md#script-contract): any
  exit other than 0 or 2 lets the call through and names the condition in a
  one line context note.

## Harness support

The conditions run on Claude Code and Codex. Both apply a `pre` deny; see
[Harness support](../hooks/conditions.md#harness-support). On Codex the shell
tools normalize to `Bash` with `tool_input.command`, so the `pre-bash` matcher
catches them.

## Inspecting

List the conditions that fire for a command in the installed chain:

```bash
agentihooks conditions list --tool Bash --command "git commit -m x"
AGENTIHOOKS_TARGET=codex agentihooks conditions list --tool Bash --command "git commit -m x"
```

A session whose chain includes `qa` or `reviewer` lists
`pre-bash-no_shell_writes.py` for that command.

Ask the shell reader directly from a checkout of this repository, with the
folders a seat would have:

```bash
~/dev/tcc-ecosystem/.venv/bin/python - <<'PY'
from pathlib import Path

from hooks.context.read_only_shell import write_reason

roots = [(Path.home() / "scratchpad").resolve()]
for command in ("git commit -m x", "echo hi > notes.md", "cat app.py"):
    print(repr(command), "->", repr(write_reason(command, Path.cwd(), roots)))
PY
```

An empty string means the command passes.

## Examples

A reviewer seat with `AGENTIHOOKS_SWARM=demo` and
`AGENTIHOOKS_SWARM_TASK=t-review-1` keeps its notes in its task folder and the
scratchpad:

```bash
cat >> ~/.agentihooks/swarm/demo/tasks/t-review-1/progress.md <<'EOF'
- read the diff, two findings sent to the author
EOF
mkdir -p ~/scratchpad/agentihooks/review
gh pr diff 12942 > ~/scratchpad/agentihooks/review/pr.diff
```

All three commands pass. These are refused:

```bash
echo note > review.md
ruff check . 2>&1 | tee lint.log
sed -i 's/old/new/' app.py
git commit -am "fix review finding"
gh pr checkout 12942
bash -c 'echo x > app.py'
```

The refusal for the second one reads:

```
reviewer cannot write through the shell: the command writes lint.log. Reads, ~/scratchpad and the task folder stay open; send the change to its author.
```

The task folder must be spelled out. A path built from `$AGENTIHOOKS_SWARM` or
`$HOME` is known only at run time and is refused.

## Troubleshooting

| Refusal | Cause and fix |
|---|---|
| `writes ./~/scratchpad/...` | the tilde is quoted or escaped; write `~/scratchpad/...` unquoted |
| `writes $HOME/scratchpad/...` | a variable makes the target known only at run time; write `~/scratchpad/...` |
| `writes +x` or `writes 755` from `chmod` | the mode operand counts as a path; run `cd ~/scratchpad && chmod +x run.sh` |
| `writes files named only at run time` from `tar` or `unzip` | extraction is refused even into the scratchpad; list with `tar -tzf` or `unzip -l`, or print one member with `unzip -p` |
| `writes files named only at run time` from `xargs` or `find -exec` | the files come from the input; run the write with the folder in the command, or send the change to its author |
| `runs git commit`, `runs git checkout`, `runs gh pr checkout` | a read only seat never changes a checkout; read the change with `gh pr diff <n>`, or `git fetch` and `git diff` |
| a write into the task folder is refused | check that `AGENTIHOOKS_SWARM` and `AGENTIHOOKS_SWARM_TASK` are set and valid, that the task is not a master task, and that no part of `~/.agentihooks/swarm/<swarm>/tasks/<task>` is a symlink |
| `cannot evaluate the coding window` | the guard raised an error on the payload; read the payload the harness sent |

## Limitations

- The shell reader reads the command text only. A program that opens files
  itself passes: `python3 -c "open('app.py', 'w')"`, `node -e ...` and a script
  run by path, such as `bash ~/scratchpad/run.sh`.
- `tar` and `unzip` extraction is refused into every folder, the scratchpad
  included.
- `chmod` and `chown` count their mode or owner operand as a path, so they pass
  only when that operand resolves inside an allowed folder.
- Serena writes need an absolute path. A relative `relative_path` is refused,
  and `rename_symbol` is refused for every path.
- A shim that crashes or times out lets the call through.
- The guard has no setting or environment switch. A later profile layer that
  ships a file with the same `<step>-<matcher>-<name>` replaces the shim.
