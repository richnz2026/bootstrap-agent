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
