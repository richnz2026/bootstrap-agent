# Session Log — 2026-06-17

## Kernel Panic / RCU Stall Fix
- Symptom: RCU preempt stalls on CPUs 12-23, 2x panics (01:58, 06:59)
- Root cause: kaalia crash-loop (vastai stop/start cycling every ~2min due to
  stale kaalia.log) while VFIO/KVM active on CPUs 12-23 → RCU stall → kernel panic
- Fix: removed broken vendor-reset DKMS (0.1.1), upgraded kernel 6.17.0-19 → 6.17.0-35
- Safe reboot: virsh shutdown --mode agent first, VM came back clean
- Old kernels removed (637MB freed)

## Ghost VM Image Vault
- Added ghost VM support to image_routes.py and image_dashboard.html
- Scan panel dropdown: Docker / Ghost mode
- Ghost scans via SSH (id_ed25519_vm), imports via SCP into samples/ghost/
- Per-file .ghost.json sidecar tracks original path for deletion
- 🗑 GHOST button on vault cards: deletes local + SSH rm on ghost VM
- Folder-level delete in scan panel and sidebar with local/ghost options
- SD Forge outputs at /home/rich-rob/stable-diffusion-webui-forge/outputs confirmed working
- 155+ files available; import tested successfully

## Forge Link Fix
- Open UI button was linking to http://192.168.50.51:8080/#
- Fixed to http://localhost:7860 (tunnelled via SSH config LocalForward)

## Kaalia False Alerts Fix
- Gaming PC blackwell-watchdog.service was running old blackwell-watchdog.sh
- Old script used pgrep PID churn detection → false "Kaalia Unhealthy" alerts every 30min
- Switched service to blackwell-watchdog_2.sh which uses systemd NRestarts instead
- No more false alerts on normal vastai PID cycling

## Outstanding
- 166 packages upgradable (defer — don't upgrade nvidia/cuda without testing)
- vendor-reset DKMS removed — monitor if GPU reset between containers is affected
- Music player service on ghost VM — deferred to next session

## ACE-Step Music Service
- Port changed from 7860 to 7861 (avoids conflict with Forge)
- Added to dashboard: 🎵 ACE-STEP MUSIC section mirroring Forge layout
- Start/stop via systemctl on ghost VM, exclusive GPU — stops lolminer/pearl/forge first
- Fix: libavutil.so.56 missing (system has .58) — created symlink + ldconfig
  `ln -s /usr/lib/x86_64-linux-gnu/libavutil.so.58 /usr/lib/x86_64-linux-gnu/libavutil.so.56`
- UI accessible at localhost:7861 via SSH tunnel (add LocalForward 7861 to Mac ~/.ssh/config)
- VRAM at idle: ~2.6GB (model loaded), leaves ~9.2GB free for generation

## XFS Corruption Incident (11:00 UTC)
- Cause: docker build of pman-worker (7.29GB context) on ghost VM caused SIGBUS
  crash, which corrupted the XFS filesystem on nvme0n1p1 (/var/lib/docker)
- Symptoms: Docker dead, ghost VM unreachable, /var/lib/docker I/O error
- Fix: xfs_repair -L /dev/nvme0n1p1 (log destroyed, repair completed)
- /var/lib/docker was missing from /etc/fstab — added with nofail flag
- All services restored: docker, ghost VM, kaalia, xmrig, dashboard

## Prevention
- Never run large docker builds (>1GB context) on the ghost VM
- Build pman-worker image on blackwell host or gaming PC instead
- The ghost VM qcow2 is on the same XFS volume — a crash during heavy I/O
  can corrupt the filesystem and take down the whole Docker stack

## pquota / storage-opt fix
- After XFS repair, /var/lib/docker remounted without pquota
- Docker --storage-opt failed, blocking customer container creation
- Fixed: added pquota to fstab entry, rebooted to activate
- Mount now shows: xfs (rw,relatime,...,prjquota)

## pman-worker Docker image
- Built on blackwell (not ghost VM) to avoid XFS corruption risk
- Excluded pearl-gemm (197MB CUDA source, not needed at runtime)
- Pushed to itsthateasymate/pman-worker:latest
- Build context: ~/pman-worker-build/ on blackwell
- Future builds: always build on blackwell, never on ghost VM

## pmate-worker Docker image
- pman-worker build failed due to XFS layer corruption in Docker
- Workaround: docker tag alphaminetech/pearl-miner:latest itsthateasymate/pmate-worker:latest
- Pushed as itsthateasymate/pmate-worker:latest
- Vast template updated to pmate-worker
- PRL mining confirmed working on RTX 5090, GPU 100%, ~90°C
- Vast.ai storage-opt error cleared after pquota reboot
