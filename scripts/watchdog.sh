#!/bin/bash
# watchdog.sh — system monitor, alerter, and mining pauser
# Monitors CPU, RAM, processes, and customer container activity
# Sends ntfy alerts and pauses mining when customer is active

LOGFILE=~/watchdog.log
NTFY_TOPIC="vastai-notifs"
STATE_FILE=/tmp/watchdog_state

# Thresholds
CPU_ALERT=22          # Load average alert threshold (per core = 15/24 = ~63%)
RAM_ALERT=80          # RAM % alert threshold
PROC_CPU_ALERT=1900     # Single process CPU % alert threshold
PROC_STRIKES=3        # Consecutive checks before alerting on high process CPU
CHECK_INTERVAL=60     # Seconds between checks

# State tracking
declare -A proc_strikes  # Track consecutive high-CPU checks per process

notify() {
    local title="$1" priority="$2" tags="$3" msg="$4"
    curl -s -o /dev/null -X POST \
        -H "Title: $title" \
        -H "Priority: $priority" \
        -H "Tags: $tags" \
        -d "$msg" \
        https://ntfy.sh/$NTFY_TOPIC &
}

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> $LOGFILE
}

get_load() {
    awk '{print $1}' /proc/loadavg
}

get_ram_pct() {
    free | awk '/^Mem:/{printf "%.0f", $3/$2*100}'
}

get_customer_container() {
    docker ps --format "{{.Names}}" 2>/dev/null | grep "^C\." | head -1
}

get_container_cpu() {
    local name="$1"
    docker stats "$name" --no-stream --format "{{.CPUPerc}}" 2>/dev/null | tr -d '%'
}

pause_mining() {
    log "PAUSING mining — customer active"
    touch ~/disable-lolminer-5090
    touch ~/disable-xmrig
}

resume_mining() {
    log "RESUMING mining — customer idle/gone"
    rm -f ~/disable-xmrig
}

# Load state
LAST_CPU_ALERT=0
LAST_RAM_ALERT=0
LAST_PROC_ALERT=0
CUSTOMER_WAS_ACTIVE=false
CUSTOMER_IDLE_CHECKS=0

if [ -f "$STATE_FILE" ]; then
    source "$STATE_FILE"
fi

save_state() {
    cat > "$STATE_FILE" << EOF
LAST_CPU_ALERT=$LAST_CPU_ALERT
LAST_RAM_ALERT=$LAST_RAM_ALERT
LAST_PROC_ALERT=$LAST_PROC_ALERT
CUSTOMER_WAS_ACTIVE=$CUSTOMER_WAS_ACTIVE
CUSTOMER_IDLE_CHECKS=$CUSTOMER_IDLE_CHECKS
EOF
}

log "=== Watchdog started ==="

while true; do
    NOW=$(date +%s)
    LOAD=$(get_load)
    RAM_PCT=$(get_ram_pct)
    CONTAINER=$(get_customer_container)

    # ── CPU load alert ────────────────────────────────────────────────────────
    LOAD_INT=${LOAD%.*}
    if [ "$LOAD_INT" -ge "$CPU_ALERT" ] && [ $((NOW - LAST_CPU_ALERT)) -gt 300 ]; then
        log "ALERT: High load average: $LOAD"
        notify "⚠️ High CPU Load" "high" "warning" \
            "Load average: $LOAD on blackwell-node-01"
        LAST_CPU_ALERT=$NOW
    fi

    # ── RAM alert + cache drop ────────────────────────────────────────────────
    if [ "$RAM_PCT" -ge "$RAM_ALERT" ] && [ $((NOW - LAST_RAM_ALERT)) -gt 300 ]; then
        log "ALERT: High RAM usage: ${RAM_PCT}%"
        notify "⚠️ High RAM Usage" "high" "warning" \
            "RAM at ${RAM_PCT}% on blackwell-node-01"
        LAST_RAM_ALERT=$NOW
    fi

    # ── Auto cache drop at 85% to prevent OOM ─────────────────────────────────
    if [ "$RAM_PCT" -ge 85 ]; then
        AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')
        if [ "$AVAIL_MB" -lt 6144 ]; then
            sync && echo 3 | sudo tee /proc/sys/vm/drop_caches > /dev/null
            log "INFO: Dropped page cache (RAM=${RAM_PCT}%, avail=${AVAIL_MB}MB)"
        fi
    fi

    # ── Per-process CPU check ─────────────────────────────────────────────────
    while IFS= read -r line; do
        CPU=$(echo "$line" | awk '{print $1}' | cut -d. -f1)
        PID=$(echo "$line" | awk '{print $2}')
        CMD=$(echo "$line" | awk '{print $11}' | xargs basename 2>/dev/null)

        # Skip known heavy processes
        case "$CMD" in
            qemu-system-x86_64|python3|dockerd|containerd) continue ;;
        esac

        if [ "$CPU" -ge "$PROC_CPU_ALERT" ]; then
            proc_strikes[$PID]=$(( ${proc_strikes[$PID]:-0} + 1 ))
            if [ "${proc_strikes[$PID]}" -ge "$PROC_STRIKES" ] && [ $((NOW - LAST_PROC_ALERT)) -gt 300 ]; then
                log "ALERT: Process $CMD (PID $PID) at ${CPU}% CPU for ${proc_strikes[$PID]} checks"
                notify "🔥 High CPU Process" "urgent" "fire" \
                    "Process: $CMD (PID $PID) at ${CPU}% CPU — ${proc_strikes[$PID]} consecutive checks"
                LAST_PROC_ALERT=$NOW
            fi
        else
            proc_strikes[$PID]=0
        fi
    done < <(ps aux --no-headers --sort=-%cpu | head -20 | awk '{print $3, $2, $11}')

    # ── Customer container activity monitoring ────────────────────────────────
    if [ -n "$CONTAINER" ]; then
        CONTAINER_CPU=$(get_container_cpu "$CONTAINER")
        CONTAINER_CPU_INT=${CONTAINER_CPU%.*}
        CONTAINER_CPU_INT=${CONTAINER_CPU_INT:-0}

        if [ "$CONTAINER_CPU_INT" -ge 10 ]; then
            # Customer is active
            if [ "$CUSTOMER_WAS_ACTIVE" = false ]; then
                log "Customer container $CONTAINER became ACTIVE (CPU: ${CONTAINER_CPU}%)"
                pause_mining
                CUSTOMER_WAS_ACTIVE=true
                CUSTOMER_IDLE_CHECKS=0
            fi
        else
            # Customer is idle
            if [ "$CUSTOMER_WAS_ACTIVE" = true ]; then
                CUSTOMER_IDLE_CHECKS=$((CUSTOMER_IDLE_CHECKS + 1))
                if [ "$CUSTOMER_IDLE_CHECKS" -ge 5 ]; then
                    log "Customer container $CONTAINER idle for 5 checks — resuming mining"
                    resume_mining
                    CUSTOMER_WAS_ACTIVE=false
                    CUSTOMER_IDLE_CHECKS=0
                fi
            fi
        fi
    else
        # No container — ensure mining is running
        if [ "$CUSTOMER_WAS_ACTIVE" = true ]; then
            log "Customer container gone — resuming mining"
            resume_mining
            CUSTOMER_WAS_ACTIVE=false
            CUSTOMER_IDLE_CHECKS=0
        fi
    fi

    # ── Periodic status log ───────────────────────────────────────────────────
   TICK=$(($(date +%s) % 300))
   if [ "$TICK" -lt 60 ]; then        RAM_USED=$(free -h | awk '/^Mem:/{print $3}')
        RAM_TOTAL=$(free -h | awk '/^Mem:/{print $2}')
        log "STATUS | load:$LOAD | ram:${RAM_USED}/${RAM_TOTAL} (${RAM_PCT}%) | container:${CONTAINER:-none} | customer_active:$CUSTOMER_WAS_ACTIVE"
    fi

    save_state
    sleep $CHECK_INTERVAL
done
