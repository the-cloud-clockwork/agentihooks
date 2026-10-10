#!/usr/bin/env bash
set -euo pipefail

proof_root="${1:?work directory required}"
proof_port="${HERDR_PROOF_PORT:-22488}"
proof_herdr="${HERDR_PROOF_BINARY:-$(command -v herdr)}"
proof_repo="$(git rev-parse --show-toplevel)"
proof_lock="$proof_repo/docker/swarm-node/versions.lock"
proof_user="$(id -un)"
proof_nonce="$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"

mkdir -p "$proof_root"/{bin,remote,client/bin,sshd}
proof_root="$(cd "$proof_root" && pwd)"
results="$proof_root/results.jsonl"
: > "$results"

record() {
    python3 -c 'import json, sys; print(json.dumps({"step": sys.argv[1], "exit": int(sys.argv[2]), "output": sys.argv[3]}))' \
        "$1" "$2" "$3" >> "$results"
}

pinned="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["tools"]["herdr"]["sha256"])' "$proof_lock")"
actual="$(sha256sum "$proof_herdr" | cut -d' ' -f1)"
record binary_checksum "$([[ "$pinned" == "$actual" ]] && echo 0 || echo 1)" "pinned=$pinned actual=$actual"
[[ "$pinned" == "$actual" ]] || { echo "herdr binary is not the pinned build" >&2; exit 1; }
cp "$proof_herdr" "$proof_root/bin/herdr"

ssh-keygen -q -t ed25519 -N '' -C hdr01-host -f "$proof_root/sshd/host_key"
ssh-keygen -q -t ed25519 -N '' -C hdr01-client -f "$proof_root/client/id"
cp "$proof_root/client/id.pub" "$proof_root/sshd/authorized_keys"
printf '[127.0.0.1]:%s %s\n' "$proof_port" "$(cut -d' ' -f1,2 "$proof_root/sshd/host_key.pub")" \
    > "$proof_root/client/known_hosts"

remote_path="$proof_root/bin:/usr/local/bin:/usr/bin:/bin"
cat > "$proof_root/sshd/remote-env" <<EOF
#!/usr/bin/env bash
exec env -i HOME="$proof_root/remote" USER="$proof_user" LOGNAME="$proof_user" SHELL=/bin/bash \
    PATH="$remote_path" TERM="\${TERM:-dumb}" /bin/bash -c "\${SSH_ORIGINAL_COMMAND:-exec /bin/bash}"
EOF
chmod 0755 "$proof_root/sshd/remote-env"
cat > "$proof_root/sshd/sshd_config" <<EOF
Port $proof_port
ListenAddress 127.0.0.1
HostKey $proof_root/sshd/host_key
AuthorizedKeysFile $proof_root/sshd/authorized_keys
PidFile $proof_root/sshd/sshd.pid
AllowUsers $proof_user
ForceCommand $proof_root/sshd/remote-env
StrictModes no
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitUserEnvironment no
LogLevel VERBOSE
EOF
cat > "$proof_root/client/bin/ssh" <<EOF
#!/usr/bin/env bash
exec /usr/bin/ssh -o IdentityFile="$proof_root/client/id" -o IdentitiesOnly=yes \
    -o UserKnownHostsFile="$proof_root/client/known_hosts" -o GlobalKnownHostsFile=/dev/null \
    -o StrictHostKeyChecking=yes -o BatchMode=yes "\$@"
EOF
chmod 0755 "$proof_root/client/bin/ssh"

remote() {
    env -i HOME="$proof_root/remote" USER="$proof_user" PATH="$remote_path" TERM=dumb "$@"
}
client() {
    env -i HOME="$proof_root/client" USER="$proof_user" PATH="$proof_root/client/bin:$remote_path" TERM=dumb \
        herdr "$@" < /dev/null
}

sshd_pid=""
server_pid=""
cleanup() {
    [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
    [[ -n "$sshd_pid" ]] && kill "$sshd_pid" 2>/dev/null || true
    rm -f "$proof_root/sshd/host_key" "$proof_root/client/id"
}
trap cleanup EXIT

/usr/sbin/sshd -f "$proof_root/sshd/sshd_config" -D -e 2> "$proof_root/sshd/sshd.log" &
sshd_pid=$!
for _ in $(seq 1 50); do
    grep -q "Server listening" "$proof_root/sshd/sshd.log" && break
    sleep 0.1
done

start_server() {
    local leader=()
    [[ "$2" == leader ]] && leader=(setsid)
    env -i HOME="$proof_root/remote" USER="$proof_user" PATH="$remote_path" TERM=dumb \
        "${leader[@]}" herdr server > "$proof_root/remote/server-$1.log" 2>&1 &
    server_pid=$!
    local status
    for _ in $(seq 1 100); do
        status="$(remote herdr status server --json 2>/dev/null || true)"
        [[ "$status" == *'"running":true'* ]] && return 0
        sleep 0.2
    done
    echo "remote herdr server did not start" >&2
    return 1
}

servers() {
    local proc
    for proc in /proc/[0-9]*; do
        [[ "$(readlink "$proc/exe" 2>/dev/null)" == "$proof_root/bin/herdr" ]] || continue
        [[ "$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null)" == "herdr server " ]] && printf '%s ' "${proc#/proc/}"
    done
    return 0
}

failures=0

step() {
    local name="$1" expected="$2"
    shift 2
    local out code=0
    out="$("$@" 2>&1)" || code=$?
    record "$name" "$code" "$out"
    if [[ "$code" == "$expected" ]]; then
        printf '%s exit=%s ok\n' "$name" "$code"
    else
        printf '%s exit=%s expected=%s FAILED\n' "$name" "$code" "$expected"
        failures=$((failures + 1))
    fi
    return 0
}

check() {
    local name="$1" detail="$2" held="$3"
    if [[ "$held" == yes ]]; then
        record "$name" 0 "$detail"
        printf '%s ok\n' "$name"
    else
        record "$name" 1 "$detail"
        printf '%s FAILED: %s\n' "$name" "$detail"
        failures=$((failures + 1))
    fi
    return 0
}

installed() {
    find "$proof_root/remote" -name herdr -type f -printf '%P\n' 2>/dev/null | sort | tr '\n' ' '
}

sockets() {
    find "$proof_root/client" -name '*.sock' -printf '%P\n' 2>/dev/null | sort | tr '\n' ' '
}

target="ssh://$proof_user@127.0.0.1:$proof_port"
remote_sum_before="$(sha256sum "$proof_root/bin/herdr" | cut -d' ' -f1)"
step client_status 0 client status client --json

start_server foreground member
member_pid="$server_pid"
step member_status 0 remote herdr status server --json
step member_machine_add 1 client machine add --label hdr01 "$target"
step member_machine_list 0 client machine list --json
check member_kept "pid=$member_pid servers=$(servers)" "$([[ "$(servers)" == "$member_pid " ]] && echo yes || echo no)"
saved="$(client machine list --json 2>&1 | tr -d ' \n')"
check member_not_saved "machines=$saved" "$([[ "$saved" == "[]" ]] && echo yes || echo no)"
kill "$member_pid"
wait "$member_pid" 2>/dev/null || true

start_server first leader
first_pid="$server_pid"
step remote_status_local 0 remote herdr status server --json
step machine_add 0 client machine add --label hdr01 "$target"
step machine_list 0 client machine list --json
step forwarded_status 0 client --machine hdr01 status server --json
step workspace_create 0 client --machine hdr01 workspace create --label "hdr01-$proof_nonce" \
    --cwd "$proof_root/remote" --no-focus

field() {
    python3 - "$results" "$1" "$2" <<'EOF'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1])]
reply = json.loads(next(r for r in rows if r["step"] == sys.argv[2])["output"])
value = reply.get("result", reply)
for key in sys.argv[3].split("."):
    value = value[key]
print(value)
EOF
}

workspace="$(field workspace_create workspace.workspace_id)"
root_pane="$(field workspace_create root_pane.pane_id)"
step pane_split 0 client --machine hdr01 pane split "$root_pane" --direction right --cwd "$proof_root/remote" --no-focus
pane="$(field pane_split pane.pane_id)"
step pane_run 0 client --machine hdr01 pane run "$pane" "echo launched-$proof_nonce"
step pane_wait_launch 0 client --machine hdr01 pane wait-output "$pane" --match "launched-$proof_nonce" \
    --timeout 10000
step pane_send_text 0 client --machine hdr01 pane send-text "$pane" "echo prompted-$proof_nonce"
step pane_send_keys 0 client --machine hdr01 pane send-keys "$pane" Enter
step pane_wait_prompt 0 client --machine hdr01 pane wait-output "$pane" --match "prompted-$proof_nonce" \
    --timeout 10000
step pane_read 0 client --machine hdr01 pane read "$pane" --lines 20
step pane_process_info 0 client --machine hdr01 pane process-info --pane "$pane"
step agent_list 0 client --machine hdr01 agent list
step pane_list 0 client --machine hdr01 pane list
step pane_close 0 client --machine hdr01 pane close "$pane"
step workspace_close 0 client --machine hdr01 workspace close "$workspace"
step unforwarded_update 2 client --machine hdr01 update
step unforwarded_session 2 client --machine hdr01 session list
step unforwarded_attach 2 client --machine hdr01 agent attach "$pane"

remote_sum_after="$(sha256sum "$proof_root/bin/herdr" | cut -d' ' -f1)"
check same_incarnation "pid=$first_pid servers=$(servers)" "$([[ "$(servers)" == "$first_pid " ]] && echo yes || echo no)"
check same_binary "before=$remote_sum_before after=$remote_sum_after" "$([[ "$remote_sum_before" == "$remote_sum_after" ]] && echo yes || echo no)"
check remote_installs "installed=$(installed)" "$([[ -z "$(installed)" ]] && echo yes || echo no)"
check client_local_socket "sockets=$(sockets)" "$([[ -z "$(sockets)" ]] && echo yes || echo no)"

kill "$first_pid"
wait "$first_pid" 2>/dev/null || true
server_pid=""
step stopped_workspace_list 1 client --machine hdr01 workspace list
step stopped_status 1 client --machine hdr01 status server --json
check stopped_remote_servers "servers=$(servers)" "$([[ -z "$(servers)" ]] && echo yes || echo no)"
check stopped_client_socket "sockets=$(sockets)" "$([[ -z "$(sockets)" ]] && echo yes || echo no)"

start_server second leader
check new_incarnation "pid=$server_pid previous=$first_pid" "$([[ "$server_pid" != "$first_pid" ]] && echo yes || echo no)"
step restarted_status 0 client --machine hdr01 status server --json
step restarted_workspace_list 0 client --machine hdr01 workspace list
check member_server_gone "member=$member_pid" "$(kill -0 "$member_pid" 2>/dev/null && echo no || echo yes)"
echo "results: $results failures: $failures"
[[ "$failures" == 0 ]]
