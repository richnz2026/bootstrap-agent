#!/bin/bash
# Pearl mining via AlphaPool (Docker) on Ghost-VM
set -euo pipefail
source /root/.mining_keys
nvidia-smi -pl 180
docker run --rm --init --gpus all \
  --name pearl-miner \
  -e PEARL_ADDRESS="${PRL_WALLET}" \
  -e PEARL_WORKER=ghost-vm \
  -e PEARL_DIFFICULTY=65536 \
  -e PEARL_POOL_HOST=sg1.alphapool.tech \
  alphaminetech/pearl-miner
