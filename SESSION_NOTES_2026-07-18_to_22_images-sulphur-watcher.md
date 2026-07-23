# Session Notes — Images Vault Features, Sulphur-2 Generation, vast-watcher Fixes
*2026-07-18 to 2026-07-22*

---

## Summary

Extensive work on the Images vault page (blackwell/ghost scan, import, delete,
secure delete), added Sulphur-2 video generation directly from the dashboard
(prompt/duration/batch + raw JSON editing), fixed a broken audio-mux bug in the
Sulphur-2 ComfyUI workflow, survived a filesystem-corrupting host crash and
recovered it, and — critically — discovered and fixed a `vast_watcher.py`
bug that had it silently erroring every cycle for days, doing **no** protection
at all despite `systemctl status` showing "active."

---

## Images Vault — Blackwell Support (new)

Blackwell (the local host itself, via `~/comfy-sulphur/output/`) added as a
third scan/import/delete source alongside Docker and Ghost VM, mirroring the
existing Ghost pattern but using local filesystem ops instead of SSH:

- `GET /api/images/scan-blackwell?path=...` — recursive local scan, 200k image
  threshold, audio/video always included
- `POST /api/images/fetch-from-blackwell` — single-file add-to-samples, with
  ffmpeg thumbnail generation for .mp4
- `POST /api/images/import-dir-blackwell` — bulk import (now also generates
  thumbnails — see bugfix below)
- `DELETE /api/images/delete-blackwell` — hard-delete a source file
- `DELETE /api/images/delete-blackwell-file/<filename>` — vault copy + source
- `DELETE /api/images/delete-blackwell-folder` — bulk vault + optional source

### Bugfix: video thumbnails were broken
`import_dir_blackwell()` / `import_dir_ghost()` (bulk imports) never ran the
ffmpeg thumbnail extraction step that single-file fetch already did — so bulk-
imported `.mp4`s had no `_thumb.jpg`. Worse, `list_images()` pointed video
`thumb_url` at the `.mp4` file itself regardless, which can't render as `<img>`.
Fixed both: bulk imports now generate thumbnails, and `list_images()` uses the
real `_thumb.jpg` when present. **Existing already-imported videos need
re-import to get thumbnails** — no backfill script written yet.

### Folder-level selection + delete
Added a checkbox to each folder header in scan results — toggles every file in
that folder into the existing `_selectedFiles` set (works alongside individual
file selection). Generalized the old Ghost-only per-folder 🗑 button
(`deleteGhostDirContents` → `deleteScanDirContents`) to work in Blackwell mode
too and respect the secure-delete toggle.

### Secure Delete (global toggle)
🔒 checkbox near the top of the page, persisted in `localStorage`. When on,
delete operations run `shred -u -z -n 3` (3-pass overwrite + zero + unlink)
instead of a plain unlink/rm — genuinely unrecoverable, for both local
blackwell files and SSH'd ghost VM files.

**Currently covered:** single-file card delete (🗑 GHOST/SOURCE button),
scan-panel selected-files delete, per-folder scan delete.
**NOT yet covered** (flagged, not done): Master Delete, generic
`delete-folder/<type>/<container>`, plain vault-only `deleteSingle`. These need
a fresh code dump before patching — don't guess at their current text.

### Filename label
Every gallery card now shows the filename in small text under the thumbnail.

---

## Sulphur-2 Video Generation (new)

Three tabs replacing the old flat Import panel: **📁 IMPORT | 🎬 GENERATE
VIDEO | ⚙️ ADVANCED JSON**. Scan panel (right column) untouched.

### Generate Video tab
- Prompt (textarea), output subdirectory, duration in **seconds** (converts to
  valid `8n+1` frame count internally: `frames = 8*round((duration*24-1)/8)+1`),
  batch count (1-20)
- `POST /api/images/sulphur-generate` — checks ComfyUI is reachable
  (`:8188/system_stats`), loads `~/sulphur_template_api.json`, fills prompt
  (node 29) + frames (node 27) + randomizes both seeds (nodes 1, 2) + sets
  output filename_prefix (node 69), submits to ComfyUI's `/prompt` — one POST
  per batch item, non-blocking (ComfyUI queues them itself)
- After queuing: shows full output path (`/home/rich-rob/comfy-sulphur/output/
  <subdir>`) with **Copy** and **Copy to Import** buttons — the latter fills
  the shared `import-dir-path` field (used by both Import tab and Scan panel)
  and switches to the Import tab, so you can immediately scan/import the
  result

### Advanced JSON tab
- Full API-format workflow in an editable textarea, loads from
  `GET /api/images/sulphur-template` on first open
- "Use this JSON when generating" checkbox — **default checked**. When on,
  `sulphur-generate` uses the edited JSON as the base instead of the on-disk
  file; prompt/frames/subdir/seeds still get overlaid on top regardless
- **Auto-save with backup**: any edit triggers a debounced (1.2s) save via
  `POST /api/images/sulphur-template`, which backs up the previous version to
  `~/sulphur_template_backups/sulphur_template_api_<timestamp>.json` before
  overwriting `~/sulphur_template_api.json`

### Bugfix: black video / audio crash
`av.error.ArgumentError: 'avcodec_send_frame()' returned 22` at `SaveVideo` —
audio codec rejecting a malformed final frame. Root cause was NOT the 8n+1
frame rule (241 and 361 are both valid `8n+1` and still crashed). Fix: bypass
node 23 (`LTXVAudioVAEDecode`) — **but bypassing alone isn't enough**, ComfyUI
doesn't clear the downstream link when a node is bypassed. Must also manually
**delete the wire** from node 23's output into `CreateVideo`'s (node 38) audio
input (link 57) — leaving the link connected to a bypassed node's dead output
was producing the black-video symptom (CreateVideo reading garbage/empty
audio, defaulting to zeroed frames). Confirmed fixed once the wire was removed.

Video-only render tested clean after the fix; audio path remains an open
investigation for a properly-working version (out of scope this session).

---

## Blackwell Filesystem Corruption + Recovery (2026-07-21)

Machine dropped to `(initramfs)` busybox rescue prompt — kernel booted but
couldn't mount root. Root cause: user set GPU to max power/clock (3090MHz/
575W), started Sulphur-2, ran jobs, then it crashed hard enough to corrupt the
ext4 filesystem (not just hang, unlike prior thermal incidents).

**Recovery:**
```
fsck -y /dev/mapper/ubuntu--vg-ubuntu--lv
```
Fixed extent tree optimizations, a stray casefold flag on a non-directory,
block/inode bitmap mismatches. `FILE SYSTEM WAS MODIFIED`, no data-loss
indicators. Reboot succeeded cleanly afterward. `post-panic-recovery.service`
ran correctly on the clean boot and re-applied GPU limits.

**Decision:** GPU limits reverted to stock (3090MHz/575W) both live and in the
boot-persistent script (`/usr/local/bin/post-panic-recovery.sh`), replacing the
earlier conservative 2100MHz/450W default. Philosophy: manage thermal risk
*reactively* (see thermal-cap below) rather than pre-emptively crippling
performance. Server room door confirmed open as the actual root-cause fix.

---

## vast_watcher.py — Critical Bug Found and Fixed

**Discovery:** while wiring up a new feature, found the live deployed
`/usr/local/bin/vast_watcher.py` was missing an entire thermal-cap feature
that had been "added" in an earlier session — it turned out that session's
patch script had been written and staged but **never actually deployed** to
blackwell. Separately, a *different* half-applied patch (adding
`rule_manage_sulphur` call without the function definition) had the watcher
**crash-looping silently every single cycle** for an unknown period:
```
Cycle error: name 'rule_manage_sulphur' is not defined
```
`systemctl status` showed "active (running)" the whole time — the error was
caught by the main loop's own `try/except` and just logged, so **the watcher
provided zero protection** (no pearl management, no anomaly detection, nothing)
while appearing healthy. This is a real gap in the watcher's own self-
monitoring — worth adding a "last successful cycle" check to `detect_anomalies`
itself in a future session, so the watcher can alert on its own silent failure.

**Fixed by rebuilding all missing pieces against the confirmed-current live
file** (not from a possibly-stale local copy):
- CFG: `dashboard_url`, `gpu_temp_cap_trigger` (85°C), `gpu_temp_sustained_s`
  (120s), `gpu_temp_cap_watts` (500W)
- `obs_im_customer()`, `obs_sulphur()` — both query the dashboard's own API
  (`:8080`) rather than duplicating logic
- `action_stop_sulphur()` — POSTs `{"force": true}` to
  `/api/control/sulphur/stop`, sends ntfy
- `action_cap_power_thermal()` — `nvidia-smi -pl 500`, sends urgent ntfy
  naming the server room door as the likely cause
- `rule_manage_sulphur()` — stops Sulphur if a customer is present AND
  Sulphur is running AND the im-customer marker is NOT set. **Never
  auto-restarts Sulphur** — that stays a manual dashboard action.
- `rule_thermal_cap()` — same pattern as the pearl-cooldown logic: tracks
  `high_temp_since`, fires once after sustained threshold, resets when temp
  drops back down so it can re-fire later in the same session

**Live-tested and confirmed working same session:** a genuine external Vast
customer (`C.45456114`, `"mine": false` per dashboard's own detection) connected
while Sulphur-2 was running — watcher correctly force-stopped it within one
60s cycle. User then self-rented with the new toggle checked, and the watcher
correctly left Sulphur alone on the following cycle. Both directions verified.

---

## "I'm the Customer" Feature (new)

Since Vast container labels carry no owner/account identifier (checked via
`docker inspect ... Config.Labels` — only generic Vast.ai image metadata, no
way to distinguish self-rentals from strangers automatically), built a manual
marker-file toggle instead:

- **Dashboard**: 🙋 checkbox in the Sulphur control panel. `GET`/`POST
  /api/control/im-customer`, marker file `~/.im_the_customer`, **auto-expires
  after 6 hours** (checked both when read and enforced server-side) so a
  forgotten toggle doesn't stay stuck on indefinitely
- Status text shows live countdown ("ON — expires in ~Nm"), refreshed every
  60s via `refreshImCustomer()`
- vast-watcher's `rule_manage_sulphur()` checks this marker before deciding
  whether to force-stop Sulphur

**Note:** the dashboard's pre-existing `"mine": false"` flag in
`/api/control/sulphur/status` is a **separate, unrelated mechanism** (likely
checking a worker-name/tag pattern) — it is cosmetic/informational only and
does NOT control the new stop-protection logic. Worth harmonizing these two
"am I the customer" signals in a future session if the pre-existing one turns
out to be more reliable than the manual toggle.

---

## Deployment Lessons (recorded for next session)

Several patch scripts aborted mid-session because they assumed file state
from earlier in the conversation that had since drifted (either from other
edits, or because a prior "deploy this" instruction was discussed but the
actual scp/run step got skipped when the conversation moved to a different
topic). Recovered each time by re-confirming exact current file content via
`grep`/`sed` before writing a corrected patch — never guessed blind twice.
**Practice to continue:** always verify a target function's live text
immediately before patching if more than one topic has been discussed since
it was last directly viewed, and always confirm a patch script's backup file
was actually created as proof the write happened, not just that scp completed.

---

## Files Modified This Session

| File | Host | Changes |
|------|------|---------|
| `image_routes.py` | blackwell | Blackwell scan/import/delete routes, secure-delete helper, sulphur-generate + sulphur-template routes, thumbnail fix |
| `image_dashboard.html` | blackwell | 3-tab restructure, secure-delete checkbox, folder checkboxes, filename labels, Sulphur generate/advanced UI |
| `dashboard.py` | blackwell | im-customer marker endpoint + UI toggle + JS |
| `/usr/local/bin/vast_watcher.py` | blackwell | Fixed crash-loop bug, added Sulphur-stop rule, added thermal-cap rule (both previously missing despite being "sessions ago" work) |
| `sulphur_template_api.json` | blackwell | New — the generation template, now auto-backed-up on edit |
| `ltx23_t2v_sulphur_*.json` (ComfyUI workflow) | blackwell | Audio decode node bypassed + wire removed to fix black-video bug |

---

## Open Items

| Item | Status |
|------|--------|
| Master Delete / folder-delete / deleteSingle secure-delete support | ❌ Flagged, needs fresh code review first |
| Backfill thumbnails for already-imported videos | ❌ Not written |
| Audio path in Sulphur-2 (root cause of avcodec crash) | ⚠️ Worked around (bypass), not fixed |
| watcher self-monitoring (detect its own cycle errors) | ❌ Gap identified, not built |
| Harmonize "mine" flag vs "I'm the customer" marker | ❌ Two separate signals currently |
| Peak VRAM at canonical 1366×768×241 resolution | ❌ Still unmeasured (carried from earlier build doc) |
