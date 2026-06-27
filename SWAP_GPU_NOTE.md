# GPU Reporting Fix — swap_gpu.py -s

When the 5070 is accidentally exposed alongside the 5090, Vast.ai's GPU
reporting gets confused and shows the wrong GPU count/identity to the
marketplace. This script fixes it by resetting Vast.ai's webserver record
to match what `nvidia-smi -L` actually sees right now.

## Command

```bash
sudo python3 /var/lib/vastai_kaalia/version_300/swap_gpu.py -s
```

Expected output:
```
Updating machine on webserver...
https://console.vast.ai/api/v0/machine/set_gpus
Got Result:
{"success": true}
```

## Safe to run with active customers

The `-s` flag (skip_swap) bypasses all GPU UUID swapping, container
rebuilding, and `nvidia_smi.json` edits entirely. It **only** calls
Vast.ai's webserver to reset the GPU count with current live UUIDs.
No container restarts, no GPU reassignment, no impact on active rentals.
Confirmed working 2026-06-20 with a live customer session active.

## Without -s — do NOT run with active customers

Running without `-s` performs actual GPU UUID swapping in kaalia's config
and rebuilds active Vast.ai containers via `commit_container.py`. This
will impact any active customer session. Only use this mode between
rentals, when no containers are running, or when explicitly recovering
from a broken GPU assignment.

## Version path note

The path includes a version number (`version_300`) that increments with
kaalia updates. If the command stops working, check for a newer version:

```bash
ls /var/lib/vastai_kaalia/ | grep -v backup | sort
```

Use the highest version number directory. The `-s` behaviour is unlikely
to change between versions since it's a simple webserver call.

## See also
- `KAALIA_INCIDENT_RUNBOOK.md` — broader kaalia shim failure history
- `SESSION_LOG_2026-06-20.md` — full context on tonight's session
