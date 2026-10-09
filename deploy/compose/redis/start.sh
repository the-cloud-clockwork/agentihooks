#!/usr/bin/env bash
set -euo pipefail

hash="$(printf '%s' "$SWARM_REDIS_PASSWORD" | sha256sum | cut -d' ' -f1)"
sed "s/@SWARM_REDIS_PASSWORD_SHA256@/$hash/" /compose/users.acl.template > /data/users.acl
args=(redis-server --appendonly yes --aclfile /data/users.acl)
if [[ "$SWARM_REDIS_SCHEME" == rediss ]]; then
  install -o redis -g redis -m 0600 /tls/cert.pem /tls/key.pem /tls/ca.pem /run/swarm-tls/
  args+=(--port 0 --tls-port 6379 --tls-cert-file /run/swarm-tls/cert.pem --tls-key-file /run/swarm-tls/key.pem
    --tls-ca-cert-file /run/swarm-tls/ca.pem --tls-auth-clients no)
fi
exec docker-entrypoint.sh "${args[@]}"
