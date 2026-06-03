# Kaalia Crash-Loop — Incident Runbook

First occurred 2026-06-02. Machine 55898 (RTX 5090, blackwell-node-01).

## The Failure Signature

A Vast customer container wedges during creation (stuck `runc create` + live
containerd-shim, container unkillable). Any `docker inspect` on it hangs
forever. Kaalia runs `docker inspect` on all containers each cycle, so it
freezes, gets killed, relaunches, hits the same container, freezes again —
crash-looping every ~2 min.

While looping, `send_mach_info` reports an empty container map `{}` to Vast
even though the customer container is still running. Vast bills on those
reports, so usage goes UNBILLED. The customer gets compute; you don't get paid.

SSH to blackwell still works the whole time (the box is up, only kaalia is
broken) — which is why the plain reachability watchdog does NOT catch it.

First incident window: 11:05–19:39 UTC 2026-06-02 (~8.5 hrs unbilled).

## How You Get Alerted

External watchdog on the Gaming PC: `gaming-pc/blackwell-watchdog.sh`
(systemd service `blackwell-watchdog`, probes blackwell every 60s over SSH).

- "Blackwell Down" (siren / Pushover priority 2) = box unreachable
- "Kaalia Unhealthy" (priority high) = box UP but kaalia broken — THIS incident.
  Triggers on: kaalia PID changed between probes (crash-loop), kaalia process
  missing, any `Dead` container, or any container that hangs `docker inspect`.

External by design so it still alerts when blackwell itself is wedged.
Note: Gaming PC dual-boots — coverage only while it's on Ubuntu.

## Response Steps — "Kaalia Unhealthy" alert

### 1. Confirm the symptom (SSH into blackwell)
    sudo tail -5 /var/lib/vastai_kaalia/kaalia.log
    sudo stat -c '%y' /var/lib/vastai_kaalia/kaalia.log; date   # advancing or frozen?
    docker ps -a --format '{{.Names}} {{.Status}}'              # any "Dead"?

Healthy = log timestamp within seconds of now, stable PID, no Dead containers.
Frozen = stale timestamp; PID keeps changing across checks = crash-loop.

### 2. Find the wedged container (the one hanging inspect)
    for c in $(docker ps -a --format '{{.ID}}'); do
      echo -n "$c: "; timeout 5 docker inspect $c >/dev/null 2>&1 && echo ok || echo HANGS
    done

### 3. Clear it surgically (replace <ID> with the hanging one)
    ps aux | grep <ID> | grep -E "runc|shim" | grep -v grep
    sudo kill -9 <runc_pid> <shim_pid>
    sudo pkill -9 -f "docker inspect <ID>"
    timeout 15 docker rm -f <ID>

### 4. Restart kaalia (prefer this over a reboot)
    sudo systemctl restart vastai
    sleep 12
    sudo tail -3 /var/lib/vastai_kaalia/kaalia.log   # must advance past docker inspect

### 5. If the container won't clear / docker wedges -> SAFE reboot
docker may get stuck in deactivating/final-sigkill. Reboot, but SAFELY —
clean-shutdown the VM FIRST or you risk destroying the VM filesystem
(that is what killed the original Ghost-VM):

    sudo virsh shutdown mining-ai-vm --mode agent   # clean VM shutdown FIRST
    sleep 20
    sudo virsh domstate mining-ai-vm                # wait for "shut off"
    nvidia-smi -L                                   # note GPU state (expect 5090 only)
    sync
    sudo reboot

After reboot: VM autostarts (grabs 5070; vfio-watchdog covers the window),
the Dead container usually clears with `docker rm -f`, kaalia recovers.

### 6. Always afterward
- Confirm on the Vast console the machine is ONLINE and billing.
- If there was an unbilled window, gather evidence for a support claim:
  - kaalia crash-loop: `sudo grep "Kaalia Launch!" /var/lib/vastai_kaalia/kaalia.log* | grep "<date>"`
  - reporting gap: `sudo grep "container start times" /var/lib/vastai_kaalia/send_mach_info.log` (look for the {} transition)
  - container ran: `~/container_history.log` and `~/container_history_commands/<NAME>.txt`

## GOLDEN RULE
Never hard-destroy / hard-reboot while the VM is mid-write. Always
`virsh shutdown mining-ai-vm --mode agent` first. This is why the reboot in
the first incident was safe and did not repeat the original disaster.
