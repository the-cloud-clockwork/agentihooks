#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
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
            assert b"<html" in body.lower()
            print(f"GET {route}: 200, ledger home HTML ({len(body)} bytes)")
        else:
            print(f"GET {route}: 200, {json.loads(body)}")
PY
docker compose exec -T swarm python -c 'import importlib.metadata as m, os; assert os.getuid() == 10001; packages = sorted(d.metadata["Name"] for d in m.distributions()); assert not {"mcp", "playwright", "tree-sitter", "opentelemetry-sdk"} & set(packages); print("uid:", os.getuid(), "packages:", packages)'
docker compose exec -T swarm python -c 'from scripts.swarm.store import connect; store = connect(); print("Redis connection: ready")'
printf 'Swarm container smoke passed\n'
