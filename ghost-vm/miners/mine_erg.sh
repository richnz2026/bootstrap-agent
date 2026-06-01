#!/bin/bash
set -euo pipefail
source /root/.mining_keys
POOL="46.4.102.169:1180"
WALLET="${GAMING_ERG_WALLET}"
WORKER="ghost-vm"
exec /opt/lolminer/lolMiner --algo AUTOLYKOS2 --pool "$POOL" --user "${WALLET}.${WORKER}" --pass x --apiport 4068
