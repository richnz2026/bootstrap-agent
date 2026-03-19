#!/bin/bash

# ============================================================
# mining-controller-all.sh
# Manages Vast.ai customer detection, XMRig, KVM VM, and
# lolminer on the RTX 5090 (direct Docker, no Vast.ai defjob)
#
# MANUAL OVERRIDES (controller keeps running, respects these):
#   touch ~/disable-lolminer-5090   # stop 5090 lolminer, don't restart
#   touch ~/disable-xmrig           # stop XMRig, don't restart
#   touch ~/disable-kvm             # suspend KVM/5070, don't resume
#
#   rm ~/disable-lolminer-5090      # hand back to controller
#   rm ~/disable-xmrig              # hand back to controller
#   rm ~/disable-kvm                # hand back to controller
#
# KILL ALL (stops controller + all miners):
#   pkill -f mining-controller-all.sh; docker stop lolminer-5090; docker rm lolminer-5090; sudo systemctl stop xmrig-qrl; sudo virsh suspend mining-ai-vm
#
# START ALL (resumes everything + restarts controller):
#   sudo virsh resume mining-ai-vm; sudo systemctl start xmrig-qrl; docker rm -f lolminer-5090 2>/dev/null; docker run -d --gpus all --name lolminer-5090 --restart no faskjem/miner bash -c 'cd lolMiner; ./IJ_Miner --algo OCTOPUS --pool cfx.f2pool.com:6800 --user richrichrich26.5090idle --pass x --watchdog off'; ~/mining-controller-all.sh >> ~/container-log.txt 2>&1 &
# ============================================================

LOLMINER_NAME="lolminer-5090"
LOLMINER_IMAGE="faskjem/miner"

LOGFILE="/home/rich-rob/container-log.txt"
NTFY_TOPIC="vastai-notifs"

DISABLE_LOLMINER="/home/rich-rob/disable-lolminer-5090"
DISABLE_XMRIG="/home/rich-rob/disable-xmrig"
DISABLE_KVM="/home/rich-rob/disable-kvm"

KVM_CPU_SUSPENDED=false    # did WE suspend it due to CPU pressure?
CPU_HIGH_COUNT=0           # sustained high CPU counter
CPU_LOW_COUNT=0            # sustained low CPU counter

# --- Initialise MINING_ACTIVE based on actual XMRig state ---
if systemctl is-active --quiet xmrig-qrl; then
    MINING_ACTIVE=true
else
    MINING_ACTIVE=false
fi

BENCH_PAUSED=false

# ============================================================
notify() {
    curl -s -o /dev/null -X POST \
        -H "Title: $1" \
        -H "Priority: $2" \
        -H "Tags: $3" \
        -d "$4" \
        https://ntfy.sh/$NTFY_TOPIC &
}

lolminer_running() {
    docker ps -q -f name="^${LOLMINER_NAME}$" 2>/dev/null | grep -q .
}

lolminer_paused() {
    docker inspect --format '{{.State.Paused}}' "$LOLMINER_NAME" 2>/dev/null | grep -q true
}

start_lolminer() {
    docker rm -f "$LOLMINER_NAME" 2>/dev/null || true
    docker run -d --gpus all \
        --name "$LOLMINER_NAME" \
        --restart no \
        "$LOLMINER_IMAGE" \
        bash -c 'cd lolMiner; ./IJ_Miner --algo OCTOPUS --pool cfx.f2pool.com:6800 --user richrichrich26.5090idle --pass x --watchdog off'
    echo "$(date) - lolminer-5090 started" | tee -a "$LOGFILE"
}

stop_lolminer() {
    if docker ps -aq -f name="^${LOLMINER_NAME}$" 2>/dev/null | grep -q .; then
        docker stop "$LOLMINER_NAME" 2>/dev/null || true
        docker rm "$LOLMINER_NAME" 2>/dev/null || true
        echo "$(date) - lolminer-5090 stopped" | tee -a "$LOGFILE"
    fi
}

pause_lolminer() {
    if lolminer_running && ! lolminer_paused; then
        docker pause "$LOLMINER_NAME" 2>/dev/null
        echo "$(date) - lolminer-5090 paused (benchmark)" | tee -a "$LOGFILE"
    fi
}

resume_lolminer() {
    if lolminer_paused; then
        docker unpause "$LOLMINER_NAME" 2>/dev/null
        echo "$(date) - lolminer-5090 resumed (benchmark done)" | tee -a "$LOGFILE"
    fi
}

get_cpu_idle() {
    # Returns integer CPU idle percentage
    top -bn1 | grep "Cpu(s)" | awk '{for(i=1;i<=NF;i++) if($i=="id,") print int($(i-1))}'
}


# ============================================================
while true; do

    	# --- Detect container types ---
    	BENCH_RUNNING=$(docker ps --format "{{.Image}}" 2>/dev/null | grep -c "vastai/test" || true)
	VAST_CUSTOMER=$(
    	for cname in $(docker ps --format "{{.Names}}" 2>/dev/null | grep "^C\."); do
        	args=$(docker inspect --format '{{json .Args}}' "$cname" 2>/dev/null)
        	if ! echo "$args" | grep -q "richrichrich26.5090idle"; then
            		echo "$cname"
        	fi
    		done | wc -l
	)
    # --- Log containers (skip our own lolminer to reduce noise) ---
    DOCKER_COUNT=$(docker ps -q 2>/dev/null | wc -l)
    if [ "$DOCKER_COUNT" -gt 0 ]; then
        docker ps --format "$(date +%H:%M:%S) | {{.Names}} | {{.Image}} | {{.Status}}" \
            | grep -v "| ${LOLMINER_NAME} |" >> "$LOGFILE" || true
    fi

    # -------------------------------------------------------
    # CUSTOMER ACTIVE — stop everything regardless of lockfiles
    # -------------------------------------------------------
if [ "$VAST_CUSTOMER" -gt 0 ]; then
    if [ "$MINING_ACTIVE" = true ]; then
        CUSTOMER_NAME=$(docker ps --format "{{.Names}}" 2>/dev/null \
            | grep "^C\." | grep -v "vastai/test" | head -1)
        echo "$(date) - Customer $CUSTOMER_NAME detected. Stopping GPU/CPU mining." | tee -a "$LOGFILE"
        stop_lolminer
        sudo systemctl stop xmrig-qrl
        MINING_ACTIVE=false
        BENCH_PAUSED=false
        CPU_HIGH_COUNT=0
        CPU_LOW_COUNT=0
        notify "💰 Vast.ai Customer Arrived" "high" "money_with_wings" \
            "Customer $CUSTOMER_NAME active. GPU/CPU mining paused. KVM monitoring CPU."
    fi

    # --- CPU pressure check — only suspend VM if genuinely needed ---
    if [ -f "$DISABLE_KVM" ]; then
        # Manual lockfile takes priority — suspend unconditionally
        VM_STATE=$(sudo virsh domstate mining-ai-vm 2>/dev/null)
        if [ "$VM_STATE" = "running" ]; then
            sudo virsh suspend mining-ai-vm 2>/dev/null
            echo "$(date) - KVM suspended (lockfile)" | tee -a "$LOGFILE"
        fi
    else
        VM_STATE=$(sudo virsh domstate mining-ai-vm 2>/dev/null)
        CPU_IDLE=$(get_cpu_idle)

        if [ "$VM_STATE" = "running" ]; then
            if [ "$CPU_IDLE" -lt 15 ]; then
                CPU_HIGH_COUNT=$((CPU_HIGH_COUNT + 1))
                CPU_LOW_COUNT=0
                # Sustained high CPU for 30 cycles (~60s) → suspend
                if [ "$CPU_HIGH_COUNT" -ge 30 ]; then
                    sudo virsh suspend mining-ai-vm 2>/dev/null
                    KVM_CPU_SUSPENDED=true
                    CPU_HIGH_COUNT=0
                    echo "$(date) - KVM suspended (CPU pressure, idle=${CPU_IDLE}%)" | tee -a "$LOGFILE"
                    notify "⚠️ KVM Suspended" "default" "computer" \
                        "CPU pressure from customer. KVM suspended to free cores."
                fi
            else
                CPU_HIGH_COUNT=0
            fi
        fi

        if [ "$VM_STATE" = "paused" ] && [ "$KVM_CPU_SUSPENDED" = true ]; then
            if [ "$CPU_IDLE" -gt 30 ]; then
                CPU_LOW_COUNT=$((CPU_LOW_COUNT + 1))
                # Sustained low CPU for 30 cycles (~60s) → resume
                if [ "$CPU_LOW_COUNT" -ge 30 ]; then
                    sudo virsh resume mining-ai-vm 2>/dev/null
                    KVM_CPU_SUSPENDED=false
                    CPU_LOW_COUNT=0
                    echo "$(date) - KVM resumed (CPU pressure eased, idle=${CPU_IDLE}%)" | tee -a "$LOGFILE"
                    notify "✅ KVM Resumed" "default" "computer" \
                        "CPU pressure eased. KVM back online mid-customer."
                fi
            else
                CPU_LOW_COUNT=0
            fi
        fi
    fi
    # -------------------------------------------------------
    # NO CUSTOMER — manage each miner individually
    # -------------------------------------------------------
    else

        # --- Mark as active if transitioning from customer ---
        if [ "$MINING_ACTIVE" = false ]; then
            echo "$(date) - No customer. Resuming mining." | tee -a "$LOGFILE"
            MINING_ACTIVE=true
            BENCH_PAUSED=false
            notify "👋 Vast.ai Customer Left" "default" "wave" "Mining resumed."
        fi

        # --- KVM / 5070 ---
        if [ -f "$DISABLE_KVM" ]; then
            VM_STATE=$(sudo virsh domstate mining-ai-vm 2>/dev/null)
            if [ "$VM_STATE" = "running" ]; then
                sudo virsh suspend mining-ai-vm 2>/dev/null
                echo "$(date) - KVM suspended (lockfile)" | tee -a "$LOGFILE"
            fi
	else
            VM_STATE=$(sudo virsh domstate mining-ai-vm 2>/dev/null)
            if [ "$VM_STATE" = "paused" ]; then
                sudo virsh resume mining-ai-vm 2>/dev/null
                KVM_CPU_SUSPENDED=false
                CPU_HIGH_COUNT=0
                CPU_LOW_COUNT=0
                echo "$(date) - KVM resumed" | tee -a "$LOGFILE"
            fi
        fi

        # --- XMRig / CPU ---
        if [ -f "$DISABLE_XMRIG" ]; then
            if systemctl is-active --quiet xmrig-qrl; then
                sudo systemctl stop xmrig-qrl
                echo "$(date) - XMRig stopped (lockfile)" | tee -a "$LOGFILE"
            fi
        else
            if ! systemctl is-active --quiet xmrig-qrl; then
                sudo systemctl start xmrig-qrl
                echo "$(date) - XMRig started" | tee -a "$LOGFILE"
            fi
        fi

        # --- lolminer / 5090 ---
        if [ -f "$DISABLE_LOLMINER" ]; then
            stop_lolminer
        else
            if [ "$BENCH_RUNNING" -gt 0 ]; then
                if [ "$BENCH_PAUSED" = false ]; then
                    pause_lolminer
                    BENCH_PAUSED=true
                fi
            else
                if [ "$BENCH_PAUSED" = true ]; then
                    resume_lolminer
                    BENCH_PAUSED=false
                fi
                if ! lolminer_running && ! lolminer_paused; then
                    echo "$(date) - lolminer-5090 not running, restarting..." | tee -a "$LOGFILE"
                    start_lolminer
                fi
            fi
        fi

    fi

    sleep 2
done
