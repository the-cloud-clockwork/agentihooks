#!/usr/bin/env bash
set -euo pipefail
file=tests/test_swarm_v2_supervision.py
case "$LEG" in
  control-head | control-dev)
    [[ "$LEG" == control-dev ]] && git show origin/dev:"$file" > "$file"
    sed -i 's/"exporter", "0.1", "complete"/"exporter", "0.6", "complete"/' "$file"
    grep -c '"exporter", "0.6", "complete"' "$file"
    selector="$file::test_sigterm_during_tool_reports_observed_checkpoint[stale-incomplete]"
    ;;
  real-head)
    selector="$file::test_sigterm_during_tool_reports_observed_checkpoint"
    ;;
esac
rounds=20
failed=0
for round in $(seq "$rounds"); do
  if ! python -m pytest -q -p no:cacheprovider "$selector" > "round-$round.log" 2>&1; then
    failed=$((failed + 1))
    grep -E "^E " "round-$round.log" | head -3
  fi
  tail -1 "round-$round.log"
done
echo "PROBE-SUMMARY leg=$LEG rounds=$rounds failed=$failed"
[[ "$failed" -eq 0 ]]
