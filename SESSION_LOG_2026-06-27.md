# Session Log 2026-06-27

## Issues Addressed

### 1. Dashboard Anthropic Analysis Buttons (404)
**Symptom:** Two "Analyse" buttons returning 404.  
**Root cause:** Wrong model name in `dashboard.py` — `claude-sonnet-4-20250514` does not exist.  
**Fix:** `sed -i 's/claude-sonnet-4-20250514/claude-sonnet-4-6/g' dashboard.py`  
**Lesson:** When Anthropic analysis buttons 404, check the model string first. Valid model as of 2026-06: `claude-sonnet-4-6`.

---

### 2. CPU Pressure Thresholds Too Low
**Symptom:** Ghost VM mining kept pausing/resuming on a ~3 min loop with ntfy spam "Ghost-VM Mining Resumed".  
**Root cause:** `cpu_crit` was 82% — too low for normal blackwell load. CPU hitting 84% triggered pause, then dropped, resumed, repeat.  
**Fix:**
```python
"cpu_warn": 86,   # was 65
"cpu_crit": 90,   # was 82
```
**Also fixed:** Message "High memory pressure" was misleading — it fires on CPU OR RAM threshold. Renamed to "High CPU/memory pressure".

---

### 3. ACE-Step Autostart Disabled
**Symptom:** acestep.service starting on ghost VM boot, consuming GPU before miners.  
**Fix:** `sudo systemctl disable acestep.service` (inside ghost VM as root).  
**Verify:** `systemctl is-enabled acestep.service` → should return `disabled`.

---

### 4. Ghost VM Filesystem Corruption (Second Occurrence)
**Root cause:** `virsh reset` issued while VM was doing active disk writes. This is the same cause as the 2026-05-31 rebuild.  
**Symptoms:**
- `virsh domifaddr mining-ai-vm` returns empty (no IP)
- `qemu-img info ghost-vm-new.qcow2` shows `corrupt: true` and `file length: 5.66 EiB`
- VM boots but never gets DHCP lease

**⚠️ NEVER USE `virsh reset` or `virsh destroy` on a running VM.**  
**Always use:** `sudo virsh shutdown ghost-vm-new --mode agent`

---

## Ghost VM Full Rebuild — 2026-06-27

### Pre-rebuild safety checks
```bash
# Verify 5070 is on vfio BEFORE touching anything
nvidia-smi -L                          # Must show 5090 ONLY
lspci -nnk -s 03:00.0 | grep "driver in use"  # Must show vfio-pci
```

If 5070 shows on nvidia, rebind immediately:
```bash
echo "0000:03:00.0" | sudo tee /sys/bus/pci/drivers/nvidia/unbind
echo "0000:03:00.0" | sudo tee /sys/bus/pci/drivers/vfio-pci/bind
echo "0000:03:00.1" | sudo tee /sys/bus/pci/drivers/nvidia/unbind
echo "0000:03:00.1" | sudo tee /sys/bus/pci/drivers/vfio-pci/bind
```

### What was lost / what survived
- **Lost:** Ghost VM filesystem (qcow2 corrupt)
- **Survived:** All code in git (`bootstrap` branch), mining keys on blackwell, wallets

### Rebuild steps (condensed)

**1. Delete corrupt disk, create fresh:**
```bash
sudo virsh undefine mining-ai-vm   # or ghost-vm-new if already renamed
sudo rm /var/lib/docker/kvm-images/ghost-vm-new.qcow2
sudo qemu-img create -f qcow2 /var/lib/docker/kvm-images/ghost-vm-new.qcow2 500G
```

**2. Install Ubuntu via virt-install:**
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

VNC tunnel from Mac: `ssh -L 5900:127.0.0.1:5900 rich-rob@192.168.50.51`  
VNC client: **VNC Viewer** (not macOS Screen Sharing — it requires a password).  
Installer choices: hostname `ghost-vm`, user `rich-rob`, enable OpenSSH.

**3. Get new VM's MAC, pin static IP .143:**
```bash
# Inside VM:
ip link show   # note MAC of enp1s0

# On blackwell:
sudo virsh net-edit default
# Update <host mac='XX:XX:XX:XX:XX:XX' name='ghost-vm' ip='192.168.122.143'/>
sudo virsh net-destroy default && sudo virsh net-start default
# Inside VM: sudo netplan apply or reboot
```

**4. SSH key setup:**
```bash
ssh-copy-id -i /home/rich-rob/.ssh/id_ed25519_vm.pub rich-rob@192.168.122.143

# Root SSH (needed for dashboard control):
ssh -i /home/rich-rob/.ssh/id_ed25519_vm rich-rob@192.168.122.143
sudo mkdir -p /root/.ssh
sudo cp ~/.ssh/authorized_keys /root/.ssh/authorized_keys
sudo chmod 700 /root/.ssh && sudo chmod 600 /root/.ssh/authorized_keys
```

**5. qemu-guest-agent (CRITICAL — prevents future corruption):**
```bash
sudo apt update && sudo apt install -y qemu-guest-agent
sudo systemctl start qemu-guest-agent
# Verify from blackwell:
sudo virsh qemu-agent-command ghost-vm-new '{"execute":"guest-ping"}'
# Expected: {"return":{}}
```

**6. Attach RTX 5070 passthrough:**
```bash
sudo virsh shutdown ghost-vm-new --mode agent
sudo virsh edit ghost-vm-new
# Add inside <devices>:
# <hostdev mode='subsystem' type='pci' managed='yes'>
#   <source><address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/></source>
# </hostdev>
# <hostdev mode='subsystem' type='pci' managed='yes'>
#   <source><address domain='0x0000' bus='0x03' slot='0x00' function='0x1'/></source>
# </hostdev>
sudo virsh start ghost-vm-new
nvidia-smi -L   # Must still show 5090 only on host
```

**7. Nvidia drivers inside VM:**
```bash
sudo apt update && sudo apt install -y ubuntu-drivers-common
sudo ubuntu-drivers install
sudo reboot
# Verify: nvidia-smi  (should show RTX 5070)
```

**8. Docker + nvidia container toolkit:**
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker rich-rob

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update && sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
sudo systemctl restart docker
```

**9. Ollama:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
```

**10. Docker stack:**
```bash
git clone https://github.com/richnz2026/bootstrap-agent.git
cd bootstrap-agent
git checkout bootstrap
docker compose up -d
docker compose ps   # verify all 9 containers up
```

**11. Auto-start docker stack on boot:**
```bash
cat > /etc/systemd/system/ghost-stack.service << 'EOF'
[Unit]
Description=Ghost VM Docker Stack
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/rich-rob/bootstrap-agent
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ghost-stack
```

**12. Mining setup:**
```bash
# From blackwell:
scp -i ~/.ssh/id_ed25519_vm ~/.mining_keys root@192.168.122.143:/root/.mining_keys
scp -i ~/.ssh/id_ed25519_vm bootstrap-agent/ghost-vm/miners/mine_erg.sh root@192.168.122.143:/root/
scp -i ~/.ssh/id_ed25519_vm bootstrap-agent/ghost-vm/miners/mine_pearl.sh root@192.168.122.143:/root/
scp -i ~/.ssh/id_ed25519_vm bootstrap-agent/ghost-vm/miners/lolminer.service root@192.168.122.143:/tmp/
scp -i ~/.ssh/id_ed25519_vm bootstrap-agent/ghost-vm/miners/pearl.service root@192.168.122.143:/tmp/

# On VM as root:
mkdir -p /opt/lolminer && cd /tmp
wget https://github.com/Lolliedieb/lolMiner-releases/releases/download/1.97/lolMiner_v1.97_Lin64.tar.gz
tar -xzf lolMiner_v1.97_Lin64.tar.gz
cp 1.97/lolMiner /opt/lolminer/lolMiner && chmod +x /opt/lolminer/lolMiner
cp /tmp/lolminer.service /etc/systemd/system/lolminer.service
cp /tmp/pearl.service /etc/systemd/system/pearl.service
chmod +x /root/mine_erg.sh /root/mine_pearl.sh
systemctl daemon-reload
systemctl enable lolminer
systemctl enable pearl
# DO NOT start — dashboard controls them
```

**13. Update dashboard.py VM name:**
```bash
sed -i 's/mining-ai-vm/ghost-vm-new/g' /home/rich-rob/dashboard.py
sudo systemctl restart dashboard
```

---

## New Permanent Fixes Made This Session

### vfio-pci permanent binding (5070 always stays on vfio)
Previously the 5070 would fall back to nvidia when the VM stopped. Now fixed:
```bash
echo "options vfio-pci ids=10de:2f04,10de:2f80" | sudo tee /etc/modprobe.d/vfio.conf
echo "vfio-pci" | sudo tee /etc/modules-load.d/vfio-pci.conf
sudo update-initramfs -u
```

### Dangerous qemu hook disabled
`/etc/libvirt/hooks/qemu` was rebinding ALL nvidia GPUs (including 5070) back to nvidia on VM stop. Disabled:
```bash
sudo mv /etc/libvirt/hooks/qemu /etc/libvirt/hooks/qemu.disabled2
```

### libvirt network hook for bridge attachment
Added `/etc/libvirt/hooks/network` to auto-attach vnet interfaces to virbr0 when the network starts.

---

## Known Issues / Still To Do

- **Vast.ai 2x5090 confusion:** When the VM was destroyed with the 5070 unbound from vfio, Vast detected two 5090s. Once the current customer's rental ends, need to unlist and relist the machine.
- **ACE-Step start on dashboard:** The stop button was previously broken due to the model name issue (now fixed). Retest after this session.
- **Ollama model:** REBUILD.md says `qwen2.5:14b` was the previous model. Not yet pulled in this rebuild — pull when needed: `ollama pull qwen2.5:14b`

---

## Quick Reference — Ghost VM

| Service | Command |
|---------|---------|
| Clean shutdown | `sudo virsh shutdown ghost-vm-new --mode agent` |
| Start | `sudo virsh start ghost-vm-new` |
| SSH as user | `ssh -i ~/.ssh/id_ed25519_vm rich-rob@192.168.122.143` |
| SSH as root | `ssh -i ~/.ssh/id_ed25519_vm root@192.168.122.143` |
| Check GPU | `ssh ... 'nvidia-smi'` |
| Docker stack | `cd ~/bootstrap-agent && docker compose ps` |
| Guest agent ping | `sudo virsh qemu-agent-command ghost-vm-new '{"execute":"guest-ping"}'` |
| 5070 vfio check | `lspci -nnk -s 03:00.0 \| grep "driver in use"` → must be `vfio-pci` |
