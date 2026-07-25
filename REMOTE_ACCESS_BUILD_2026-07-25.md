# Remote Access Build — Tailscale + HTTPS + Passkey Gateway
*Written 2026-07-25. Executes the build teed up in
`PROJECT_HANDOFF_2026-07-23_remote-access.md`. User is already traveling, so this
is ordered stopgap-first: **plain Tailscale restores recovery access in minutes**,
and the HTTPS/passkey layers stack on top without blocking that.*

Confidence markers follow the project convention:
**[solid]** — verified here or structurally certain.
**[single-source]** — one observation; believe but don't lean on.
**[open]** — unresolved / needs on-box confirmation.

Ships with `authgate.py` (the passkey reverse-proxy gateway), which was
smoke-tested against `webauthn` 3.0.0 + Flask 3.1 before this doc was written:
unauthenticated API → 401, browser nav → passkey login, both WebAuthn ceremonies
generate valid options (user-verification `required`). The verify path needs a
real authenticator and is confirmed on-box in Step 3.6.

---

## 0. The one thing to do first (recovery access, ~10 min) — **[solid]**

You do **not** need HTTPS or passkeys to recover blackwell from the road. Plain
Tailscale gives you a private, encrypted path to the dashboard immediately. Do
this before anything else so the actual priority — recovering blackwell while
unattended — is covered even if the rest of this build stalls.

On **blackwell**:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh          # --ssh lets you shell in over the tailnet later
tailscale ip -4                  # note the 100.x.y.z address
```
On the **Mac** and **phone**: install the Tailscale app, sign in to the **same
tailnet**, done.

Now, from the road:
```
http://100.x.y.z:8080            # the dashboard, over the tailnet, no port-forward
```
or, with MagicDNS on, `http://blackwell-node-01:8080`.

That's recovery access restored. It's HTTP and it's protected only by "you're an
enrolled device on my tailnet" — which is already a real boundary (no public
exposure, consistent with the project's no-port-forward rule). The passkey layer
below upgrades this from *device-on-tailnet* to *device-on-tailnet **and**
biometric*, which matters because this dashboard can wipe disks, cap GPU power,
and stop/start Vast rentals.

> **Prereq for everything HTTPS below:** in the Tailscale admin console enable
> **HTTPS Certificates** (and **MagicDNS**). Without it `tailscale serve --https`
> and `tailscale cert` can't provision a Let's Encrypt cert. One toggle, tailnet-wide.

---

## 1. HTTPS on the tailnet hostname (~5 min) — **[solid]**

`tailscale serve` acts as a reverse proxy that terminates TLS with an
auto-provisioned Let's Encrypt cert for your tailnet DNS name — no cert files to
manage. Syntax confirmed current (Jan 2026 docs; serve/funnel CLI stabilized in
1.52, `--bg` = persistent background, auto-resumes on reboot).

**Temporary — prove HTTPS works against the dashboard directly:**
```bash
sudo tailscale serve --bg --https=443 8080
tailscale serve status
```
Gives you `https://blackwell-node-01.<tailnet>.ts.net` → dashboard, valid cert,
no browser warning. Good enough as a second stopgap.

**But don't leave it pointed at 8080.** The passkey gateway (Step 3) must sit in
the path, so in Step 3.5 you'll repoint serve at the gateway on **8081**. If you
ran the line above, clear it first: `sudo tailscale serve --https=443 off`.

Find your exact tailnet hostname now — you need it for the gateway config:
```bash
tailscale status --json | grep -i dnsname     # e.g. blackwell-node-01.tail1234.ts.net
```

---

## 2. Why a separate gateway, not edits to `dashboard.py` — **[solid]**

The handoff is explicit that `dashboard.py`'s `TEMPLATE` is a fragile
non-raw triple-quoted string where a single vs double backslash silently breaks
served JS. Threading a full WebAuthn ceremony through that file is exactly the
kind of edit that bites. So instead:

```
  Mac / phone ──HTTPS──▶ tailscale serve :443 ──▶ authgate.py :8081 ──▶ dashboard.py :8080
  (tailnet only)          (TLS termination)        (passkey gate)         (unchanged)
```

- `dashboard.py` is **never modified**. It keeps running on 8080.
- `authgate.py` owns only `/__auth/*`; every other path is transparently proxied
  to the dashboard **once a valid passkey session cookie is present**.
- Only the gateway is exposed via serve. This is defence in depth:
  **factor 1** = enrolled tailnet device (Tailscale), **factor 2** = registered
  passkey with biometric user-verification.

**Bypass note — decide your LAN posture:**
- If `dashboard.py` stays bound to `0.0.0.0:8080`, anyone already on your **LAN**
  reaches it directly, unauthenticated (today's behavior — the handoff accepts LAN
  trust). Remote/tailnet traffic still must pass the passkey.
- To force *everyone*, including LAN, through the passkey, bind the dashboard to
  `127.0.0.1:8080` so only the gateway (and local shell) can reach it. Recommended
  once you've confirmed the gateway works. This is a one-line change wherever
  `dashboard.py` calls `.run(...)` / binds its socket.

---

## 3. The passkey gateway

### 3.1 — Install (~5 min) — **[solid]**
```bash
sudo mkdir -p /opt/authgate && sudo cp authgate.py /opt/authgate/
python3 -m venv /opt/authgate/venv
/opt/authgate/venv/bin/pip install flask requests webauthn
```
Package versions this was written against: `webauthn==3.0.0`, `flask==3.1.x`,
`requests`. The four WebAuthn methods used (`generate_registration_options`,
`verify_registration_response`, `generate_authentication_options`,
`verify_authentication_response`) are the stable public surface.

### 3.2 — Configure — **[solid]**
The gateway reads config from the environment. Put it in
`/opt/authgate/authgate.env` (mode 600) — **`RP_ID` / `ORIGIN` must be the exact
tailnet hostname the browser connects to**, or WebAuthn silently refuses:
```ini
AUTHGATE_RP_ID=blackwell-node-01.tail1234.ts.net
AUTHGATE_ORIGIN=https://blackwell-node-01.tail1234.ts.net
AUTHGATE_RP_NAME=Blackwell Dashboard
AUTHGATE_USER=rich-rob
AUTHGATE_UPSTREAM=http://127.0.0.1:8080
AUTHGATE_HOST=127.0.0.1
AUTHGATE_PORT=8081
AUTHGATE_STATE=/opt/authgate/state
AUTHGATE_SESSION_TTL=28800
```
State (signing secret, registered credentials, active enrolment token) lives in
`AUTHGATE_STATE`, created 0600 on first run.

### 3.3 — The enrolment chicken-and-egg (you're already remote) — **[solid]**
You can't register a passkey behind an auth wall that has no passkeys yet, and
you may no longer be on the LAN. Solution: a **one-time, 10-minute enrolment
token** minted from a shell on blackwell — and you already have a safe shell
channel (Tailscale SSH from Step 0, or your existing `id_ed25519` key):

```bash
ssh rich-rob@blackwell-node-01      # over the tailnet
cd /opt/authgate
sudo AUTHGATE_STATE=/opt/authgate/state \
     ./venv/bin/python authgate.py --enroll
```
It prints:
```
https://blackwell-node-01.tail1234.ts.net/__auth/register?token=<one-time>
```
The token is single-use and self-destructs on the first successful registration.
Because minting it requires shell access (tailnet + SSH key), the enrolment
channel is as trusted as the recovery channel — no open registration endpoint.

### 3.4 — Run it as a service — **[solid]**
`/etc/systemd/system/authgate.service`:
```ini
[Unit]
Description=Passkey auth gateway for blackwell dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=rich-rob
EnvironmentFile=/opt/authgate/authgate.env
ExecStart=/opt/authgate/venv/bin/python /opt/authgate/authgate.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now authgate
systemctl is-active authgate
```
> **Crash-loop trap (same lesson as the Sulphur build):** don't leave a manual
> `python authgate.py` running while the unit is enabled — both will fight for
> 8081 and `Restart=on-failure` will respawn every 3s. One owner.

### 3.5 — Put the gateway in the path — **[solid]**
```bash
sudo tailscale serve --https=443 off          # clear any Step-1 mapping to 8080
sudo tailscale serve --bg --https=443 8081     # HTTPS 443 -> gateway 8081
tailscale serve status                         # confirm it points at 127.0.0.1:8081
```

### 3.6 — Enrol, then verify the full loop — **[open → solid on-box]**
1. Mint the token (3.3), open the URL **on the Mac**, click *Create passkey* →
   Touch ID. On success the page says enrolled and the token is burned.
2. Register the **phone** too — mint a second token, open on the phone. Two
   passkeys = you're not locked out if one device is lost.
3. Go to `https://blackwell-node-01.tail1234.ts.net/` → you should be bounced to
   `/__auth/login` → *Unlock* → Touch ID → the real dashboard loads.
4. Confirm an unauthenticated API call is refused: from a fresh private window,
   `curl -k https://…/status` should 401, not return dashboard data.

The generate-side of both ceremonies and the 401/redirect gating are already
verified. Step 3.6 is what confirms `verify_registration_response` /
`verify_authentication_response` against a real Apple authenticator on this
hostname — the one thing a sandbox can't exercise.

### 3.7 — (Recommended) close the LAN bypass — **[solid]**
Once 3.6 passes, bind `dashboard.py` to `127.0.0.1:8080` so the passkey is
unavoidable for everyone, then `sudo systemctl restart` whatever runs the
dashboard. Skip this only if you deliberately want unauthenticated LAN access.

---

## 4. Verification checklist

| Check | Command / action | Expect |
|---|---|---|
| Tailscale up, 3 devices | `tailscale status` | blackwell + Mac + phone online |
| HTTPS cert valid | open `https://…ts.net/` on Mac | no cert warning |
| Serve points at gateway | `tailscale serve status` | `/ → proxy http://127.0.0.1:8081` |
| Gateway alive | `systemctl is-active authgate` | `active` |
| Unauth API blocked | `curl -k https://…/status` (no cookie) | `401` |
| Passkey unlock | visit `/`, Touch ID | dashboard loads |
| Counter persists | unlock twice | no clone-warning; `sign_count` rising in `state/credentials.json` |
| Survives reboot | `sudo reboot`, retry | serve + authgate auto-resume |

---

## 5. Rollback / lockout recovery — **[solid]**

Nothing here can lock you out of *recovery*, because the SSH-over-tailnet channel
is independent of the passkey layer.

- **Locked out of the dashboard (lost/failed passkey):** SSH in, mint a fresh
  enrolment token (3.3), register again. Or drop the wall entirely:
  `sudo tailscale serve --bg --https=443 8080` (back to Step-1 HTTPS-only), or
  `sudo tailscale serve --https=443 off` and use `http://100.x.y.z:8080`.
- **Gateway misbehaving:** `sudo systemctl stop authgate` and repoint serve at
  8080. Dashboard is untouched, so it keeps working.
- **Wrong RP_ID / origin (passkey prompt errors):** fix `authgate.env` to the
  exact `tailscale status --json` dnsname, `systemctl restart authgate`,
  re-enrol (a passkey is bound to the RP ID it was created under).

---

## 6. Security notes & honest caveats

- **[solid]** Session cookie is HMAC-signed (persistent per-host secret),
  `Secure` + `HttpOnly` + `SameSite=Strict`, 8h TTL. It is a bearer token for its
  lifetime — a stolen cookie works until expiry. Acceptable for single-user + short
  TTL; shorten `AUTHGATE_SESSION_TTL` if you want tighter.
- **[solid]** Signature-counter replay/clone detection is on
  (`credential_current_sign_count` checked and advanced each login).
- **[open]** **No step-up auth for destructive actions.** A live session can wipe
  free space or cap GPU power without re-prompting the passkey. Adding a
  "re-verify passkey for this action" gate would require `dashboard.py`
  cooperation on the specific destructive endpoints — deferred, noted as a
  follow-up. Given the dashboard's blast radius (disk wipe, Vast control), worth
  doing eventually.
- **[solid]** Keep the dashboard off Tailscale **Funnel**. Funnel is public
  internet; Serve is tailnet-only. This build uses Serve exclusively — consistent
  with the project's no-public-exposure rule.
- **[single-source]** Tailscale Serve also injects identity headers
  (`Tailscale-User-Login`) for tailnet traffic. The gateway doesn't rely on them,
  but you could add a header check as a third belt to pin sessions to one tailnet
  identity. Only safe because the upstream listens on localhost (per Tailscale's
  own guidance — otherwise headers are spoofable).

---

## 7. Open items — carried forward + new

Merges the handoff's still-open items with what this build leaves.

| # | Item | Status |
|---|---|---|
| 3 | Sulphur-2 audio path (`avcodec_send_frame` crash) | ⏭️ Still skipped |
| 4 | vast-watcher self-monitoring (silent crash-loop risk) | ⏳ Still deferred to always-on hardware. **Watch for it while traveling:** `journalctl -u vast-watcher -n 30`, look for repeating `Cycle error:` |
| 6 | Agent GitHub PAT (oldest open item in the project) | ❌ Still not done — unrelated to this build, but it's the last bootstrap blocker |
| 7 | Peak VRAM at full 1366×768×241 Sulphur-2 | ⏳ Still deferred (customer-dependent) |
| RA-1 | Tailscale + HTTPS + passkey gateway | ✅ **This build** (verify 3.6 on-box) |
| RA-2 | Enrol a **second** passkey (phone) so a lost device ≠ lockout | ◻️ Do during 3.6 |
| RA-3 | Bind dashboard to `127.0.0.1` to close LAN bypass | ◻️ After 3.6 passes (3.7) |
| RA-4 | Step-up passkey re-verify on destructive dashboard actions | ◻️ Future — needs `dashboard.py` cooperation |
| RA-5 | Back up `/opt/authgate/state` (secret + credentials) to NFS `/mnt/data/backups/` | ◻️ Else a state loss forces re-enrol |

---

## 8. Note for the design doc — a small inconsistency to reconcile

The dashboard port drifted: `AI_AGENT_SYSTEM_DESIGN_v6` lists the Human Control
Dashboard on **:8001** and API on **:8000**; the 2026-07-23 handoff and this build
use **:8080** for the operational dashboard that actually controls mining / GPU /
Vast / Sulphur. Not a blocker — the handoff is the live source of truth for the
box — but worth a one-line reconciliation in v7 so a future session doesn't wire
the gateway to the wrong port. This build assumes **:8080** per the handoff.

---

*Application/remote-access layer. Recovery posture (post-panic-recovery,
blackwell-watchdog, vast-watcher, kdump) is unchanged and remains the actual
guardian while unattended — this build only adds a safe, private, biometric-gated
way to reach the controls. Ships `authgate.py` alongside.*
