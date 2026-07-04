# Session Notes — OpenHands Bootstrap & Blackwell Maintenance
*2026-06-28 to 2026-07-04*

---

## Summary

OpenHands was successfully connected to local ollama, but required switching from
`qwen2.5-coder:14b` to `qwen3:14b` due to a litellm/OpenHands thinking parameter
incompatibility. Blackwell had a D-Bus failure requiring a sysrq hard reboot.
Post-panic-recovery service performed correctly on reboot.

---

## OpenHands LLM Configuration

### Final Working Configuration
- **Model:** `ollama/qwen3:14b`
- **Base URL:** `http://172.17.0.1:11434`
- **API Key:** blank

### Why qwen2.5-coder:14b Failed
OpenHands 1.7 / litellm unconditionally sends a `think: true` (or similar thinking/
reasoning) parameter when initialising agents. `qwen2.5-coder:14b` does not support
thinking mode and rejects this with:
```
OllamaException - {"error":"\"qwen2.5-coder:14b\" does not support thinking"}
```
This is a known issue affecting multiple tools (Continue, crush, claude-code-router)
with qwen2.5-coder and other non-thinking ollama models. Env vars tried that did NOT
fix it: `DISABLE_THINKING=true`, `LLM_THINKING_ENABLED=false`. These are not
recognised by the agent-server.

**Fix:** Pull `qwen3:14b` which natively supports thinking mode, so OpenHands'
thinking parameter is accepted:
```bash
ollama pull qwen3:14b
```
Update model in OpenHands Advanced Settings UI to `ollama/qwen3:14b`.

### Why Base URL is 172.17.0.1
Ollama runs as a systemd host service on ghost VM (not in Docker). It was originally
bound to `127.0.0.1:11434` only. Fixed by adding `OLLAMA_HOST=0.0.0.0:11434` via
systemd override:
```bash
sudo systemctl edit ollama
# Add: [Service]
#      Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
```
The OpenHands container uses `host.docker.internal` which resolves to `172.17.0.1`
(default bridge gateway) inside the openhands container. The oh-agent-server
containers also use `172.17.0.1` — confirmed working via curl test from inside the
container.

---

## OpenHands Networking Architecture

OpenHands 1.7 spawns a separate `oh-agent-server` container per conversation with
**random high ports** (e.g. 42923, 50001, 58263). The browser connects directly to
these ports via WebSocket. This makes remote access via SSH tunnel fragile.

### Current Workaround
A helper script `~/oh-tunnel.sh` on the Mac automatically detects the current
agent-server port and opens a tunnel:

```bash
#!/bin/bash
echo "Getting agent-server port..."
PORT=$(ssh -o StrictHostKeyChecking=no rich-rob@192.168.50.51 \
  "ssh -o StrictHostKeyChecking=no rich-rob@192.168.122.143 \
  'sudo docker ps 2>/dev/null | grep oh-agent-server | \
  grep -o \"0\.0\.0\.0:[0-9]*->8000\" | cut -d: -f2 | cut -d- -f1'")
echo "Agent server port: $PORT"
if [ -z "$PORT" ]; then
    echo "No agent-server running — open a conversation in OH first"
    exit 1
fi
ssh -L 3000:192.168.122.143:3000 -L ${PORT}:192.168.122.143:${PORT} \
  -o ServerAliveInterval=30 -o TCPKeepAlive=yes rich-rob@192.168.50.51
```

**Workflow each session:**
1. Kill old tunnel processes if any: `lsof -i :3000 | grep ssh | awk '{print $2}' | xargs kill`
2. Open `http://localhost:3000` via existing tunnel or start one manually
3. Start a new conversation in the browser
4. Run `~/oh-tunnel.sh` — it detects the port and opens the tunnel
5. Refresh browser

### Access URLs
- OpenHands UI: `http://localhost:3000` (via SSH tunnel from Mac)
- Direct on LAN: `http://192.168.50.51:3000` (nginx proxy on blackwell → ghost VM)
- Ghost VM direct: `http://192.168.122.143:3000`

### nginx Proxy on Blackwell
nginx installed on blackwell to proxy port 3000 → ghost VM:
- Config: `/etc/nginx/sites-available/openhands`
- Started manually with `sudo nginx` (systemd was broken at time of install)
- Proxies `192.168.50.51:3000` → `192.168.122.143:3000` with WebSocket upgrade headers

### Ghost VM Network Notes
- Guest VM is on libvirt NAT network `192.168.122.0/24`
- Not directly reachable from Mac without SSH tunnel
- Docker bridge: `172.18.0.1` (bootstrap-agent_agent-net gateway)
- Default bridge: `172.17.0.1` (used by oh-agent-server containers)
- Ollama reachable from oh-agent-server containers at `172.17.0.1:11434`

---

## docker-compose.yml Changes (bootstrap-agent)

Key environment variables added/changed in the openhands service:

```yaml
- LLM_BASE_URL=http://172.17.0.1:11434
- LLM_MODEL=ollama/qwen2.5-coder:14b          # set in UI, not effective here
- USE_HOST_NETWORK=true                         # openhands on host network
- SANDBOX_LOCAL_RUNTIME_URL=http://192.168.122.143:40000
- OH_AGENT_SERVER_BASE_URL=http://192.168.122.143:40001
- AGENT_SERVER_PORT=40001                       # not effective in 1.7
- LLM_DISABLE_VISION=true
- DISABLE_THINKING=true                         # not effective
- LLM_THINKING_ENABLED=false                    # not effective
```

**Note:** Most thinking-related env vars are ineffective in OpenHands 1.7.
The only working solution is using a model that supports thinking (qwen3:14b).

**Note:** `USE_HOST_NETWORK=true` means the openhands container shares the host
network stack. Port bindings are discarded (expected warning in docker compose up).
The container is accessible on ghost VM's host IP directly.

### nginx proxy on ghost VM
Script at `/usr/local/bin/update-oh-proxy.sh` proxies fixed port 40000 to the
current oh-agent-server port. Run after starting a new conversation:
```bash
sudo bash /usr/local/bin/update-oh-proxy.sh
```

---

## MCP Connection Failure (Resolved)

Early sessions had every message fail with:
```
MCPError: MCP Connection Failure
httpx.ConnectError: [SSL] record layer failure
```
Caused by `WEB_HOST` env var being set, which caused the agent-server to attempt
SSL connections back to OpenHands. Fixed by removing `WEB_HOST` from docker-compose.

---

## Blackwell Maintenance

### GPU Clock and Power Limits (Vast.ai Customer Active)
With a Vast customer running on the 5090, set limits to reduce noise/heat:

```bash
# Limit clock to 2500MHz max
sudo nvidia-smi -lgc 0,2500 -i 0

# Limit power to 300W (from ~600W TDP)
sudo nvidia-smi -pl 300 -i 0
```

These are runtime settings — cleared on reboot. Re-apply after reboot if needed.
The 5090 runs at ~2040MHz under load with these limits, which is significantly
quieter than unconstrained operation.

### SSH Slowness Fix
Added `UseDNS no` to `/etc/ssh/sshd_config` to prevent reverse DNS lookup on
SSH connect. Applied with `sudo kill -HUP $(pgrep -x sshd | head -1)`.

### systemd D-Bus Failure
After the high-load xmrig incident (load avg 20+), systemd's D-Bus activation
socket became permanently stuck:
```
Failed to activate service 'org.freedesktop.systemd1': timed out (service_start_timeout=25000ms)
```
This affected: `systemctl`, `systemd reload/restart`, `sudo reboot`, all service
management. D-Bus daemon was running but not responding to systemd activation.

**Attempted fixes that failed:**
- `systemctl daemon-reexec`
- `sudo kill -HUP $(pgrep -x dbus-daemon)`
- `sudo reboot` (timed out)

**Working fix:** sysrq hard reboot:
```bash
sudo bash -c 'echo b > /proc/sysrq-trigger'
```

**Post-reboot:** All services came up correctly via systemd and post-panic-recovery.service.

### xmrig-qrl Manual Start (during systemd failure)
When systemd was broken, started xmrig directly:
```bash
nohup sudo /home/rich-rob/xmrig-6.25.0/xmrig \
  --config /home/rich-rob/xmrig-6.25.0/config.json > /tmp/xmrig.log 2>&1 &
```
After reboot, xmrig-qrl.service started automatically via systemd as normal.

### Docker Hub Image Rename
Vast.ai worker image renamed:
- Old: `itsthateasymate/ppal-worker:latest`
- New: `itsthateasymate/psis-worker:latest`

Push commands:
```bash
docker pull itsthateasymate/ppal-worker:latest
docker tag itsthateasymate/ppal-worker:latest itsthateasymate/psis-worker:latest
docker push itsthateasymate/psis-worker:latest
```
Note: Update Vast.ai template in web console to use new image name.
Reference doc: `/home/rich-rob/bootstrap-agent/DOCKERHUB_RENAME_NOTE.md`

---

## Post-Reboot Verification (2026-07-04)

```
systemctl status xmrig-qrl.service   → active (running)
cat /sys/kernel/kexec_crash_loaded   → 1
sudo virsh domstate ghost-vm-new     → running
systemd                              → fully recovered
```

post-panic-recovery.service fired correctly and sent clean boot ntfy notification.

---

## Current Status of Bootstrap Agent Items

| Item | Status |
|------|--------|
| qwen2.5-coder:14b in ghost VM ollama | ✅ Done |
| qwen3:14b in ghost VM ollama | ✅ Done (active model) |
| NFS mounted on ghost VM | ✅ Done |
| OpenHands pointed at ollama | ✅ Done |
| OpenHands WebSocket accessible from Mac | ⚠️ Works via oh-tunnel.sh, requires port tunnel each session |
| OpenHands agent can run tasks | ✅ Confirmed working with qwen3:14b |
| Agent GitHub PAT | ❌ Not done |

---

## Remaining Known Issues

**oh-agent-server random ports** — OpenHands 1.7 always uses random ports for the
agent-server container. No env var fixes this in 1.7. Workaround: `~/oh-tunnel.sh`
on Mac. Long-term fix: upgrade OpenHands when a version with fixed port config is
available, or set up a proper reverse proxy on blackwell that covers the port range.

**OH settings don't persist** — LLM model and base URL must be re-entered in the
OpenHands UI after container recreation. The openhands-state volume is empty.
Consider setting these via docker-compose env vars in a way the UI respects.

**nginx on blackwell not in systemd** — Started manually during systemd failure.
Add to systemd properly:
```bash
sudo systemctl enable nginx
sudo systemctl start nginx
```
