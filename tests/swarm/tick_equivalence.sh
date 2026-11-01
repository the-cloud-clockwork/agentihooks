#!/usr/bin/env bash
set -euo pipefail

workspace="$(pwd)"
python_bin="$(command -v python)"
mkdir tick-evidence
sides=(base head)
golden=tests/swarm/tick_scenarios/goldens.json
if [[ -f "base/$golden" ]] && ! cmp -s "base/$golden" "head/$golden"; then
  sides=(head)
  echo "::notice::The head changes the tick goldens, so its characterisation grades the declared behaviour without base equivalence."
fi
cp -R head/tests/swarm/tick_scenarios base/tests/swarm/tick_scenarios-head
if [[ -d base/tests/swarm/tick_scenarios ]]; then
  rm -r base/tests/swarm/tick_scenarios
fi
mv base/tests/swarm/tick_scenarios-head base/tests/swarm/tick_scenarios

pids=()
for side in "${sides[@]}"; do
  (
    cd "$side"
    unset TICK_CHARACTERISATION_PLANT
    export TICK_CHARACTERISATION_OUTPUT="$workspace/tick-evidence/$side"
    "$python_bin" -m pytest tests/swarm/tick_scenarios -n 0 --tb=short -k test_tick_matches_golden
  ) > "tick-evidence/$side.log" 2>&1 &
  pids+=("$!")
done

failed=0
for index in "${!pids[@]}"; do
  side="${sides[$index]}"
  wait "${pids[$index]}" || failed=1
  cat "tick-evidence/$side.log"
done
[[ "$failed" == 0 ]]

shopt -s nullglob
for side in "${sides[@]}"; do
  records=("tick-evidence/$side/"*.json)
  [[ "${#records[@]}" == 24 ]] || { echo "::error::Expected twenty four $side observations, found ${#records[@]}"; exit 1; }
done
if [[ "${#sides[@]}" == 2 ]]; then
  diff -ru tick-evidence/base tick-evidence/head
fi

cd head
export TICK_CHARACTERISATION_OUTPUT="$workspace/tick-evidence/plant"
export TICK_CHARACTERISATION_PLANT="${TICK_EQUIVALENCE_PLANT:-0}"
"$python_bin" -m pytest tests/swarm/tick_scenarios -n 0 --tb=short -k test_dropped_reap_action_breaks_golden
