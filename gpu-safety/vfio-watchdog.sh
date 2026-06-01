#!/bin/bash
# VFIO Watchdog — protects RTX 5070 from Vast.ai exposure
# Runs every 10 seconds, ensures 5070 stays hidden when VM is down

DOMAIN="mining-ai-vm"
GPU_PCI="0000:03:00.0"
AUD_PCI="0000:03:00.1"
NTFY_TOPIC="blackwell-alerts"
LOG="/home/rich-rob/vfio-watchdog.log"
RECOVERY_ATTEMPTS=0
MAX_ATTEMPTS=5
LAST_STATE=""

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [vfio-watchdog] $*" | tee -a "$LOG"
}

ntfy() {
    curl -s -d "$1" "https://ntfy.sh/${NTFY_TOPIC}" \
        -H "Title: VFIO Watchdog" \
        -H "Priority: urgent" \
        -H "Tags: warning" > /dev/null 2>&1
}

VFIO_LOCK="/tmp/vfio-watchdog.lock"

bind_vfio() {
    local dev=$1
    # Acquire lock — prevent concurrent PCI rebinding
    exec 9>"$VFIO_LOCK"
    if ! flock -w 30 9; then
        log "Could not acquire lock for bind_vfio — skipping"
        echo "locked"
        return 1
    fi
    # Unbind from any current driver
    local current_driver=$(readlink /sys/bus/pci/devices/${dev}/driver 2>/dev/null | xargs basename 2>/dev/null)
    if [ -n "$current_driver" ] && [ "$current_driver" != "vfio-pci" ]; then
        echo "$dev" > /sys/bus/pci/drivers/${current_driver}/unbind 2>/dev/null
    fi
    # Force vfio-pci
    echo "vfio-pci" > /sys/bus/pci/devices/${dev}/driver_override 2>/dev/null
    echo "$dev" > /sys/bus/pci/drivers_probe 2>/dev/null
    sleep 2
    # Verify
    local new_driver=$(readlink /sys/bus/pci/devices/${dev}/driver 2>/dev/null | xargs basename 2>/dev/null)
    flock -u 9
    echo "$new_driver"
}

hide_gpu() {
    log "Hiding RTX 5070 from host..."
    local d1=$(bind_vfio $GPU_PCI)
    local d2=$(bind_vfio $AUD_PCI)
    log "GPU driver: $d1 | Audio driver: $d2"
    if [ "$d1" = "vfio-pci" ]; then
        log "RTX 5070 successfully hidden (vfio-pci bound)"
        return 0
    else
        log "WARNING: Could not bind vfio-pci to GPU!"
        return 1
    fi
}

attempt_recovery() {
    RECOVERY_ATTEMPTS=$((RECOVERY_ATTEMPTS + 1))
    log "Recovery attempt $RECOVERY_ATTEMPTS/$MAX_ATTEMPTS..."
    
    # Always hide GPU first
    hide_gpu
    
    if [ $RECOVERY_ATTEMPTS -gt $MAX_ATTEMPTS ]; then
        log "Max recovery attempts reached — GPU hidden, giving up auto-restart"
        ntfy "Ghost-VM failed to recover after $MAX_ATTEMPTS attempts. GPU hidden. Manual intervention needed."
        return 1
    fi
    
    # Wait before attempting restart
    sleep $((RECOVERY_ATTEMPTS * 10))
    
    log "Attempting VM start..."
    virsh start $DOMAIN > /dev/null 2>&1
    sleep 15
    
    local state=$(virsh domstate $DOMAIN 2>/dev/null)
    if [ "$state" = "running" ] || [ "$state" = "paused" ]; then
        log "VM recovered successfully! State: $state"
        RECOVERY_ATTEMPTS=0
        ntfy "Ghost-VM recovered successfully after crash. State: $state"
        return 0
    else
        log "VM still not running after restart attempt. State: $state"
        return 1
    fi
}

log "=== VFIO Watchdog starting ==="

while true; do
    STATE=$(virsh domstate $DOMAIN 2>/dev/null)
    
    if [ "$STATE" = "running" ] || [ "$STATE" = "paused" ]; then
        # VM is healthy - reset recovery counter if it was previously recovering
        if [ $RECOVERY_ATTEMPTS -gt 0 ]; then
            log "VM healthy (state: $STATE) - resetting recovery counter"
            RECOVERY_ATTEMPTS=0
        fi
        # Verify GPU is still hidden from nvidia-smi
        GPU_VISIBLE=$(nvidia-smi -L 2>/dev/null | grep -c "5070" || true)
        if [ "$GPU_VISIBLE" -gt 0 ]; then
            log "WARNING: RTX 5070 visible to nvidia-smi while VM is $STATE!"
            ntfy "RTX 5070 visible to Vast while VM is $STATE!"
        fi
    else
        # VM is not running
        if [ "$STATE" != "$LAST_STATE" ]; then
            log "VM state changed to: ${STATE:-unknown}"
            ntfy "Ghost-VM is ${STATE:-unknown}! Hiding RTX 5070 and attempting recovery..."
        fi
        attempt_recovery
    fi
    
    LAST_STATE="$STATE"
    sleep 30
done
