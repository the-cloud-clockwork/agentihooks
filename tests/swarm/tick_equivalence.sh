#!/usr/bin/env bash
set -euo pipefail

workspace="$(pwd)"
python_bin="$(command -v python)"
mkdir tick-evidence
golden=tests/swarm/tick_scenarios/goldens.json
declared=0
if [[ -f "base/$golden" ]] && ! cmp -s "base/$golden" "head/$golden"; then
  declared=1
  cp "base/$golden" tick-evidence/base-goldens.json
fi
cp -R head/tests/swarm/tick_scenarios base/tests/swarm/tick_scenarios-head
if [[ -d base/tests/swarm/tick_scenarios ]]; then
  rm -r base/tests/swarm/tick_scenarios
fi
mv base/tests/swarm/tick_scenarios-head base/tests/swarm/tick_scenarios
if [[ "$declared" == 1 ]]; then
  mv tick-evidence/base-goldens.json "base/$golden"
fi
mapfile -t added < <("$python_bin" -c '
import json, sys
base, head = (json.load(open(path)) for path in sys.argv[1:])
for key in sorted(set(head) - set(base)):
    entry, scenario = key.split("/", 1)
    print(f"tests/swarm/tick_scenarios/test_characterisation.py::test_tick_matches_golden[{scenario}-{entry}]")
' "base/$golden" "head/$golden")
head_count="$("$python_bin" -c 'import json, sys; print(len(json.load(open(sys.argv[1]))))' "head/$golden")"
base_count=$((head_count - ${#added[@]}))
base_args=()
for node in "${added[@]}"; do
  base_args+=(--deselect "$node")
done

pids=()
for side in base head; do
  (
    cd "$side"
    unset TICK_CHARACTERISATION_PLANT
    export TICK_CHARACTERISATION_OUTPUT="$workspace/tick-evidence/$side"
    side_args=()
    [[ "$side" == head ]] || side_args=("${base_args[@]}")
    "$python_bin" -m pytest tests/swarm/tick_scenarios -n 0 --tb=short -k test_tick_matches_golden "${side_args[@]}"
  ) > "tick-evidence/$side.log" 2>&1 &
  pids+=("$!")
done

failed=0
for index in "${!pids[@]}"; do
  side=base
  [[ "$index" == 0 ]] || side=head
  wait "${pids[$index]}" || failed=1
  cat "tick-evidence/$side.log"
done
[[ "$failed" == 0 ]]

shopt -s nullglob
for side in base head; do
  records=("tick-evidence/$side/"*.json)
  expected="$head_count"
  [[ "$side" == head ]] || expected="$base_count"
  [[ "${#records[@]}" == "$expected" ]] || { echo "::error::Expected $expected $side observations, found ${#records[@]}"; exit 1; }
done
if [[ "$declared" == 1 ]]; then
  echo "::notice::The head regenerates the tick goldens; base and head each match their own goldens, the observation diff below does not block, and code owner review of the goldens gates the change."
  status=0
  diff -ru tick-evidence/base tick-evidence/head > tick-evidence/declared.diff || status=$?
  cat tick-evidence/declared.diff
  [[ "$status" -le 1 ]] || { echo "::error::Comparing base and head observations failed with status $status"; exit 1; }
else
  diff -ru tick-evidence/base tick-evidence/head
fi

cd head
export TICK_CHARACTERISATION_OUTPUT="$workspace/tick-evidence/plant"
export TICK_CHARACTERISATION_PLANT="${TICK_EQUIVALENCE_PLANT:-0}"
"$python_bin" -m pytest tests/swarm/tick_scenarios -n 0 --tb=short -k test_dropped_reap_action_breaks_golden
