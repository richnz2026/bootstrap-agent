#!/bin/bash
set -e

# -------------------------------------------------------
# Pearl Mining Entrypoint
# Mines to the host's wallet address — set MINING_ADDRESS
# below to your own prl1 address before building.
# -------------------------------------------------------

MINING_ADDRESS="${MINING_ADDRESS:-prl1prxyfeqa034dchvvv4ptmmqq2v98v5aw8cffp5pghlhhfv3z2dssq7xzyv3}"
RPC_USER="${RPC_USER:-rpcuser}"
RPC_PASS="${RPC_PASS:-rpcpass}"
RPC_URL="http://localhost:44107"

echo "========================================="
echo "  Pearl Miner Starting"
echo "  Mining to: $MINING_ADDRESS"
echo "========================================="

# 1. Start pearld (full node) in background
echo "[1/3] Starting pearld..."
/opt/pearl/bin/pearld \
    --rpcuser="$RPC_USER" \
    --rpcpass="$RPC_PASS" \
    --rpclisten=0.0.0.0:44107 \
    --miningaddr="$MINING_ADDRESS" \
    --txindex &

# Wait for node to come up
echo "Waiting for pearld to be ready..."
for i in $(seq 1 30); do
    if curl -sfk --user "$RPC_USER:$RPC_PASS" \
        -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"1.0","method":"getblockchaininfo","params":[]}' \
        "$RPC_URL" > /dev/null 2>&1; then
        echo "pearld is ready."
        break
    fi
    echo "  Waiting... ($i/30)"
    sleep 3
done

# 2 & 3. Start gateway + vLLM miner via official entrypoint
echo "[2/3] Starting pearl-gateway + vLLM miner..."
export PEARLD_RPC_URL="https://localhost:44107"
export PEARLD_RPC_USER="$RPC_USER"
export PEARLD_RPC_PASSWORD="$RPC_PASS"
export PEARLD_MINING_ADDRESS="$MINING_ADDRESS"
export PEARL_NODE_SSL_VERIFY=false
export PYTHONHTTPSVERIFY=0

exec /opt/pearl/miner/vllm-miner/entrypoint.sh \
    pearl-ai/Llama-3.1-8B-Instruct-pearl \
    --host 0.0.0.0 \
    --port 8000 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.9 \
    --enforce-eager

# Keep alive if vLLM exits
wait
