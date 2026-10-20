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
python3 - <<'PY'
import os, re
from pathlib import Path

keep = re.compile(r'^(AH_|AGENTIHOOKS_|HIVE_|SWARM_|BRAIN_|LANGFUSE_|MCP_|OPENBAO_|REDIS_|SSL_CERT_FILE)')
lines = [f'{k}={v}' for k, v in sorted(os.environ.items()) if keep.match(k) and '\n' not in v]
target = Path(os.environ['AGENTIHOOKS_HOME']) / 'hive-container.env'
target.write_text('\n'.join(lines) + '\n')
target.chmod(0o600)
PY
if [[ -n "${HIVE_SSH_AUTHORIZED_KEYS_FILE:-}" ]]; then
  ssh_state="$AGENTIHOOKS_HOME/ssh"
  mkdir -p "$ssh_state"
  if [[ -n "${HIVE_SSH_HOST_KEY_FILE:-}" ]]; then
    install -m 0600 "$HIVE_SSH_HOST_KEY_FILE" "$ssh_state/host_key"
  elif [[ ! -f "$ssh_state/host_key" ]]; then
    ssh-keygen -q -t ed25519 -N '' -f "$ssh_state/host_key"
  fi
  cat > "$ssh_state/sshd_config" <<EOF
Port ${HIVE_SSH_PORT:-2222}
ListenAddress 0.0.0.0
HostKey $ssh_state/host_key
PidFile none
AuthorizedKeysFile $HIVE_SSH_AUTHORIZED_KEYS_FILE
AllowUsers $(id -un)
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes no
PermitRootLogin no
X11Forwarding no
SetEnv PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin AGENTIHOOKS_HOME=$AGENTIHOOKS_HOME
LogLevel VERBOSE
EOF
  /usr/sbin/sshd -D -e -f "$ssh_state/sshd_config" &
fi
bundle=()
[[ -d "$HIVE_REPO_ROOT/agentihooks-bundle" ]] && bundle=(--bundle "$HIVE_REPO_ROOT/agentihooks-bundle")
agentihooks init "${bundle[@]}" --profile "${HIVE_PROFILE:-engineer}" \
  || echo "worker-hive: agentihooks init failed; shells run without the profile" >&2
member="$(python3 -c 'import hooks.config; from scripts.swarm.commands import hive_id; print(hive_id())')"
agentihooks hive set "$member" ui=no ephemeral=yes \
  "roles=${HIVE_ROLES:-eng,ci,plan,dispatch}" "max-agents=${HIVE_MAX_AGENTS:-1}"
exec agentihooks hive run
