#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${HIVE_GITHUB_AUTH_MODE:-}" ]]; then
  if [[ -n "${GITHUB_APP_PRIVATE_KEY:-}" ]]; then
    HIVE_GITHUB_AUTH_MODE=app
  elif [[ -n "${GITHUB_TOKEN:-}" ]]; then
    HIVE_GITHUB_AUTH_MODE=token
  fi
fi
if [[ -n "${HIVE_GITHUB_AUTH_MODE:-}" ]]; then
  export HIVE_GITHUB_AUTH_MODE
  if [[ "${AGENTIHOOKS_GITHUB_AUTH_READY:-}" != 1 ]]; then
    exec python3 -m scripts.github_app_cli exec bash "$0" "$@"
  fi
fi
umask 077
export AGENTIHOOKS_HOME="${AGENTIHOOKS_HOME:-$HOME/.agentihooks}"
export HIVE_REPO_ROOT="${HIVE_REPO_ROOT:-$HOME/dev}"
mkdir -p "$AGENTIHOOKS_HOME" "$HIVE_REPO_ROOT" "$HOME/dev/tcc-ecosystem"
venv="$HOME/dev/tcc-ecosystem/.venv"
[[ -e "$venv" && ! -L "$venv" ]] || ln -sfn "$(python3 -c 'import sys; print(sys.prefix)')" "$venv"
git config --global user.name >/dev/null || git config --global user.name "agentihooks hive"
git config --global user.email >/dev/null || git config --global user.email agentihooks-hive@users.noreply.github.com
git config --global credential.https://github.com.helper \
  "!$(command -v python3) -m scripts.github_app_cli credential"
if [[ "${HIVE_GITHUB_AUTH_MODE:-}" == app ]]; then
  token_file="$AGENTIHOOKS_HOME/github-token"
  python3 -m scripts.github_app_cli token > "$token_file.next"
  mv "$token_file.next" "$token_file"
fi
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
bundle_dir="$HIVE_REPO_ROOT/agentihooks-bundle"
if [[ -n "${HIVE_BUNDLE_URL:-}" && ! -d "$bundle_dir/.git" ]]; then
  git clone --branch "${HIVE_BUNDLE_REF:-dev}" -- "$HIVE_BUNDLE_URL" "$bundle_dir" \
    || echo "worker-hive: bundle clone failed; init runs on the package roles alone" >&2
fi

python3 - <<'PY'
import os, re, shlex, sys
from pathlib import Path

keep = re.compile(r'^(AH_|AGENTIHOOKS_|HIVE_|SWARM_|LANGFUSE_|SSL_CERT_FILE)')
tick_only = {'SWARM_SECRET', 'AGENTIHOOKS_LEDGER_AGENT_TOKEN'}
kept = {k: v for k, v in sorted(os.environ.items()) if keep.match(k) and k not in tick_only and '\n' not in v}
for k in [k for k, v in kept.items() if "'" in v or '\\' in v]:
    print(f'worker-hive: {k} holds a quote or backslash, which python-dotenv cannot read back; left out', file=sys.stderr)
    del kept[k]
lines = [f'{k}={shlex.quote(v)}' for k, v in kept.items()]
target = Path(os.environ['AGENTIHOOKS_HOME']) / 'hive-container.env'
target.write_text('\n'.join(lines) + '\n')
target.chmod(0o600)
PY
if [[ -n "${HIVE_SSH_AUTHORIZED_KEYS_FILE:-}" ]]; then
  sshd="${HIVE_SSHD:-/usr/sbin/sshd}"
  ssh_state="$AGENTIHOOKS_HOME/ssh"
  mkdir -p "$ssh_state"
  if [[ -n "${HIVE_SSH_HOST_KEY_FILE:-}" ]]; then
    install -m 0600 "$HIVE_SSH_HOST_KEY_FILE" "$ssh_state/host_key"
  elif [[ ! -f "$ssh_state/host_key" ]]; then
    ssh-keygen -q -t ed25519 -N '' -f "$ssh_state/host_key"
  fi
  # StrictModes off: the keys come from a read-only Secret mount the worker cannot write.
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
AllowTcpForwarding no
AllowAgentForwarding no
GatewayPorts no
PermitTunnel no
PermitUserEnvironment no
SetEnv PATH=$HOME/.local/bin:/opt/venv/bin:/usr/local/bin:/usr/bin:/bin NPM_CONFIG_PREFIX=$HOME/.local AGENTIHOOKS_HOME=$AGENTIHOOKS_HOME
LogLevel VERBOSE
EOF
  "$sshd" -t -f "$ssh_state/sshd_config"
  "$sshd" -D -e -f "$ssh_state/sshd_config" &
  sshd_pid=$!
  sleep 1
  kill -0 "$sshd_pid"
fi
ledger_credential() {
  grep -m1 '^AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL=' "$1" 2>/dev/null || true
}
if [[ -n "${HIVE_CREDENTIAL_FILE:-}" ]]; then
  # hive refresh retires the mounted password, so a restart keeps the saved refresh of the same member.
  if [[ ! -f "$AGENTIHOOKS_HOME/hive.env" ]] \
    || [[ "$(ledger_credential "$HIVE_CREDENTIAL_FILE")" != "$(ledger_credential "$AGENTIHOOKS_HOME/hive.env")" ]]; then
    install -m 0600 "$HIVE_CREDENTIAL_FILE" "$AGENTIHOOKS_HOME/hive.env"
  fi
elif [[ ! -f "$AGENTIHOOKS_HOME/hive.env" ]]; then
  wait_s="${HIVE_DELIVERY_WAIT_S:-900}"
  deadline=$(( $(date +%s) + wait_s ))
  until [[ -f "$AGENTIHOOKS_HOME/hive.env" ]]; do
    if (( $(date +%s) >= deadline )); then
      echo "worker-hive: no member credential was delivered within ${wait_s}s; run agentihooks hive add from the UI hive" >&2
      exit 1
    fi
    sleep 2
  done
fi
if [[ -n "${AGENTIHOOKS_HIVE_URL:-}" ]]; then
  # hive refresh from an ssh shell has no pod environment, so it reads the endpoint from hive.env.
  python3 - <<'PY'
import os
from pathlib import Path

saved = Path(os.environ['AGENTIHOOKS_HOME']) / 'hive.env'
kept = [line for line in saved.read_text().splitlines() if not line.startswith('AGENTIHOOKS_HIVE_URL=')]
saved.write_text('\n'.join([f"AGENTIHOOKS_HIVE_URL={os.environ['AGENTIHOOKS_HIVE_URL']}", *kept]) + '\n')
PY
fi
bundle=()
[[ -n "${HIVE_BUNDLE_URL:-}" && -d "$bundle_dir/.git" ]] && bundle=(--bundle "$bundle_dir")
python3 -m scripts.hive.linked_profiles
if (( ${#bundle[@]} )) && ! agentihooks init "${bundle[@]}"; then
  echo "worker-hive: agentihooks init with the bundle failed; init runs on the package roles alone" >&2
  bundle=()
fi
if (( ! ${#bundle[@]} )); then
  agentihooks bundle unlink
  agentihooks init
fi
# init writes the hosted ledger CA; agents read only this file, never the multi-line LEDGER_CA_PEM.
if [[ -n "${LEDGER_CA_PEM:-}" ]]; then
  python3 - <<'PY'
import os, ssl
from pathlib import Path

from scripts.swarm_ledger import ledger_ca

roots = ssl.get_default_verify_paths().cafile
if not roots:
    raise SystemExit('worker-hive: no system CA file to keep beside LEDGER_CA_PEM')
system = Path(roots).read_text()
if ledger_ca.place(Path(os.environ['AGENTIHOOKS_HOME']), system.rstrip() + '\n' + os.environ['LEDGER_CA_PEM'].strip() + '\n') is None:
    raise SystemExit('worker-hive: LEDGER_CA_PEM holds no valid certificate')
PY
fi
unset LEDGER_CA_PEM
agentihooks deps ensure \
  || echo "worker-hive: agentihooks deps ensure failed; each launch retries the bundle dependencies" >&2
member="$(python3 -c 'import hooks.config; from scripts.swarm.commands import hive_id; print(hive_id())')"
agentihooks hive set --keep "$member" ui=no ephemeral=yes draining=no \
  "excluded=${HIVE_EXCLUDED-master}" "max-agents=${HIVE_MAX_AGENTS:-20}"
exec agentihooks hive run
