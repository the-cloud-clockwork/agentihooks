#!/usr/bin/env bash
set -euo pipefail

umask 077
export AGENTIHOOKS_HOME="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}"
export HIVE_REPO_ROOT="${HIVE_REPO_ROOT:-$HOME/dev}"
mkdir -p "$AGENTIHOOKS_HOME" "$HIVE_REPO_ROOT"
python3 - <<'PY'
import json, os, re, subprocess
from pathlib import Path

root = Path(os.environ['HIVE_REPO_ROOT'])
for repo in json.loads(os.environ.get('HIVE_REPOSITORIES', '[]')):
    name = repo['name']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise SystemExit('repository name must be a single directory name')
    target = root / name
    if not (target / '.git').is_dir():
        subprocess.run(['git', 'clone', '--branch', repo.get('ref', 'dev'), '--', repo['url'], str(target)], check=True)
PY

if [[ -n "${HIVE_CREDENTIAL_FILE:-}" ]]; then
  install -m 0600 "$HIVE_CREDENTIAL_FILE" "$AGENTIHOOKS_HOME/hive.env"
elif [[ ! -f "$AGENTIHOOKS_HOME/hive.env" ]]; then
  code="$(< "${HIVE_JOIN_CODE_FILE:?a mounted join code or member credential is required}")"
  agentihooks hive join "${HIVE_JOIN_URL:?the hive join URL is required}" "$code"
  unset code
fi
member="$(python3 -c 'import hooks.config; from scripts.swarm.commands import hive_id; print(hive_id())')"
agentihooks hive set "$member" ui=no ephemeral=yes \
  "roles=${HIVE_ROLES:-eng,ci,plan}" "max-agents=${HIVE_MAX_AGENTS:-1}"
exec agentihooks hive run
