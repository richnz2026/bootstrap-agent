#!/bin/bash
echo "=== Rebinding RTX 5070 to vfio-pci ==="

echo vfio-pci > /sys/bus/pci/devices/0000:03:00.0/driver_override
echo 0000:03:00.0 > /sys/bus/pci/drivers/nvidia/unbind 2>/dev/null || true
echo 0000:03:00.0 > /sys/bus/pci/drivers_probe

echo "=== Verifying ==="
lspci -ks 03:00.0

echo "=== Regenerating CDI ==="
nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
systemctl restart docker

echo "=== Done — check status ==="
lspci -ks 03:00.0 | grep "driver in use"
