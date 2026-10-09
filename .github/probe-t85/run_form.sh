#!/usr/bin/env bash
set -uo pipefail
name=$1
cmd=$2
log="$RUNNER_TEMP/$name.log"
echo "=== $name"
echo "command: $cmd"
timeout 2400 bash -c "$cmd" >"$log" 2>&1
echo "exit: $?"
echo "--- log tail"
tail -n 40 "$log"
echo "--- markers"
echo "entered main (Changed Python files line): $(grep -c '^Changed Python files' "$log")"
echo "changed files line: $(grep '^Changed Python files' "$log")"
echo "entered run_gate (Mutation shard line): $(grep -c '^Mutation shard:' "$log")"
for out in .mutation-gate scripts/.mutation-gate; do
  [[ -d $out ]] || continue
  echo "output dir $out: entries=$(find "$out" -mindepth 1 -maxdepth 1 | wc -l) files=$(find "$out" -type f | wc -l)"
  for f in "$out"/*.json; do [[ -f $f ]] && echo "$f: $(head -c 2000 "$f")"; done
done
echo "mutants dirs: $(find . -type d -name mutants -not -path './.git/*' | tr '\n' ' ')"
echo "new or changed clearance files: $(git status --porcelain -- mutation-clearances scripts/mutation-clearances | tee /dev/stderr | wc -l)"
exit 0
