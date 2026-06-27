# Session Notes — Kernel Panic Hardening
*blackwell / ghost-vm VFIO stability*
*2026-06-27 to 2026-06-28*

---

## Problem

Ghost VM (KVM guest on blackwell) causing kernel panics on the host. Root causes
identified as VFIO-related: GPU reset failure on VM crash, and a stale VM name
in the vfio-watchdog that meant the GPU protection was silently doing nothing.

---

## Root Cause Analysis

**Primary trigger (most likely):** `vfio-watchdog.sh` was checking for VM name
`mining-ai-vm` (old name). Ghost VM is now `ghost-vm-new`. Watchdog never found
the VM running, so it was not protecting the GPU — potentially allowing GPU
rebind operations at the wrong time.

**Secondary trigger:** No GPU reset mechanism. When ghost VM crashes hard
(`on_crash destroy`), the RTX 5070 (GB205) gets no clean reset signal. The
kernel sees a PCIe device in an unknown state. If anything triggers a bus rescan
or driver probe in that window, the nvidia driver can race vfio-pci to claim the
device. nvidia wins, tries to initialise an unreset GPU, gets garbage back →
kernel panic.

**vendor-reset status:** The `gnif/vendor-reset` DKMS module does NOT support
RTX 50-series (GB205/GB202) as of 2026-06-27. Confirmed by source inspection —
only false positives from AMD register definition files. No 50-series entries
exist. Check https://github.com/gnif/vendor-reset/issues for future support.

---

## What Was Done

### 1. Fix vfio-watchdog VM name
- **File:** `/home/rich-rob/vfio-watchdog.sh` (service copy)
- **File:** `/home/rich-rob/bootstrap-agent/gpu-safety/vfio-watchdog.sh` (repo copy)
- **Change:** `DOMAIN="mining-ai-vm"` → `DOMAIN="ghost-vm-new"`
- **Method:** `sed -i.bak 's/mining-ai-vm/ghost-vm-new/g'`
- **Service:** `vfio-watchdog.service` restarted, confirmed running with
  `virsh domstate ghost-vm-new` visible in process tree
- **Note:** Both copies must be kept in sync. Repo copy is the source of truth
  for future rebuilds.

### 2. kdump + pstore
- **Packages installed:** `kdump-tools`, `crash`, `linux-crashdump`,
  `kexec-tools`, `makedumpfile`
- **Install note:** apt hung for 20+ min on first run due to interactive debconf
  prompt (ncurses broken over SSH). Fixed with:
  `sudo fuser -k /var/cache/debconf/config.dat && sudo DEBIAN_FRONTEND=noninteractive dpkg --configure -a`
- **kdump config:** `/etc/default/kdump-tools` — `USE_KDUMP=1`
- **GRUB additions** to `GRUB_CMDLINE_LINUX`:
  - `crashkernel=512M,high` — reserves memory for crash kernel
  - `pstore.backend=efi` — writes panic reason to EFI NVRAM (survives reboot)
- **Service:** `kdump-tools.service` enabled
- **Post-reboot verification:** `cat /sys/kernel/kexec_crash_loaded` → `1` ✅
- **Crash dumps land in:** `/var/crash/`
- **pstore records land in:** `/sys/fs/pstore/` (empty = no panics recorded)
- **Analyse a dump with:**
  `crash /usr/lib/debug/boot/vmlinux-$(uname -r) /var/crash/<timestamp>/vmcore`

### 3. vendor-reset fallback — three-layer GPU binding defence

vendor-reset not available for GB205. Implemented equivalent protection via:

**Layer 1 — udev rule (earliest possible intervention)**
- **File:** `/etc/udev/rules.d/99-vfio-5070.rules`
- Fires at device-probe time, before any driver binding
- Sets `driver_override=vfio-pci` on both GB205 PCI IDs the moment the kernel
  sees them on the bus — including after a VM crash triggers a PCIe bus rescan
- IDs: `10de:2f04` (GPU), `10de:2f80` (audio)
- `udevadm control --reload-rules` + `update-initramfs -u -k all` run after

**Layer 2 — modprobe.d**
- **File:** `/etc/modprobe.d/vfio.conf`
- `options vfio-pci ids=10de:2f04,10de:2f80 disable_vga=1`
- Added `disable_vga=1` — prevents 5070 being used as VGA fallback by host
- Was already present with correct IDs; `disable_vga=1` added this session

**Layer 3 — vfio-watchdog settle delay**
- Added 10-second sleep at the top of `attempt_recovery()` before `hide_gpu()`
  is called
- Gives the GB205 time to self-reset before vfio-pci rebind is attempted
- Without vendor-reset, this is the only mechanism for GPU self-recovery
- Patched via Python (sed couldn't handle multiline match):
  ```python
  txt.replace('attempt_recovery() {\n    RECOVERY_ATTEMPTS',
              'attempt_recovery() {\n    log "Waiting 10s..."\n    sleep 10\n    RECOVERY_ATTEMPTS')
  ```

**GRUB addition:**
- `acpi_enforce_resources=lax` added to `GRUB_CMDLINE_LINUX`
- Helps some boards handle PCIe device reset more cleanly

**Vast.ai threat model clarified:**
- Vast runs as a Docker container — cannot directly touch PCI bindings
- The threat is nvidia driver racing vfio-pci, not Vast stealing the GPU
- modprobe.d + udev rule together make nvidia binding effectively impossible
  without explicit manual intervention

### 4. post-panic-recovery.service (blackwell)
- **Script:** `/usr/local/bin/post-panic-recovery.sh`
- **Service:** `/etc/systemd/system/post-panic-recovery.service`
- Runs as systemd oneshot on every boot
- Detects panic vs clean boot via pstore crash records and `/var/crash` vmcore count
- Saves pstore records to `/var/log/pstore-crashes/<timestamp>/` before they're
  cleared
- Waits 20s for services to settle, then:
  - Checks GPU binding state (logs vfio-pci bound devices)
  - Ensures libvirtd is running
  - Starts ghost-vm-new if shut off or crashed
  - Restarts bare-metal services: vfio-watchdog, container-logger, vast_metrics,
    xmrig-qrl
  - Sends ntfy alert: `urgent` priority for panic recovery, `low` for clean boot
- **ntfy config:** `https://ntfy.sh/blackwell-alerts`
- **Recovery log:** `/var/log/post-panic-recovery.log`
- **Panic archives:** `/var/log/pstore-crashes/`
- **Test run result:** clean — found ghost-vm-new already running, restarted
  vast_metrics.service (was not running), sent clean boot notification

### 5. blackwell-watchdog upgrade (gaming PC)
- **Script:** `/home/rich-rob/blackwell-watchdog.sh` (upgraded in-place, backup saved)
- **Service:** `blackwell-watchdog.service` (existing service file unchanged)
- **State files:** `/var/lib/blackwell-watchdog/` (created, owned by rich-rob)
- **Log:** `/var/log/blackwell-watchdog.log`
- **Config:**
  - Blackwell IP: `192.168.50.51`
  - ntfy: `https://ntfy.sh/blackwell-alerts`
  - Check interval: 30s
  - Failure threshold: 3 consecutive failures before alert
- Improvements over original:
  - Tracks `state` (up/down) and `down_since` timestamp in state files
  - Differentiates "just went down" vs "still down" vs "came back"
  - Reports exact downtime duration on recovery
  - Attempts NFS remount (`/mnt/data`) when blackwell comes back
  - Sends periodic reminders every 10 minutes while blackwell stays down
  - Alert priorities: `urgent` for going down, `high` for recovery
- **Permission fix required:** `/var/lib/blackwell-watchdog/` and
  `/var/log/blackwell-watchdog.log` needed `chown rich-rob:rich-rob` after
  creation (service runs as rich-rob, not root)

---

## Post-Reboot Verification (all passed)

```
cat /sys/kernel/kexec_crash_loaded     → 1
sudo ls /sys/fs/pstore/                → (empty, no prior panics)
systemctl status vfio-watchdog         → active (running)
systemctl status post-panic-recovery   → active (exited, status=0)
sudo virsh domstate ghost-vm-new       → running
```

---

## Known Remaining Gaps

**vendor-reset** — GB205 not supported. Watch https://github.com/gnif/vendor-reset
for RTX 50-series support. When it lands, install via DKMS and it supersedes the
10s settle delay workaround.

**VM crash behaviour** — `on_crash destroy` means hard kill, no GPU reset signal.
Consider changing to `on_crash restart` so libvirt attempts a clean restart cycle,
giving the GPU slightly more time. Risk: could loop if the VM keeps crashing.
Leave as-is for now.

**197 packages held back** — noted during apt operations. Unrelated to this work
but worth a `sudo apt upgrade` in a maintenance window.

---

## File Locations Summary

| File | Host | Purpose |
|------|------|---------|
| `/home/rich-rob/vfio-watchdog.sh` | blackwell | Active watchdog script |
| `/home/rich-rob/vfio-watchdog.sh.bak` | blackwell | Backup before VM name fix |
| `/home/rich-rob/vfio-watchdog.sh.bak2` | blackwell | Backup before settle delay patch |
| `/home/rich-rob/vfio-watchdog.sh.bak3` | blackwell | Backup before Python patch attempt |
| `/home/rich-rob/bootstrap-agent/gpu-safety/vfio-watchdog.sh` | blackwell | Repo copy (also patched) |
| `/etc/modprobe.d/vfio.conf` | blackwell | vfio-pci ID binding + disable_vga |
| `/etc/udev/rules.d/99-vfio-5070.rules` | blackwell | Hard driver_override at probe time |
| `/etc/default/grub` | blackwell | crashkernel + pstore + acpi_enforce |
| `/etc/default/kdump-tools` | blackwell | USE_KDUMP=1 |
| `/usr/local/bin/post-panic-recovery.sh` | blackwell | Boot recovery script |
| `/etc/systemd/system/post-panic-recovery.service` | blackwell | Oneshot recovery service |
| `/var/log/post-panic-recovery.log` | blackwell | Recovery run log |
| `/var/log/pstore-crashes/` | blackwell | Archived pstore panic records |
| `/var/crash/` | blackwell | kdump vmcore files |
| `/home/rich-rob/blackwell-watchdog.sh` | gaming PC | Upgraded external watchdog |
| `/var/lib/blackwell-watchdog/` | gaming PC | Watchdog state files |
| `/var/log/blackwell-watchdog.log` | gaming PC | Watchdog log |

---

## Scripts Delivered (in ~/Downloads on Mac)

| Script | Purpose |
|--------|---------|
| `01-fix-vfio-watchdog-vmname.sh` | Auto-finds and patches watchdog VM name |
| `02-enable-kdump-pstore.sh` | Installs kdump + configures GRUB |
| `03-install-vendor-reset.sh` | Attempts vendor-reset, reports 50-series status, applies fallbacks |
| `04-install-post-panic-recovery.sh` | Installs boot recovery service |
| `05-extend-blackwell-watchdog.sh` | Upgrades gaming PC watchdog |
| `README-panic-hardening.md` | Deployment guide for all five scripts |
