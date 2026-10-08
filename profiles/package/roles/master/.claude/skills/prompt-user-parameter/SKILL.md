---
name: prompt-user-parameter
description: >
  Gets a secret, credential or other sensitive parameter from the operator, or
  runs a command he must take part in, without the value entering any
  transcript. Writes a throwaway script in a scratch folder, opens it in a
  herdr pane beside the master pane where he types the value hidden, and
  watches a status file for DONE or ERROR. Use when the master needs a key,
  token or password, or when the operator says "prompt me for it", "let me
  type the secret" or "run it in a pane".
argument-hint: "<what is needed>"
---

# Prompt User Parameter

The operator types the value into a pane only he sees. The value reaches the
command that needs it through the environment of a throwaway script and never
passes through your context, a log, a status file or a command argument.

Run it only while operator on is set in this pane. Otherwise add the ask to
Priorities with `agentihooks ledger --slug <slug> --as <name> priority add
<item> "<the ask in plain words>"` and continue other work.

## Steps

1. Create the scratch folder with `agentihooks scratch new`, then
   `mkdir -p <folder>/inputs`. Write the throwaway script there as
   `<folder>/inputs/<name>.sh`, never under `/tmp`. The opener refuses a
   script outside `~/scratchpad` and a caller outside a herdr pane.
2. In the script, read each value with the runner's helper and use it only
   through its variable:

   ```bash
   read_secret TS_AUTHKEY "Tailnet auth key"
   some-cli login --auth-key-env TS_AUTHKEY
   ```

   Never echo, log, write to a file or pass the value as an argument of a
   long lived process. Use a tool's environment variable or stdin option when
   it has one. The script runs with `bash -e`, so the first failing step ends
   it; an empty value fails `read_secret`.
3. Open the pane: `bash <skill dir>/scripts/open.sh <script> "<pane title>"`.
   It prints `pane=<id>` and `status=<file>`.
4. Tell the operator in plain words what to type in the new pane, then start
   a Monitor on the status file that emits its first line and exits when the
   file appears:

   ```bash
   until [[ -f <status file> ]]; do sleep 2; done; head -1 <status file>
   ```

5. Read the result. `DONE` means the script finished; the runner has deleted
   it and closed the pane. `ERROR <code>`
   carries the exit code; the runner deleted the script too, and the pane
   stays open showing the failing step until the operator presses Enter, so
   ask him what it said instead of reading the pane. Write a corrected script
   and open a new pane.

## Done when

The status file reads `DONE`, the script is gone and the step that needed the
value is verified by a check that does not print it, such as a status command
of the tool that received it.
