#!/usr/bin/env bash
# guard-daemon.sh — Process Guard Daemon v3 (WSL/Linux)
# Ghost detector: only kills processes with N+ instances hanging for timeout+.
# One-pass: load config, group processes, flag ghosts, kill if timed out, exit.
# Triggered every 60s by the agentihooks scheduler (agentihooks host-guard install).

set -euo pipefail

SCRIPT_DIR="$HOME"
CONFIG_PATH="$SCRIPT_DIR/guard-config.json"
LOG_PATH="$SCRIPT_DIR/guard-daemon.log"
LOG_BAK_PATH="$LOG_PATH.1"
STATE_PATH="$SCRIPT_DIR/guard-daemon-state.json"
LOG_MAX_SIZE_MB=10
DRY_RUN="${1:-}"

# --- Dependencies check ---
if ! command -v jq &>/dev/null; then
    echo "ERROR: jq required. Install with: sudo apt install jq" >&2
    exit 1
fi

# --- Log helper ---
guard_log() {
    local msg="$1"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $msg" >> "$LOG_PATH"
    if [[ -f "$LOG_PATH" ]]; then
        local size_bytes
        size_bytes=$(stat -c%s "$LOG_PATH" 2>/dev/null || echo 0)
        local max_bytes=$((LOG_MAX_SIZE_MB * 1048576))
        if (( size_bytes > max_bytes )); then
            mv -f "$LOG_PATH" "$LOG_BAK_PATH"
        fi
    fi
}

# --- Parse timeout to seconds ---
parse_timeout() {
    local val="$1"
    if [[ "$val" == "null" || -z "$val" ]]; then
        echo 3600
        return
    fi
    if [[ "$val" =~ ^([0-9]+)h$ ]]; then
        echo $(( ${BASH_REMATCH[1]} * 3600 ))
    elif [[ "$val" =~ ^([0-9]+)m$ ]]; then
        echo $(( ${BASH_REMATCH[1]} * 60 ))
    elif [[ "$val" =~ ^([0-9]+)s$ ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "$val" =~ ^[0-9]+$ ]]; then
        echo "$val"
    else
        echo 3600
    fi
}

# --- Load config ---
if [[ ! -f "$CONFIG_PATH" ]]; then
    guard_log "ERROR config_missing path=$CONFIG_PATH"
    exit 1
fi

CONFIG=$(cat "$CONFIG_PATH")
if ! echo "$CONFIG" | jq . &>/dev/null; then
    guard_log "ERROR config_malformed"
    exit 1
fi

LOG_MAX_SIZE_MB=$(echo "$CONFIG" | jq -r '.logMaxSizeMB // 10')
STATE_PATH_CFG=$(echo "$CONFIG" | jq -r '.stateFile // empty')
if [[ -n "$STATE_PATH_CFG" ]]; then
    STATE_PATH="$STATE_PATH_CFG"
fi

THRESHOLD=$(echo "$CONFIG" | jq -r '.threshold // 30')
TIMEOUT_RAW=$(echo "$CONFIG" | jq -r '.timeout // "1h"')
TIMEOUT_SEC=$(parse_timeout "$TIMEOUT_RAW")

# Build excludes set (lowercase)
declare -A EXCLUDES
while IFS= read -r name; do
    [[ -z "$name" || "$name" == "null" ]] && continue
    name_lower=$(echo "$name" | tr '[:upper:]' '[:lower:]')
    EXCLUDES["$name_lower"]=1
done < <(echo "$CONFIG" | jq -r '.excludes[]? // empty' 2>/dev/null)

if ! echo "$CONFIG" | jq -e '(.excludeCommands // []) | type == "array" and all(type == "string")' &>/dev/null; then
    guard_log "ERROR config_malformed excludeCommands"
    exit 1
fi
EXCLUDE_COMMANDS=()
while IFS= read -r pattern; do
    [[ -z "$pattern" ]] && continue
    rc=0
    [[ "" =~ $pattern ]] || rc=$?
    if (( rc == 2 )); then
        guard_log "ERROR config_malformed excludeCommands pattern=$pattern"
        exit 1
    fi
    EXCLUDE_COMMANDS+=("$pattern")
done < <(echo "$CONFIG" | jq -r '.excludeCommands // [] | .[]')

excluded_command() {
    local args pattern
    args=$(tr '\0' ' ' 2>/dev/null < "/proc/$1/cmdline" || true)
    for pattern in "${EXCLUDE_COMMANDS[@]}"; do
        [[ "$args" =~ $pattern ]] && return 0
    done
    return 1
}

# --- Load state (per process-name: firstFlagged timestamp) ---
declare -A STATE_FLAGGED
declare -A STATE_COUNT
if [[ -f "$STATE_PATH" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        pname=$(echo "$line" | jq -r '.key')
        flagged=$(echo "$line" | jq -r '.value.firstFlagged // empty')
        count=$(echo "$line" | jq -r '.value.count // 0')
        if [[ -n "$flagged" ]]; then
            STATE_FLAGGED["$pname"]="$flagged"
            STATE_COUNT["$pname"]="$count"
        fi
    done < <(jq -c 'to_entries[]' "$STATE_PATH" 2>/dev/null || true)
fi

NOW=$(date +%s)
NOW_ISO=$(date -Iseconds)

guard_log "TICK_START threshold=$THRESHOLD timeout=$TIMEOUT_RAW excludes=${#EXCLUDES[@]}"

kill_count=0
flag_count=0
error_count=0
declare -A NEW_STATE_FLAGGED
declare -A NEW_STATE_COUNT

MY_PID=$$

# --- Group processes by name and count ---
declare -A PROC_COUNTS
while IFS= read -r line; do
    count=$(echo "$line" | awk '{print $1}')
    name=$(echo "$line" | awk '{print $2}')
    [[ -z "$name" ]] && continue
    PROC_COUNTS["$name"]=$count
done < <(ps -u "$(whoami)" -o comm= --no-headers 2>/dev/null | sort | uniq -c | sort -rn)

for name in "${!PROC_COUNTS[@]}"; do
    count=${PROC_COUNTS[$name]}
    name_lower=$(echo "$name" | tr '[:upper:]' '[:lower:]')

    # Skip if below threshold
    (( count < THRESHOLD )) && continue

    # Skip excluded
    [[ -v "EXCLUDES[$name_lower]" ]] && continue

    # This group is over threshold
    ((flag_count++)) || true

    if [[ -v "STATE_FLAGGED[$name_lower]" ]]; then
        first_flagged="${STATE_FLAGGED[$name_lower]}"
        first_flagged_epoch=$(date -d "$first_flagged" +%s 2>/dev/null || echo "$NOW")
        elapsed=$(( NOW - first_flagged_epoch ))

        if (( elapsed >= TIMEOUT_SEC )); then
            # Ghost confirmed — kill all instances
            hrs=$(awk "BEGIN{printf \"%.1f\", $elapsed/3600}")
            while IFS= read -r pid; do
                [[ "$pid" == "$MY_PID" ]] && continue
                if excluded_command "$pid"; then
                    guard_log "SKIPPED pid=$pid name=$name reason=excluded_command"
                    continue
                fi
                if [[ "$DRY_RUN" == "--dry-run" ]]; then
                    guard_log "DRY_RUN would_kill pid=$pid name=$name reason=ghost_detected count=$count elapsed=${hrs}h"
                    ((kill_count++)) || true
                else
                    if kill -TERM "$pid" 2>/dev/null; then
                        sleep 0.5
                        kill -KILL "$pid" 2>/dev/null || true
                        guard_log "KILLED pid=$pid name=$name reason=ghost_detected count=$count elapsed=${hrs}h"
                        ((kill_count++)) || true
                    fi
                fi
            done < <(pgrep -u "$(whoami)" -x "$name" 2>/dev/null || true)
            # Don't carry to new state — they're dead
        else
            # Still within grace period
            mins=$(( elapsed / 60 ))
            guard_log "FLAGGED name=$name count=$count first_flagged=${mins}m_ago"
            NEW_STATE_FLAGGED["$name_lower"]="$first_flagged"
            NEW_STATE_COUNT["$name_lower"]="$count"
        fi
    else
        # First time over threshold
        guard_log "FLAGGED name=$name count=$count first_flagged=now"
        NEW_STATE_FLAGGED["$name_lower"]="$NOW_ISO"
        NEW_STATE_COUNT["$name_lower"]="$count"
    fi
done

# --- Save state ---
{
    echo "{"
    first=true
    for pname in "${!NEW_STATE_FLAGGED[@]}"; do
        if $first; then first=false; else echo ","; fi
        printf '  "%s": {"firstFlagged": "%s", "count": %s}' \
            "$pname" "${NEW_STATE_FLAGGED[$pname]}" "${NEW_STATE_COUNT[$pname]}"
    done
    echo ""
    echo "}"
} > "$STATE_PATH"

guard_log "TICK_COMPLETE flagged=$flag_count kills=$kill_count errors=$error_count"
