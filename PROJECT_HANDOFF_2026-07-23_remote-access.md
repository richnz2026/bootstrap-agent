# Project Handoff — Remote Access Build (Next Session)
*Written 2026-07-23. User traveling from tomorrow — main requirement is
reliable recovery from bad states on blackwell while away, accessed
remotely from a Mac on the road.*

---

## What This Next Session Needs to Build

**Priority order:**
1. **Tailscale** on blackwell + Mac + phone — private network access, no port-forwarding, no public exposure
2. **Tailscale HTTPS** (`tailscale cert` + `tailscale serve`) — real Let's Encrypt cert on the tailnet hostname, required before passkeys work
3. **Passkey auth** in front of the dashboard (`:8080`) — WebAuthn, Mac Touch ID/Face ID as the unlock, since this dashboard controls mining, GPU power, disk wipes, Vast actions, and Sulphur-2

**Why this order:** WebAuthn/passkeys require HTTPS (browsers block it over plain HTTP except on `localhost`). Tailscale can auto-provision that cert for the tailnet hostname with minimal setup. Basic Tailscale access alone (no HTTPS/passkey yet) still gives usable, private remote access as a stopgap if the full build doesn't finish before departure.

**Explicit non-goal:** no public port-forwarding of the dashboard, ever. Consistent with the project's existing philosophy (OpenClaw's design spec already mandates Tailscale/VPN over port-forwarding for exactly this reason).

---

## Recovery Posture While Traveling (the actual priority)

This is what protects blackwell while unattended, all confirmed working as of tonight:

| Layer | What it does | Status |
|---|---|---|
| `post-panic-recovery.service` | Runs on every boot (clean or panic). Re-applies GPU clock/power (currently stock 3090MHz/575W), checks vfio-pci binding, restarts ghost-vm-new if down, restarts bare-metal services, sends ntfy | ✅ Tested across multiple real reboots |
| `blackwell-watchdog.service` (gaming PC) | External monitor — detects blackwell going down/coming back, reports downtime, attempts NFS remount | ✅ Running |
| `vast-watcher.service` | Runs every 60s on blackwell. Three active rules (see below) | ✅ Fixed tonight after being silently broken for days |
| kdump + pstore | Crash diagnostics for any future panic | ✅ Active (`kexec_crash_loaded=1`) |

**vast-watcher's three live rules (as of tonight, confirmed working, including one live-tested against a real customer):**
1. **Pearl-vs-customer** — stops PRL mining (`pearl.service` on ghost VM) when a Vast customer is present, resumes 5 min after they leave
2. **Sulphur-vs-customer** — force-stops Sulphur-2 (ComfyUI) when a customer connects, UNLESS the "I'm the customer" marker is set (self-rental). Never auto-restarts Sulphur — that stays manual. **Live-tested tonight**: correctly stopped Sulphur for a genuine stranger, correctly left it alone once the user toggled "I'm the customer" for their own rental.
3. **Thermal cap** — if the 5090 sustains ≥85°C for 2+ minutes, auto-caps power to 500W and sends an urgent ntfy naming the server room door as the likely cause

**Known gap (accepted, deferred to future always-on hardware):** vast-watcher has no self-monitoring. It was found tonight to have been silently crash-looping every single cycle for an unknown period — `systemctl status` showed "active (running)" the whole time, but a `NameError` was caught by the main loop's try/except and just logged, providing zero actual protection. **If you notice odd behavior while traveling (miner running when it shouldn't, Sulphur running with a stranger connected), check `journalctl -u vast-watcher -n 30` for repeating `Cycle error:` lines** — that's the signature of this failure mode recurring.

---

## System Architecture Summary

**Three nodes:**
- **blackwell** (bare metal) — RTX 5090, Vast.ai rental host, runs the dashboard (`:8080`), Sulphur-2/ComfyUI (`:8188`), hosts ghost VM via KVM/libvirt with RTX 5070 passed through via VFIO
- **ghost VM** (KVM guest on blackwell) — RTX 5070, runs pearl-miner (PRL), 10+ other containers (OpenHands, Redis, Qdrant, Grafana, etc. from the original bootstrap-agent build), qwen3:14b via ollama
- **gaming PC** — dual-boot Windows/Ubuntu, RTX 5070, lolMiner (ERG/CFX coin-switching), independently controllable stop/start from the dashboard

**Key recurring lesson from tonight, worth remembering going forward:** `TEMPLATE` in `dashboard.py` is a plain (non-raw, non-f-string) triple-quoted Python string. Any `\n` you want to survive into the served JS as a literal 2-character escape sequence must be written as `\\n` (double backslash) in the Python source — a single `\n` gets converted to a real newline by Python's own parser before the browser ever sees it, which silently breaks JS string literals. This bit us multiple times tonight; same applies to any future dashboard.py HTML/JS edits.

---

## What Changed Recently (this session, 2026-07-18 to 22)

Full detail in `SESSION_NOTES_2026-07-18_to_22_images-sulphur-watcher.md` (committed to
`bootstrap-agent` repo). Summary:

- **Images vault** (`/images` page): Blackwell added as a scan/import/delete source
  alongside Docker/Ghost. Folder-level checkbox selection. Secure Delete (shred) toggle
  — covers single-file and per-folder deletes; **NOT yet wired into Master Delete or
  `deleteSingle`**. Video thumbnail generation fixed for bulk imports. Filename labels
  under cards.
- **Sulphur-2 generation from the dashboard**: prompt/subdir/duration(seconds)/batch-count
  UI, converts duration to valid `8n+1` frame count automatically, submits to ComfyUI's
  own `/prompt` queue. Advanced JSON tab with autosave+backup on every edit.
- **Fixed the Sulphur-2 black-video bug**: bypassing the audio decode node alone wasn't
  enough — had to also manually delete the dead wire into `CreateVideo`'s audio input.
  Video-only renders confirmed working. Audio path itself still broken (deferred, out
  of scope — item 3 on the open list, explicitly skipped by user for now).
- **Survived and recovered a real filesystem-corrupting crash** on blackwell (max GPU
  power + Sulphur render → hard crash → `fsck -y` recovery, no data loss). GPU limits
  reverted to stock; philosophy shifted to *reactive* thermal management (the cap rule
  above) rather than pre-emptive conservative limits.
- **Found and fixed the vast-watcher silent-failure bug** described above.
- **Built "I'm the customer" feature** — manual marker toggle (6h auto-expiry) since
  Vast container labels carry no owner/account identifier to distinguish self-rentals
  from strangers automatically.
- **Built "Wipe Free Space (Secure)"** — dashboard button, Blackwell or Ghost VM target,
  zero-fills all free disk space then deletes the fill file (protects against recovery
  of previously plain-deleted files). Refuses to run if a customer/render is active
  unless forced. **Confirmed NOT applicable to the user's Mac** — APFS explicitly
  rejects `diskutil secureErase freespace`, and Apple's actual current recommendation
  for SSDs is FileVault (full-disk encryption), not free-space wiping.

---

## Open Items (carried forward, prioritized per user tonight)

| # | Item | Status |
|---|------|--------|
| 1 | Secure-delete in Master Delete / folder-delete / `deleteSingle` | ✅ User confirmed done |
| 2 | Backfill thumbnails for pre-existing videos | ✅ User confirmed done |
| 3 | Sulphur-2 audio path (root cause of `avcodec_send_frame` crash) | ⏭️ Explicitly skipped for now |
| 4 | vast-watcher self-monitoring | ⏳ Deferred — will be solved when an always-on secondary machine (e.g. Mac Mini) is acquired |
| 5 | Harmonize dashboard's pre-existing `"mine"` flag vs new "I'm the customer" marker | ✅ Confirmed no conflict — `"mine"` is display-only, marker is the only thing gating stop logic |
| 6 | Agent GitHub PAT (original bootstrap blocker, oldest open item in the whole project) | ❌ Still not done |
| 7 | Peak VRAM measurement at full 1366×768×241 Sulphur-2 resolution | ⏳ Deferred — customer currently active on the 5090 |
| **NEW** | Tailscale + Tailscale HTTPS + Passkey auth for remote dashboard access | ❌ This session's task |

---

## Quick Reference

- Dashboard: `http://192.168.50.51:8080` (LAN only currently)
- Images vault: `http://192.168.50.51:8080/images`
- Sulphur-2 UI: `http://192.168.50.51:8188`
- Ghost VM: `192.168.122.143` (SSH via `~/.ssh/id_ed25519_vm`, root)
- Gaming PC: `192.168.50.172`
- ntfy: `https://ntfy.sh/blackwell-alerts`
- Repo: `~/bootstrap-agent` on blackwell, session notes committed there after each session
