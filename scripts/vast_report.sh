#!/bin/bash

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
ORANGE='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

NTFY_TOPIC="vastai-notifs"
LAST_RELIABILITY_ALERT=""  # "low" or "recovered"
LAST_TEMP_ALERT=0
LAST_MULTI_CONTAINER_ALERT=0

notify() {
    curl -s -o /dev/null -X POST \
        -H "Title: $1" \
        -H "Priority: $2" \
        -H "Tags: $3" \
        -d "$4" \
        https://ntfy.sh/$NTFY_TOPIC &
}

while true; do
    clear
    echo "=========================================="
    echo " VAST.AI GPU MONITOR - $(TZ='Pacific/Auckland' date) - ~/vast_report.sh"
    echo "=========================================="
    echo ""

    MACHINE_OUTPUT=$(vastai show machines 2>/dev/null)
    if echo "$MACHINE_OUTPUT" | grep -qi "verified"; then
        echo -e "${GREEN}--- MACHINE STATUS ---${NC}"
    else
        echo -e "${YELLOW}--- MACHINE STATUS ---${NC}"
    fi
    echo "$MACHINE_OUTPUT"

    # Extract reliability score
    RELIABILITY=$(echo "$MACHINE_OUTPUT" | grep -oP '\b0\.\d+\b' | head -1)
    if [ -n "$RELIABILITY" ]; then
        RELIABILITY_PCT=$(echo "$RELIABILITY * 100" | bc -l | xargs printf "%.2f")
        SCORE=$(echo "$RELIABILITY_PCT" | cut -d'.' -f1)
        if [ "$SCORE" -lt 90 ]; then
            COLOR=$RED
        elif [ "$SCORE" -lt 95 ]; then
            COLOR=$ORANGE
        elif [ "$SCORE" -lt 99 ]; then
            COLOR=$YELLOW
        else
            COLOR=$GREEN
        fi
        echo -e "${COLOR}Reliability: ${RELIABILITY_PCT}%${NC}"

        # Notify on reliability drop below 90%
        if [ "$SCORE" -lt 90 ] && [ "$LAST_RELIABILITY_ALERT" != "low" ]; then
            notify "⚠️ Reliability Low" "high" "warning" "Reliability dropped to ${RELIABILITY_PCT}% on blackwell-node-01"
            LAST_RELIABILITY_ALERT="low"
        fi

        # Notify on reliability recovery above 90%
        if [ "$SCORE" -ge 90 ] && [ "$LAST_RELIABILITY_ALERT" = "low" ]; then
            notify "✅ Reliability Recovered" "default" "white_check_mark" "Reliability recovered to ${RELIABILITY_PCT}%"
            LAST_RELIABILITY_ALERT="recovered"
        fi
    else
        echo -e "${YELLOW}Reliability: N/A${NC}"
    fi
    echo ""
    # echo ""

    DOCKER_OUTPUT=$(docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" 2>/dev/null)
    DOCKER_COUNT=$(docker ps -q 2>/dev/null | wc -l)
    if [ "$DOCKER_COUNT" -gt 0 ]; then
        echo -e "${GREEN}--- DOCKER CONTAINERS ---${NC}"
    else
        echo -e "${YELLOW}--- DOCKER CONTAINERS ---${NC}"
    fi
    echo "$DOCKER_OUTPUT"
    echo ""

    # --- VAST INSTANCES ---
    VAST_OUTPUT=$(vastai show instances 2>/dev/null)
    VAST_COUNT=$(echo "$VAST_OUTPUT" | grep -c "running\|exited\|loading" 2>/dev/null)
    if [ "$VAST_COUNT" -gt 0 ]; then
        echo -e "${GREEN}--- VAST INSTANCES ---${NC} (docker ps | grep CONTAINER_ID)"
    else
        echo -e "${YELLOW}--- VAST INSTANCES ---${NC} (docker ps | grep CONTAINER_ID)"
    fi
    echo "$VAST_OUTPUT"
    echo ""

    # Check for multiple non-test containers
    REAL_CONTAINERS=$(docker ps --format "{{.Names}} {{.Image}}" | grep -v "vastai/test" | grep -c "^C\." || true)
    NOW=$(date +%s)
    if [ "$REAL_CONTAINERS" -gt 1 ]; then
        if [ $((NOW - LAST_MULTI_CONTAINER_ALERT)) -gt 300 ]; then
            CONTAINER_LIST=$(docker ps --format "{{.Names}}" | grep -v "vastai/test" | tr '\n' ' ')
            notify "🐳 Multiple Containers Running" "high" "whale" "$REAL_CONTAINERS containers active: $CONTAINER_LIST"
            LAST_MULTI_CONTAINER_ALERT=$NOW
            echo ""
        fi
    fi

    # echo "--- GPU STATUS ---"
    GPU_OUTPUT=$(nvidia-smi)
    echo "$GPU_OUTPUT"
    echo ""

    # Check GPU temperature
    GPU_TEMP=$(echo "$GPU_OUTPUT" | grep -oP '\d+(?=C\s+P)' | head -1)
    if [ -n "$GPU_TEMP" ] && [ "$GPU_TEMP" -gt 85 ]; then
        if [ $((NOW - LAST_TEMP_ALERT)) -gt 300 ]; then
            notify "🌡️ GPU Temperature High" "urgent" "thermometer" "RTX 5090 temperature is ${GPU_TEMP}°C on blackwell-node-01"
            LAST_TEMP_ALERT=$NOW
            echo ""
        fi
    fi

    # --- GPU MINER (lolminer via Docker) ---
    LOLMINER_CONTAINER=$(docker ps --format "{{.Names}} {{.Image}}" | grep -Ei "lolminer|cfx|miner|faskjem" | awk '{print $1}' | head -1)
    if [ -n "$LOLMINER_CONTAINER" ]; then        echo -e "${GREEN}--- GPU MINER (lolminer / CFX Octopus) ---${NC} (touch/rm ~/disable-lolminer-5090)"
        LOLMINER_LOG=$(docker logs --tail 20 "$LOLMINER_CONTAINER" 2>/dev/null)
        HASHRATE=$(echo "$LOLMINER_LOG" | grep -oP 'Average speed \(15s\): \K[0-9.]+ \w+/s' | tail -1)
        LAST_SHARE=$(echo "$LOLMINER_LOG" | grep "Share accepted" | tail -1)
        STALE_COUNT=$(echo "$LOLMINER_LOG" | grep -c "Share is stale" || true)
        if [ -n "$HASHRATE" ]; then
            echo -e "  Hashrate:     ${GREEN}${HASHRATE}${NC}"
        else
            echo -e "  Hashrate:     ${YELLOW}warming up...${NC}"
        fi
        if [ -n "$LAST_SHARE" ]; then
            echo "  Last share:   $LAST_SHARE"
        fi
        echo "  Container:    $LOLMINER_CONTAINER"
    else
        echo -e "${YELLOW}--- GPU MINER (lolminer) ---${NC} (touch/rm ~/disable-lolminer-5090)"
        echo "  Status: not running (customer job active or stopped)"
    fi
    echo ""
#    echo ""

    # --- CPU MINER (XMRig / QRL) ---
    XMRIG_STATUS=$(systemctl is-active xmrig-qrl 2>/dev/null)
    if [ "$XMRIG_STATUS" = "active" ]; then
        echo -e "${GREEN}--- CPU MINER (XMRig / QRL RandomX) ---${NC} (touch/rm ~/disable-xmrig)"
        XMRIG_LOG=$(journalctl -u xmrig-qrl -n 20 --no-pager 2>/dev/null)
        SPEED_LINE=$(echo "$XMRIG_LOG" | grep "speed" | tail -1)
        SPEED_VALS=$(echo "$SPEED_LINE" | grep -oP '[\d.]+ [\d.]+ [\d.]+(?= H/s)')
        ACCEPTED=$(echo "$XMRIG_LOG" | grep "accepted" | tail -1)
        HEIGHT=$(echo "$XMRIG_LOG" | grep "new job" | tail -1 | grep -oP 'height \K\d+')
        echo -e "  Status:       ${GREEN}active${NC}"
        if [ -n "$SPEED_VALS" ]; then
            echo "  Speed (10s/60s/15m): ${SPEED_VALS} H/s"
        fi
        if [ -n "$HEIGHT" ]; then
            echo "  Block height: $HEIGHT"
        fi
        if [ -n "$ACCEPTED" ]; then
            echo "  Last result:  $(echo "$ACCEPTED" | sed 's/.*xmrig\[.*\]: //')"
        fi
    else
        echo -e "${YELLOW}--- CPU MINER (XMRig / QRL) ---${NC} (touch/rm ~/disable-xmrig)"
        CONTROLLER_RUNNING=$(pgrep -f mining-controller-all.sh > /dev/null && echo "active" || echo "inactive")
        if [ "$CONTROLLER_RUNNING" = "active" ]; then
            echo -e "  Status: ${YELLOW}paused by controller (customer job active)${NC}"
        else
            echo -e "  Status: ${RED}stopped (controller also inactive)${NC}"
        fi
    fi
    echo ""
 #   echo ""

    # --- CONTROLLER STATUS ---
    CTRL_RUNNING=$(pgrep -f mining-controller-all.sh > /dev/null && echo "active" || echo "inactive")
    if [ "$CTRL_RUNNING" = "active" ]; then
        echo -e "${GREEN}--- MINING CONTROLLER ---${NC} (mining-controller-all.sh   tail -f ~/container-log.txt)"
    else
        echo -e "${RED}--- MINING CONTROLLER ---${NC} (mining-controller-all.sh   tail -f ~/container-log.txt)"
    fi
    echo "  Status: $CTRL_RUNNING"
    CTRL_LOG=$(tail -1 "$LOGFILE" 2>/dev/null)
    echo "  Last:   $CTRL_LOG"
    echo ""

    # --- KVM VM STATUS ---
    VM_STATE=$(sudo virsh domstate mining-ai-vm 2>/dev/null)
    if [ "$VM_STATE" = "running" ]; then
        echo -e "${GREEN}--- KVM VM (mining-ai-vm) ---${NC} (touch/rm ~/disable-kvm)"
    elif [ "$VM_STATE" = "paused" ]; then
        echo -e "${YELLOW}--- KVM VM (mining-ai-vm) ---${NC} (touch/rm ~/disable-kvm)"
    else
        echo -e "${RED}--- KVM VM (mining-ai-vm) ---${NC} (touch/rm ~/disable-kvm)"
    fi
    echo "  State: $VM_STATE"
    echo ""


    # --- SYSTEM RESOURCES ---
    TOTAL_CORES=$(nproc)
    TOTAL_PCT=$((TOTAL_CORES * 100))

    # CPU per process
    XMRIG_CPU=$(ps -p $(pgrep -f xmrig | head -1) -o %cpu= 2>/dev/null | xargs printf "%.1f" 2>/dev/null)
    KVM_CPU=$(ps -p $(pgrep -f qemu-system | head -1) -o %cpu= 2>/dev/null | xargs printf "%.1f" 2>/dev/null)
    KVM_CORES=$(sudo virsh dominfo mining-ai-vm 2>/dev/null | grep "CPU(s)" | awk '{print $2}')
    DOCKER_CPU=$(docker stats --no-stream --format "{{.Name}} {{.CPUPerc}}" 2>/dev/null | grep "^C\.")

    # Sum total CPU
    USED_CPU="0"
    [ -n "$XMRIG_CPU" ] && USED_CPU=$(echo "$USED_CPU + $XMRIG_CPU" | bc)
    [ -n "$KVM_CPU" ]   && USED_CPU=$(echo "$USED_CPU + $KVM_CPU" | bc)
    if [ -n "$DOCKER_CPU" ]; then
        while read -r name pct; do
            [ -z "$pct" ] && continue
            pct_clean=$(echo "$pct" | tr -d '%')
            USED_CPU=$(echo "$USED_CPU + $pct_clean" | bc)
        done <<< "$DOCKER_CPU"
    fi
    OVERALL_STD=$(printf "%.1f" $(echo "scale=4; $USED_CPU / $TOTAL_PCT * 100" | bc))

    # RAM bare metal
    BM_RAM_USED=$(free -h | awk '/^Mem:/ {print $3}')
    BM_RAM_FREE=$(free -h | awk '/^Mem:/ {print $7}')

    # RAM KVM VM
    VM_STATE_CHECK=$(sudo virsh domstate mining-ai-vm 2>/dev/null)
    if [ "$VM_STATE_CHECK" = "running" ]; then
        VM_RAM=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=3 rich-rob@192.168.122.142 \
            "free -h | awk '/^Mem:/ {print \$3, \$7}'" 2>/dev/null)
        VM_RAM_USED=$(echo "$VM_RAM" | awk '{print $1}')
        VM_RAM_FREE=$(echo "$VM_RAM" | awk '{print $2}')
    else
        VM_RAM_USED="--"
        VM_RAM_FREE="--"
    fi

    # KVM label
    KVM_LABEL="KVM (${KVM_CORES} alloc)"
    [ "$VM_STATE_CHECK" = "paused" ] && KVM_LABEL="KVM (${KVM_CORES} alloc,PAU)"

    printf "\n--- SYSTEM RESOURCES --- (%s cores)\n" "$TOTAL_CORES"
    printf "%-22s %8s %8s %10s %10s\n" "Host" "CPU%" "Cores" "RAM Used" "RAM Free"
    printf "%-22s %8s %8s %10s %10s\n" "----------------------" "--------" "--------" "----------" "----------"

    # AI PC row
    printf "%-22s %8s %8s %10s %10s\n" \
        "AI PC (bare metal)" "${OVERALL_STD}%" "~$(printf "%.1f" $(echo "scale=4; $USED_CPU / 100" | bc))" \
        "$BM_RAM_USED" "$BM_RAM_FREE"

    # KVM row
    if [ -n "$KVM_CPU" ]; then
        KVM_STD=$(printf "%.1f" $(echo "scale=4; $KVM_CPU / $TOTAL_PCT * 100" | bc))
        KVM_CORES_USED=$(printf "%.1f" $(echo "scale=4; $KVM_CPU / 100" | bc))
        printf "%-22s %8s %8s %10s %10s\n" \
            "$KVM_LABEL" "${KVM_STD}%" "~${KVM_CORES_USED}" "$VM_RAM_USED" "$VM_RAM_FREE"
    else
        printf "%-22s %8s %8s %10s %10s\n" "$KVM_LABEL" "stopped" "" "$VM_RAM_USED" "$VM_RAM_FREE"
    fi

    printf "\n  Processes:\n"
    printf "  %-20s %8s %8s\n" "Process" "CPU%" "Cores"
    printf "  %-20s %8s %8s\n" "--------------------" "--------" "--------"

    if [ -n "$XMRIG_CPU" ]; then
        XMRIG_STD=$(printf "%.1f" $(echo "scale=4; $XMRIG_CPU / $TOTAL_PCT * 100" | bc))
        XMRIG_CORES=$(printf "%.1f" $(echo "scale=4; $XMRIG_CPU / 100" | bc))
        printf "  %-20s %8s %8s\n" "XMRig" "${XMRIG_STD}%" "~${XMRIG_CORES}"
    else
        printf "  %-20s %8s %8s\n" "XMRig" "stopped" ""
    fi

    if [ -n "$DOCKER_CPU" ]; then
        echo "$DOCKER_CPU" | while read -r name pct; do
            [ -z "$pct" ] && continue
            pct_clean=$(echo "$pct" | tr -d '%')
            STD=$(printf "%.1f" $(echo "scale=4; $pct_clean / $TOTAL_PCT * 100" | bc))
            CORES=$(printf "%.1f" $(echo "scale=4; $pct_clean / 100" | bc))
            printf "  %-20s %8s %8s\n" "$name" "${STD}%" "~${CORES}"
        done
    fi

    # Top containers RAM on bare metal
    BM_DOCKER_RAM=$(docker stats --no-stream --format "{{.Name}} {{.MemUsage}}" 2>/dev/null | grep "^C\." | head -3)
    if [ -n "$BM_DOCKER_RAM" ]; then
        printf "\n  Container RAM (bare metal):\n"
        echo "$BM_DOCKER_RAM" | while read -r line; do
            printf "  %s\n" "$line"
        done
    fi
    echo ""
    sleep 20
done
