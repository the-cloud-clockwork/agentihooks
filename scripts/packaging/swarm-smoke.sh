#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
export COMPOSE_PROJECT_NAME
COMPOSE_PROJECT_NAME="$(python3 -c 'import uuid; print("swarm-proof-" + uuid.uuid4().hex[:12])')"
trap 'docker compose down --volumes' EXIT
docker compose up --build --wait --wait-timeout 120
python3 - <<'PY'
import json
import os
import urllib.request

base = f"http://127.0.0.1:{os.environ.get('SWARM_PORT', '8765')}"
for route in ("/healthz", "/", "/api/v1/ledgers"):
    with urllib.request.urlopen(base + route, timeout=5) as response:
        body = response.read()
        assert response.status == 200
        if route == "/":
            assert b"<!doctype html>" in body.lower() and b"<title>HOME</title>" in body
            print(f"GET {route}: 200, ledger home HTML ({len(body)} bytes)")
        else:
            print(f"GET {route}: 200, {json.loads(body)}")
payload = json.dumps({"capacity-box": {"height": 240}}).encode()
request = urllib.request.Request(base + "/api/v1/layout", data=payload, method="PUT", headers={"Content-Type": "application/json", "Origin": base})
with urllib.request.urlopen(request, timeout=5) as response:
    assert json.load(response)["data"]["capacity-box"]["height"] == 240
print("PUT /api/v1/layout: persisted height 240")
PY
docker compose exec -T swarm python -c 'import importlib.metadata as m, os; assert os.getuid() == 10001; packages = sorted(d.metadata["Name"] for d in m.distributions()); assert not {"mcp", "playwright", "tree-sitter", "opentelemetry-sdk"} & set(packages); print("uid:", os.getuid(), "packages:", packages)'
docker compose exec -T swarm python -c 'from scripts.swarm.store import connect; store = connect(); print("Redis connection: ready")'
docker compose up --force-recreate --no-deps --wait --wait-timeout 120 swarm
python3 - <<'PY'
import json
import os
import urllib.request

base = f"http://127.0.0.1:{os.environ.get('SWARM_PORT', '8765')}"
with urllib.request.urlopen(base + "/api/v1/layout", timeout=5) as response:
    assert json.load(response)["data"]["capacity-box"]["height"] == 240
print("After container recreation: persisted height 240")
PY
printf 'Swarm container smoke passed\n'
