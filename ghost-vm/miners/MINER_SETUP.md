# Ghost-VM Miner Setup

Both miners run on the VM's RTX 5070, controlled by the blackwell dashboard
over root SSH (systemctl start/stop). Wallets are sourced from
/root/.mining_keys (never hardcoded). Services are `enabled` but NOT
auto-started on boot — the dashboard toggles them, so they don't fight
ollama for the 5070's 12GB VRAM.

Deploy on the VM:  ssh -i ~/.ssh/id_ed25519_vm root@192.168.122.143

## ERG — lolMiner (AutolykosV2), service: lolminer

Install lolMiner (1.97 supports RTX 5000 / Blackwell):
    mkdir -p /opt/lolminer && cd /tmp
    wget https://github.com/Lolliedieb/lolMiner-releases/releases/download/1.97/lolMiner_v1.97_Lin64.tar.gz
    tar -xzf lolMiner_v1.97_Lin64.tar.gz
    cp 1.97/lolMiner /opt/lolminer/lolMiner && chmod +x /opt/lolminer/lolMiner

Files (in this repo, deploy to the VM):
    /root/mine_erg.sh                     <- mine_erg.sh
    /etc/systemd/system/lolminer.service  <- lolminer.service

Enable (not start):
    systemctl daemon-reload && systemctl enable lolminer

Control / verify:
    systemctl start lolminer      # ~101 MH/s, ~6.8GB VRAM
    journalctl -u lolminer -f
    systemctl stop lolminer

Pool 46.4.102.169:1180, wallet $GAMING_ERG_WALLET, worker ghost-vm.

## PRL — pearl-miner (Docker), service: pearl

Image alphaminetech/pearl-miner (pulled on first run). The service wraps a
Docker container named pearl-miner (the dashboard checks `docker logs
pearl-miner`).

Files (in this repo, deploy to the VM):
    /root/mine_pearl.sh                <- mine_pearl.sh
    /etc/systemd/system/pearl.service  <- pearl.service

Enable (not start):
    systemctl daemon-reload && systemctl enable pearl

Control / verify (allow ~30s to come up):
    systemctl start pearl
    docker logs pearl-miner --tail 10   # pool connect + shares submitted
    systemctl stop pearl                # --rm + ExecStop removes container

Pool sg1.alphapool.tech:5566, wallet $PRL_WALLET, worker ghost-vm,
difficulty 65536, power limit 180W. ~2.8GB VRAM.

## GPU contention (5070 = 12GB)

ollama qwen2.5:14b uses ~9.5GB. A GPU miner + the 14B model will not
coexist. Mine OR run agents (dashboard toggles), not both. ollama unloads
the model after idle (default 5m), freeing VRAM.

## Dashboard control

The dashboard (blackwell) drives both via root SSH:
    ssh root@192.168.122.143 'systemctl {start,stop,is-active} {lolminer,pearl}'
Service names match the dashboard's checks. Root login by key is set up on
the VM (/root/.ssh/authorized_keys).
