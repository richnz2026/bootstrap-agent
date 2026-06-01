# Ghost-VM Recovery & Rebuild — Session Log

**Date:** 2026-06-01
**Host:** blackwell-node-01 (Ubuntu 24.04, 192.168.50.51)
**VM:** mining-ai-vm (static 192.168.122.143)

## Starting Point
Ghost-VM wouldn't boot — dropped to GRUB. Prior diagnosis: root filesystem
destroyed (contradictory partition tables, ext4 superblock + all backups
gone), caused by a hard virsh reset/destroy during an active disk write.
NVMe healthy (SMART). Keys safe on blackwell, code safe in GitHub, but the
VM's orchestration config was lost with the filesystem.

## Environment
- RTX 5090 (Vast machine 55898, paying customer) — host driver 580.142,
  must stay visible to Vast
- RTX 5070 — VFIO passthrough to VM, PCI 03:00.0/03:00.1, must NEVER be
  exposed to host nvidia/Vast
- Gaming PC 192.168.50.172 (dual-boot)
- Repo: github.com/richnz2026/bootstrap-agent (branch: bootstrap)

## Phase 1 — Reconstruct & Commit Lost Config
Reconstructed orchestration from recovered logs + notes: docker-compose.yml,
prometheus.yml, 8-phase REBUILD.md. Committed and pushed.

## Phase 2 — Build Fresh VM
- New 500G qcow2 (ghost-vm-new.qcow2); virt-install Ubuntu 24.04, no GPU yet
- Installed via VNC over SSH tunnel: single ext4 root on /dev/vda, hostname
  ghost-vm, OpenSSH on, no snaps
- GPU verified 5090-only at every step

## Phase 3 — Network & Clean Shutdown
- qemu-guest-agent installed (fix for what killed the original — enables
  virsh shutdown --mode agent)
- Static .143 pinned via libvirt DHCP reservation on the VM MAC
- guest-ping verified; clean shutdown/restart landed on .143

## Phase 4 — RTX 5070 Passthrough
- Two <hostdev managed='yes'> entries (video + audio) added to VM XML
- Started VM; host stayed 5090-only, guest saw 5070 (GB205 [10de:2f04])
- Safety invariant held through every shutdown/start cycle

## Phase 5 — Driver, Container Toolkit, Ollama
- nvidia-driver-580-server-open (open module for Blackwell; 580 over 595
  due to known miner-compat issues)
- Manual modprobe first boot, made persistent via
  /etc/modules-load.d/nvidia.conf; auto-loads on reboot
- Docker + nvidia-container-toolkit + CDI; CUDA container sees 5070
- ollama 0.24.0 on 0.0.0.0:11434, model qwen2.5:14b, GPU inference verified
  (~9.5GB VRAM)

## Phase 6 — The 9-Service Stack
- OpenHands registry dead (docker.all-hands.dev NXDOMAIN) -> updated to
  docker.openhands.dev/openhands/openhands:1.7 (1.x line)
- Dashboard had no Dockerfile (just index.html) -> created nginx Dockerfile,
  port 8001:80
All 9 running: openhands 3000, human-control-api 8000,
human-control-dashboard 8001, redis 6379, qdrant 6333-6334, grafana 3001,
prometheus 9090, ntfy 9093, searxng 8080, + ollama 11434.

## Cleanup & Renaming
- Root SSH by key on the VM so dashboard.py's root@192.168.122.143 scripts
  work unchanged (matched VM to tooling)
- Renamed ghost-vm-new -> mining-ai-vm (undefined dead old VM first) so
  dashboard virsh calls match
- virsh autostart enabled (also aids GPU safety — VM grabs 5070 fast)
- vfio-watchdog.service confirmed active (rebinds 5070 to vfio if it appears
  on host; persistent binding never worked on this Blackwell hardware, so
  the watchdog is the solution)
- Deleted 3 corrupt qcow2s (~750G reclaimed) after confirming live VM runs
  on ghost-vm-new.qcow2

## Miners — Both Restored, Dashboard-Controllable
- .mining_keys copied to VM /root/ (chmod 600); wallets sourced by variable
- ERG (lolminer): lolMiner 1.97, /root/mine_erg.sh (AUTOLYKOS2, pool
  46.4.102.169:1180, $GAMING_ERG_WALLET, worker ghost-vm), lolminer.service.
  ~101 MH/s, shares accepted, dashboard start/stop verified.
- PRL (pearl): replicated Gaming PC config — alphaminetech/pearl-miner
  Docker, /root/mine_pearl.sh (pool sg1.alphapool.tech:5566, $PRL_WALLET,
  worker ghost-vm, 180W), pearl.service wrapping container pearl-miner.
  Submitting shares to alphapool, dashboard start/stop verified.
- Both enabled but not auto-started (dashboard toggles; avoids 5070 VRAM
  contention with ollama). SuccessExitStatus=143 on lolminer; sudo removed
  from VM mine_pearl.sh.

## Bugs Fixed
- container_logger.sh returning-customer detection: keyed off .container_seen
  (ever-seen) so returning customers never re-logged. Changed to key off
  .container_active. Fixed the missing dashboard tab AND the
  Demand-vs-Interruptible mislabel (both stale-data symptoms).
- Wrapped container_logger in a systemd service (was loose nohup); removed
  the redundant @reboot cron line that would spawn a duplicate logger.

## Notification Investigation
- "Customer Connected/Job Ended" vs "New Rental/Rental Ended" = historical
  ntfy messages from retired watch_vast_extended.sh (not running, not in
  cron). Not a live duplicate.
- watchdog.sh (CPU/RAM/proc alerts + mining pauser) and temp_logger.sh (pure
  GPU temp logger) confirmed working, left as-is.
- Two lolminers confirmed legitimately separate (5090 CFX host, 5070 ERG VM)
  with distinct disable-flags.
- Deduped double ANTHROPIC_API_KEY in .mining_keys (identical values), synced
  to both machines.

## Full Control Plane Backed Up to Git
Scanned for hardcoded secrets (clean — code sources from .mining_keys), then
committed:
- dashboard.py (~5500 lines), watchdog.sh, temp_logger.sh
- gpu-safety/: vfio-watchdog.sh, vfio-rebind-5070.sh
- systemd/: dashboard, vfio-watchdog, mining-controller, cpu-mining-manager,
  xmrig-controller, xmrig-qrl, vast_metrics, nvidia-cdi-refresh
- cron-blackwell.txt
- (Earlier: compose, prometheus.yml, REBUILD.md, miner scripts/services,
  MINER_SETUP.md, container_logger + service)
Full machine loss now recoverable from repo + .mining_keys (off git by
design). The gap that started this saga is closed.

## Parked / Outstanding
- #7 — point OpenHands at ollama in Settings UI (base URL
  http://host.docker.internal:11434, model ollama/qwen2.5:14b, dummy key);
  needs UI-access path sorted (.122 subnet routing/tunnel)
- Verify the 93C thermal-power guard actually exists — temp_logger only
  logs, does not reduce power; confirm this protection is implemented
  somewhere (guards the customer's 5090)
- Classify/back up watch_vast.sh, vast.py, send_mach_info.py, image_routes.py
  if any are live
- Delete watch_vast_extended.sh.bak leftover
- ~25 patch_*.py are one-off dev patches — skip or archive separately

## Process Lessons
1. Never hard-destroy a running VM — use virsh shutdown --mode agent
2. Anything non-secret you'd be sad to lose goes in git; secrets stay in
   .mining_keys (known location, off git)
3. Static IP + guest agent + localhost-bound VNC + autostart
4. Match the VM to existing tooling (names, users) rather than editing many
   script references
