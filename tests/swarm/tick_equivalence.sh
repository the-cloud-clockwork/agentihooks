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

pids=()
for side in base head; do
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
  side=base
  [[ "$index" == 0 ]] || side=head
  wait "${pids[$index]}" || failed=1
  cat "tick-evidence/$side.log"
done
[[ "$failed" == 0 ]]

shopt -s nullglob
for side in base head; do
  records=("tick-evidence/$side/"*.json)
  [[ "${#records[@]}" == 24 ]] || { echo "::error::Expected twenty four $side observations, found ${#records[@]}"; exit 1; }
done
if [[ "$declared" == 1 ]]; then
  echo "::notice::The head regenerates the tick goldens; base and head each match their own goldens and review confirms the change."
  diff -ru tick-evidence/base tick-evidence/head || true
else
  diff -ru tick-evidence/base tick-evidence/head
fi

cd head
export TICK_CHARACTERISATION_OUTPUT="$workspace/tick-evidence/plant"
export TICK_CHARACTERISATION_PLANT="${TICK_EQUIVALENCE_PLANT:-0}"
"$python_bin" -m pytest tests/swarm/tick_scenarios -n 0 --tb=short -k test_dropped_reap_action_breaks_golden
