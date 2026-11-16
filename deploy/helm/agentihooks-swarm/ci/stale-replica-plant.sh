#!/usr/bin/env bash
set -euo pipefail

release=$1
step=$2
ledger_pod="$release-ledger-0"
stale=planted-gone-replica
hive="$(kubectl get deployment "$release-controller" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SWARM_HIVE_ID")].value}')"

if [[ $step == plant ]]; then
  kubectl exec -i "$ledger_pod" -- env HIVE="$hive" STALE="$stale" python - <<'PLANT'
import os

from scripts.swarm import replica
from scripts.swarm.store import connect

key = f"{replica.PREFIX}:{os.environ['HIVE']}"
connect().redis.set(key, os.environ["STALE"], px=120_000)
print(f"plant: a stale replica lock names a gone replica on {key}")
PLANT
  exit 0
fi

pod=""
line=""
for _ in $(seq 120); do
  for candidate in $(kubectl get pods -l "app.kubernetes.io/instance=$release,app.kubernetes.io/component=controller" -o jsonpath='{.items[*].metadata.name}'); do
    line="$(kubectl logs "$candidate" -c controller --timestamps 2>/dev/null | grep -m1 -F "stands by while $stale leads" || true)"
    if [[ -n $line ]]; then
      pod=$candidate
      break 2
    fi
  done
  sleep 0.5
done
if [[ -z $pod ]]; then
  printf 'plant: no restarted controller stood by behind the stale lock\n' >&2
  exit 1
fi
printf 'plant: %s\n' "$line"
standby_ms="$(python3 -c '
import calendar, re, sys, time
found = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", sys.argv[1])
print(int((calendar.timegm(time.strptime(found.group(1), "%Y-%m-%dT%H:%M:%S")) + float(found.group(2) or 0)) * 1000))
' "${line%% *}")"
lapse_ms=$((standby_ms + 6500))
probe="$(kubectl get deployment "$release-controller" -o jsonpath='{.spec.template.spec.containers[0].livenessProbe.exec.command[2]}')"
(
  wait_ms=$((lapse_ms + 1500 - $(date +%s%3N)))
  if ((wait_ms > 0)); then
    sleep "$((wait_ms / 1000)).$(printf '%03d' $((wait_ms % 1000)))"
  fi
  if out="$(kubectl exec "$pod" -c controller -- python -c "$probe" 2>&1)"; then rc=0; else rc=$?; fi
  printf 'plant: the liveness probe 1.5s after the lapse exited %s: %s\n' "$rc" "$(printf '%s' "$out" | grep -m1 -E 'Error' || printf '%s' "$out" | tail -1)"
) &
sampled=$!
set +e
kubectl exec -i "$ledger_pod" -- env HIVE="$hive" OWNER="$pod" STANDBY_MS="$standby_ms" LAPSE_MS="$lapse_ms" python - <<'MEASURE'
import os
import time

from scripts.swarm import replica
from scripts.swarm.store import connect

store = connect()
key = f"{replica.PREFIX}:{os.environ['HIVE']}"
owner = os.environ["OWNER"]
standby, lapse = int(os.environ["STANDBY_MS"]) / 1000, int(os.environ["LAPSE_MS"]) / 1000
set_after = time.time() - standby
if set_after > 4:
    raise SystemExit(f"plant: inconclusive, the lapse was set {set_after:.1f}s after the stand by, past the next poll")
store.redis.pexpireat(key, int(lapse * 1000))
print(f"plant: the stale lock lapses {lapse - standby:.1f}s after the stand by, set {set_after:.1f}s after it", flush=True)
took = None
while time.time() < lapse + 30:
    if store.redis.get(key) == owner:
        took = time.time()
        break
    time.sleep(0.05)
if took is None:
    raise SystemExit("plant: the standby never took the replica lock within 30s of the lapse")
late = took - lapse
print(f"plant: the standby took the replica lock {late:.2f}s after the stale lock lapsed", flush=True)
if late > 1.5:
    raise SystemExit(f"plant: red, the standby stood by {late:.2f}s past the lapse, inside the liveness probe window")
MEASURE
status=$?
set -e
wait "$sampled" || true
exit "$status"
