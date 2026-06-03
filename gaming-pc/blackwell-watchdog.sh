#!/bin/bash
# Gaming PC → Blackwell SSH watchdog
# Runs every 60s, alerts via ntfy if Blackwell is unreachable

BLACKWELL_IP="192.168.50.51"
BLACKWELL_USER="rich-rob"
NTFY_TOPIC="blackwell-alerts"
FAIL_COUNT=0
MAX_FAILS=2  # 3 consecutive failures = alert (3 mins)
LAST_ALERT=0
ALERT_COOLDOWN=1800  # re-alert every 30 mins if still down

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [blackwell-watchdog] $*"; }
source "$HOME/.watchdog_secrets"

pushover() {
    curl -s \
        --form-string "token=${PUSHOVER_TOKEN}" \
        --form-string "user=${PUSHOVER_USER}" \
        --form-string "title=🚨 Blackwell Down" \
        --form-string "message=$1" \
        --form-string "priority=2" \
        --form-string "retry=60" \
        --form-string "expire=3600" \
        --form-string "sound=siren" \
        "https://api.pushover.net/1/messages.json" > /dev/null 2>&1
}


ntfy() {
    curl -s -d "$1" "https://ntfy.sh/${NTFY_TOPIC}" \
        -H "Title: 🚨 Blackwell Down" \
        -H "Priority: urgent" \
        -H "Tags: rotating_light" > /dev/null 2>&1
}



# ── Kaalia health check state ────────────────────────────────────────────────
KAALIA_LAST_ALERT=0
KAALIA_ALERT_COOLDOWN=1800   # re-alert every 30 min while unhealthy
PREV_KAALIA_PID=""

kaalia_warn() {
    # lower-priority warning channel (not the siren) — machine is up but Vast-unhealthy
    curl -s -d "$1" "https://ntfy.sh/${NTFY_TOPIC}" \
        -H "Title: ⚠️ Kaalia Unhealthy" \
        -H "Priority: high" \
        -H "Tags: warning" > /dev/null 2>&1
    curl -s \
        --form-string "token=${PUSHOVER_TOKEN}" \
        --form-string "user=${PUSHOVER_USER}" \
        --form-string "title=⚠️ Kaalia Unhealthy" \
        --form-string "message=$1" \
        --form-string "priority=1" \
        "https://api.pushover.net/1/messages.json" > /dev/null 2>&1
}

check_kaalia_health() {
    # Runs only when SSH is up. Detects: (a) kaalia crash-loop via PID churn,
    # (b) Dead / inspect-hung customer containers. Alert-only.
    local probe
    probe=$(ssh -i /home/rich-rob/.ssh/id_ed25519 -o ConnectTimeout=10 -o BatchMode=yes \
        -o StrictHostKeyChecking=no ${BLACKWELL_USER}@${BLACKWELL_IP} '
        KPID=$(pgrep -f "latest/kaalia" | head -1)
        echo "KPID=$KPID"
        DEAD=$(timeout 15 docker ps -a --format "{{.Names}} {{.Status}}" 2>/dev/null | grep -c "Dead")
        echo "DEAD=$DEAD"
        HUNG=0
        for c in $(timeout 10 docker ps -a --format "{{.Names}}" 2>/dev/null | grep "^C\."); do
            timeout 8 docker inspect "$c" >/dev/null 2>&1 || HUNG=$((HUNG+1))
        done
        echo "HUNG=$HUNG"
    ' 2>/dev/null)

    local kpid dead hung
    kpid=$(echo "$probe" | grep "^KPID=" | cut -d= -f2)
    dead=$(echo "$probe" | grep "^DEAD=" | cut -d= -f2)
    hung=$(echo "$probe" | grep "^HUNG=" | cut -d= -f2)

    local problem=""
    # crash-loop: kaalia PID changed between probes (it churns every ~2 min when looping)
    if [ -n "$PREV_KAALIA_PID" ] && [ -n "$kpid" ] && [ "$kpid" != "$PREV_KAALIA_PID" ]; then
        problem="kaalia PID changed ($PREV_KAALIA_PID -> $kpid) — possible crash-loop"
    fi
    [ -z "$kpid" ] && problem="kaalia process not found"
    [ -n "$dead" ] && [ "$dead" -gt 0 ] 2>/dev/null && problem="$problem; $dead Dead container(s)"
    [ -n "$hung" ] && [ "$hung" -gt 0 ] 2>/dev/null && problem="$problem; $hung container(s) hang on docker inspect"

    PREV_KAALIA_PID="$kpid"

    if [ -n "$problem" ]; then
        local now=$(date +%s)
        if [ $((now - KAALIA_LAST_ALERT)) -gt $KAALIA_ALERT_COOLDOWN ] || [ $KAALIA_LAST_ALERT -eq 0 ]; then
            log "KAALIA UNHEALTHY: $problem"
            kaalia_warn "Blackwell is UP but kaalia looks unhealthy: ${problem}. Vast may be offline / not billing. Check now."
            KAALIA_LAST_ALERT=$now
        fi
    else
        KAALIA_LAST_ALERT=0
    fi
}

log "=== Blackwell watchdog starting ==="

while true; do
    # Try SSH with 10s timeout
    if ssh -i /home/rich-rob/.ssh/id_ed25519 \
           -o ConnectTimeout=10 \
           -o BatchMode=yes \
           -o StrictHostKeyChecking=no \
           ${BLACKWELL_USER}@${BLACKWELL_IP} \
           "echo ok" > /dev/null 2>&1; then
        if [ $FAIL_COUNT -gt 0 ]; then
            log "Blackwell recovered after $FAIL_COUNT failed attempts"
            ntfy "✅ Blackwell is back online after ${FAIL_COUNT} failed SSH attempts"
            pushover "✅ Blackwell is back online after ${FAIL_COUNT} failed SSH attempts"
            # Also send recovery to vastai-notifs
            curl -s -d "Blackwell recovered — check mining + Vast.ai status" \
                "https://ntfy.sh/vastai-notifs" \
                -H "Title: ✅ Blackwell Recovered" \
                -H "Priority: high" > /dev/null 2>&1
        fi
        FAIL_COUNT=0
        LAST_ALERT=0
        check_kaalia_health
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        log "SSH failed (attempt $FAIL_COUNT/$MAX_FAILS)"
        
        if [ $FAIL_COUNT -ge $MAX_FAILS ]; then
            NOW=$(date +%s)
            if [ $((NOW - LAST_ALERT)) -gt $ALERT_COOLDOWN ] || [ $LAST_ALERT -eq 0 ]; then
                log "ALERTING: Blackwell unreachable for $((FAIL_COUNT * 60))s"
                ntfy "Blackwell unreachable for $((FAIL_COUNT * 60))s — may be down! Check power + network. Last seen: $(date)"
                pushover "Blackwell unreachable for $((FAIL_COUNT * 60))s — may be down! Check power + network. Last seen: $(date)"
                LAST_ALERT=$NOW
            fi
        fi
    fi
    
    sleep 60
done
