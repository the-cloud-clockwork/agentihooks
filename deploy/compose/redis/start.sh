#!/usr/bin/env bash
set -euo pipefail

hash="$(printf '%s' "$SWARM_REDIS_PASSWORD" | sha256sum | cut -d' ' -f1)"
sed "s/@SWARM_REDIS_PASSWORD_SHA256@/$hash/" /compose/users.acl.template > /data/users.acl
args=(redis-server --appendonly yes --aclfile /data/users.acl)
if [[ "$SWARM_REDIS_SCHEME" == rediss ]]; then
  mkdir -p /data/tls
  cp /tls/cert.pem /tls/key.pem /tls/ca.pem /data/tls/
  args+=(--port 0 --tls-port 6379 --tls-cert-file /data/tls/cert.pem --tls-key-file /data/tls/key.pem
    --tls-ca-cert-file /data/tls/ca.pem --tls-auth-clients no)
fi
exec docker-entrypoint.sh "${args[@]}"
