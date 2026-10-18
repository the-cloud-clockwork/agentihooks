#!/usr/bin/env bash
set -euo pipefail

hash="$(printf '%s' "$SWARM_REDIS_PASSWORD" | sha256sum | cut -d' ' -f1)"
# Hive members are added at runtime and saved by ACL SAVE; the template users are rebuilt on every start.
# The default user stays off yet holds every command, because Redis replays each MULTI/EXEC in the AOF as that user.
root="${SWARM_REDIS_ROOT:-}"
hive_users="$(grep '^user hive-' "$root/data/users.acl" 2>/dev/null || true)"
deny=""
[[ -z "${SWARM_REDIS_ADOPTED:-}" ]] || deny=" -flushall -flushdb"
sed -e "s/@SWARM_REDIS_PASSWORD_SHA256@/$hash/" -e "s/@SWARM_REDIS_DENY@/$deny/" \
  "$root/compose/users.acl.template" > "$root/data/users.acl"
[[ -z "$hive_users" ]] || printf '%s\n' "$hive_users" >> "$root/data/users.acl"
args=(redis-server --appendonly yes --aclfile "$root/data/users.acl")
if [[ "$SWARM_REDIS_SCHEME" == rediss ]]; then
  install -o redis -g redis -m 0600 /tls/cert.pem /tls/key.pem /tls/ca.pem /run/swarm-tls/
  args+=(--port 0 --tls-port 6379 --tls-cert-file /run/swarm-tls/cert.pem --tls-key-file /run/swarm-tls/key.pem
    --tls-ca-cert-file /run/swarm-tls/ca.pem --tls-auth-clients no)
fi
exec docker-entrypoint.sh "${args[@]}"
