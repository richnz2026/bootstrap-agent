# Ghost-VM Rebuild Runbook
**Reconstructed 2026-05-31 after filesystem loss.**

The previous Ghost-VM (`mining-ai-vm`) lost its root filesystem: the GPT/MBR
partition tables and the ext4 superblock (primary + all backups) were
destroyed. Diagnosis showed the NVMe is healthy (1% wear, 0 media errors);
the most likely cause was a hard `virsh reset`/`destroy` during an active
disk write (38 unsafe shutdowns on record). **The fix for next time is
clean shutdowns + a guest agent — built into this runbook.**

What survived: custom code (`human_control_api`, `human_control_dashboard`)
in this GitHub repo; keys/wallets on blackwell. What was lost: the
orchestration config (now reconstructed as `docker-compose.yml`).

---

## ⚠️ RTX 5070 SAFETY — READ FIRST

The 5070 must stay bound to `vfio-pci` and hidden from the host nvidia
driver / Vast. **None of the steps below rebind the GPU**, but verify at
every VM start.

**The check (run before AND after every VM start):**
```bash
nvidia-smi -L
# MUST show 5090 only. If 5070 appears -> STOP, see "Re-protect" below.
```

**Re-protect (only if 5070 ever shows as nvidia-bound):**
```bash
echo "0000:03:00.0" | sudo tee /sys/bus/pci/drivers/nvidia/unbind
echo "0000:03:00.0" | sudo tee /sys/bus/pci/drivers/vfio-pci/bind
echo "0000:03:00.1" | sudo tee /sys/bus/pci/drivers/nvidia/unbind
echo "0000:03:00.1" | sudo tee /sys/bus/pci/drivers/vfio-pci/bind
```

**Never during this rebuild:** reboot blackwell, run nvidia driver ops
against 03:00.x, or let Vast claim all GPUs. The Vast customer on the 5090
is unaffected by any VM disk/build work.

---

## Phase 1 — Create the fresh VM (does NOT touch the 5070)

Old disk images are corrupt but KEEP them until the new VM is verified:
```bash
sudo ls -lh /var/lib/docker/kvm-images/
# mining-ai-vm.qcow2  (corrupt original)
# mining-ai-vm.qcow2.backup / .clean-backup  (also corrupt)
# ubuntu-24.04-server.iso  (installer — already present)
```

Create a NEW qcow2 (don't overwrite the old ones — new name):
```bash
sudo qemu-img create -f qcow2 \
  /var/lib/docker/kvm-images/ghost-vm-new.qcow2 500G
```

Install Ubuntu. Easiest is virt-install pointing at the local ISO. **Do not
attach the 5070 yet** — install the OS first, add passthrough after.
```bash
sudo virt-install \
  --name ghost-vm-new \
  --memory 16384 --vcpus 8 \
  --disk path=/var/lib/docker/kvm-images/ghost-vm-new.qcow2,format=qcow2,bus=virtio \
  --cdrom /var/lib/docker/kvm-images/ubuntu-24.04-server.iso \
  --os-variant ubuntu24.04 \
  --network network=default,model=virtio \
  --graphics vnc,listen=127.0.0.1 \
  --noautoconsole
```
Note: VNC bound to 127.0.0.1 (more secure than the old 0.0.0.0). Tunnel from
the Mac to reach it: `ssh -L 5900:127.0.0.1:5900 rich-rob@192.168.50.51`

Run the OS install through VNC. Inside the installer:
- Enable OpenSSH server.
- Single ext4 root is fine. Set hostname `ghost-vm`.
- Create your user; add your SSH key.

After install completes and the VM reboots into Ubuntu:
```bash
nvidia-smi -L          # confirm 5090-only before doing anything else
```

---

## Phase 2 — Static IP .143 (fixes the DHCP drift)

The old VM drifted .142<->.143 because it used dynamic DHCP. Pin a static
lease to its MAC in libvirt's default network.

Get the new VM's MAC:
```bash
sudo virsh domiflist ghost-vm-new      # note the MAC, e.g. 52:54:00:xx:xx:xx
```

Edit the libvirt network to add a static host entry:
```bash
sudo virsh net-edit default
```
Inside the `<dhcp>` block add (using the real MAC):
```xml
<host mac='52:54:00:XX:XX:XX' name='ghost-vm' ip='192.168.122.143'/>
```
Then:
```bash
sudo virsh net-destroy default && sudo virsh net-start default
# inside the VM, renew DHCP or reboot; confirm it comes up on .143
ping -c3 192.168.122.143
```

Your existing `~/.ssh/config` already targets 192.168.122.143 with
`id_ed25519_vm`, so SSH will work unchanged.

---

## Phase 3 — qemu-guest-agent (prevents a repeat of the filesystem loss)

This is the single most important preventative step. With the agent
installed you can do CLEAN shutdowns instead of hard `destroy`.

Inside the VM:
```bash
sudo apt update && sudo apt install -y qemu-guest-agent
sudo systemctl enable --now qemu-guest-agent
```

From the host, verify the agent responds:
```bash
sudo virsh qemu-agent-command ghost-vm-new '{"execute":"guest-ping"}'
```

**New shutdown discipline — use these, NOT `virsh destroy`:**
```bash
sudo virsh shutdown ghost-vm-new --mode agent   # clean guest shutdown
# only if truly hung, as last resort:
# sudo virsh destroy ghost-vm-new
```

---

## Phase 4 — Attach the RTX 5070 passthrough (GPU-sensitive)

Now add the GPU. Check before and after.

```bash
nvidia-smi -L                      # 5090-only expected (VM should be OFF for edit)
sudo virsh shutdown ghost-vm-new --mode agent
```

Add the hostdev for 03:00.0 (and audio 03:00.1) to the VM XML:
```bash
sudo virsh edit ghost-vm-new
```
Add inside `<devices>`:
```xml
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source><address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/></source>
</hostdev>
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source><address domain='0x0000' bus='0x03' slot='0x00' function='0x1'/></source>
</hostdev>
```
Then start and immediately verify the host still can't see the 5070:
```bash
sudo virsh start ghost-vm-new
nvidia-smi -L                      # MUST still be 5090-only
lspci -nnk -s 03:00.0 | grep "driver in use"   # want: vfio-pci
```
Inside the VM, confirm the GPU is present to the guest:
```bash
nvidia-smi                         # should list the 5070 INSIDE the VM
```

---

## Phase 5 — Host services (ollama + nvidia toolkit), inside the VM

The original ran ollama as a HOST systemd service (v0.18.0) and used NVIDIA
CDI so containers could use the 5070.

NVIDIA container toolkit + CDI (the boot logs showed `nvidia-cdi-refresh`):
```bash
# (install nvidia driver in guest first if not already)
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
sudo systemctl restart docker
```

Ollama (host service, bound to 0.0.0.0:11434):
```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
ollama pull qwen2.5:14b            # VERIFY model — Phase 2.4 was Qwen 14B
ollama list
```

---

## Phase 6 — Restore the stack

```bash
git clone https://github.com/richnz2026/bootstrap-agent.git
cd bootstrap-agent
# add the reconstructed files (docker-compose.yml, prometheus.yml) — see repo
docker compose up -d
docker compose ps                  # confirm all 9 services up
```

Verify the ports respond (from blackwell or the Mac):
- openhands           http://192.168.122.143:3000
- human-control-api   http://192.168.122.143:8000
- dashboard           http://192.168.122.143:8001
- grafana             http://192.168.122.143:3001
- prometheus          http://192.168.122.143:9090
- qdrant              http://192.168.122.143:6333
- searxng             http://192.168.122.143:8080
- ntfy                http://192.168.122.143:9093
- ollama (host)       http://192.168.122.143:11434

---

## Phase 7 — Reconnect blackwell-side + optional extras

- Update the blackwell dashboard / watchdogs to point at .143 (should already).
- **ERG mining: OPTIONAL / deprioritized** (poor payout). If wanted later,
  recreate `mine_erg.sh` + a systemd unit inside the VM using lolMiner on the
  5070, pool from `~/.mining_keys` on blackwell.

---

## Phase 8 — Cleanup (only AFTER the new VM is verified working)

```bash
# Reclaim ~750G once you're sure the new VM is good:
sudo rm /var/lib/docker/kvm-images/mining-ai-vm.qcow2
sudo rm /var/lib/docker/kvm-images/mining-ai-vm.qcow2.backup
sudo rm /var/lib/docker/kvm-images/mining-ai-vm.qcow2.clean-backup
# Keep ~/ghost-recovered-keep/ logs as a reference if you like.
```

---

## Lessons baked in
1. **Clean shutdowns** via qemu-guest-agent — never hard `destroy` a running VM.
2. **Static IP** so the VM is reliably reachable / watchdog-monitored.
3. **Config in git** — docker-compose.yml + prometheus.yml now committed, so a
   filesystem loss can't take the orchestration with it again.
4. **VNC on 127.0.0.1** + SSH tunnel, not 0.0.0.0.
5. Consider periodic `qemu-img convert` snapshots of the new qcow2 while OFF,
   as real backups (the old "backups" were taken too late, of a broken FS).
