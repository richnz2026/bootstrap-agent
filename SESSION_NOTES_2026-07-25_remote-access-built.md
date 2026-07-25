# Session Notes — Remote Access Built & Committed
*2026-07-25, after midnight. Executed `REMOTE_ACCESS_BUILD_2026-07-25.md` end to
end. Tailscale + HTTPS + passkey gateway are live on blackwell; Mac + phone both
enrolled. This is the "what actually happened" record — the build doc is the
how-to, this is the state-of-the-box.*

---

## Done and verified tonight

| Thing | State | Verified how |
|---|---|---|
| Git: remote-access build committed | ✅ | pushed `aee0cbc` to `origin/bootstrap` |
| Git: vfio-watchdog VM-name fix | ✅ | `ad47d9d` — `mining-ai-vm` → `ghost-vm-new`, **and service restarted** so it's live, not just committed |
| Git: `.bak` gitignored | ✅ | `fc20014` |
| Tailscale on blackwell | ✅ | `tailscale up --ssh`, authenticated, IP `100.100.199.84` |
| Tailscale on Mac | ✅ | app connected, network extension approved |
| Tailscale on phone | ✅ | connected (enrolled passkey through it) |
| MagicDNS + HTTPS Certificates | ✅ | admin console; `tailscale serve` provisioned a real cert |
| HTTPS on tailnet hostname | ✅ | `https://blackwell-node-01.tail696aec.ts.net/` → padlock, no warning |
| authgate.py gateway service | ✅ | `authgate.service` active, `/status` → 401 (correct) |
| serve → gateway (not dashboard) | ✅ | `serve status`: `/ → proxy http://127.0.0.1:8081` |
| **Passkey on Mac (Touch ID)** | ✅ | enrolled + **full Unlock→Touch ID→dashboard round trip confirmed** |
| **Passkey on phone** | ✅ | enrolled via phone browser |

**Bottom line: the dashboard is now reachable remotely, privately (tailnet-only,
no public exposure), and gated behind a passkey + biometric. That's the build.**

---

## The hostname / key facts (you'll need these from the road)

- **Tailnet name:** `tail696aec.ts.net`
- **blackwell full hostname:** `blackwell-node-01.tail696aec.ts.net`
- **blackwell tailnet IP:** `100.100.199.84` (stable — works from anywhere)
- **blackwell LAN IP:** `192.168.50.51` (home only)
- **Dashboard (passkey-gated):** `https://blackwell-node-01.tail696aec.ts.net/`
- **Gateway:** `authgate.service`, listens `127.0.0.1:8081`, proxies to dashboard `:8080`
- **Gateway config:** `/opt/authgate/authgate.env`
- **Gateway state (secret + passkeys):** `/opt/authgate/state/`

---

## Recovery access while away — the two independent paths

1. **SSH (the recovery shell — for *fixing*):**
   ```
   ssh rich-rob@100.100.199.84
   ```
   From the **Mac Terminal**. ⚠️ **NOT yet tested from the Mac this session** —
   see open items. Uses Tailscale SSH (enabled via `--ssh`).

2. **Dashboard (the buttons — for *operating*):**
   `https://blackwell-node-01.tail696aec.ts.net/` → Unlock → Touch ID. ✅ tested.

These are independent by design: if the gateway/dashboard breaks, SSH still gets
you in to fix it. The passkey layer can fail without locking you out of recovery.

**Hard limit (unchanged, accepted):** Tailscale can't reach a machine that's off.
A crash+reboot self-heals via `post-panic-recovery.service`; the gaming PC's
`blackwell-watchdog` ntfy's you if blackwell drops. A hard power-off still needs
someone physically there — the same "no always-on secondary hardware" gap that's
open item #4 in the prior handoff.

---

## Open / not done — carry to next session

| # | Item | Why it matters |
|---|---|---|
| **RA-A** | **Test `ssh rich-rob@100.100.199.84` from the Mac** | The single load-bearing pre-travel check. Dashboard is proven; the *recovery shell* is not. **Do this before leaving.** Should "just work" from a real terminal (iOS clients struggle; Mac shouldn't). |
| RA-B | Phone SSH | Parked tonight. Tailscale-SSH + iOS = "password → failed to open session" (approval-step issue). Fix later via **Ed25519 key in Termius** added to blackwell `~/.ssh/authorized_keys` — bypasses the approval dance. Mosh not needed. Backup-to-a-backup; Mac SSH is primary. |
| RA-C | Back up gateway state to NFS | Without it, disk loss = re-enrol both passkeys from scratch. `sudo cp -a /opt/authgate/state /opt/authgate/authgate.env /mnt/data/backups/authgate/` |
| RA-D | LAN bypass decision | Dashboard still on `0.0.0.0:8080` → LAN reaches it *without* passkey (matches current LAN-trust posture, fine to leave). To force passkey even on LAN: bind dashboard to `127.0.0.1:8080` + restart. |
| RA-E | `authgate.py --enroll` prints `example.ts.net` when run under `sudo` | Cosmetic but a real foot-gun (cost us ~20 min tonight — "invalid token" was actually the wrong *domain* in the printed URL, token was fine). Fix: run `--enroll` as `rich-rob` with `authgate.env` sourced, OR patch the script to read `AUTHGATE_ORIGIN` / warn on the placeholder default. |
| RA-F | Step-up passkey on destructive dashboard actions | Live session can wipe disk / cap GPU without re-auth. Needs `dashboard.py` cooperation. Deferred. |
| — | Agent GitHub PAT | Still the oldest open item in the whole project. Untouched tonight. |

---

## Gotchas hit tonight (so they aren't re-learned)

- **Files created in a Claude session live in the chat, not on blackwell.** Six
  rounds of "no such file" / no-op commits were just: the doc + `authgate.py` had
  to be *downloaded to the Mac* then `scp`'d over. Don't assume a written file is
  on the box.
- **`git add` and `git commit` must actually both run.** Several no-op commits
  happened because only `commit` ran with nothing staged. **Always `git status`
  and see files under "Changes to be committed" before committing.**
- **`blackwell-node-01` doesn't resolve from the Mac** without MagicDNS/config
  (flagged in the Sulphur doc too). Use `100.100.199.84`, or add an `~/.ssh/config`
  `Host blackwell` entry pointing at the **tailnet IP** (works home + away).
- **HTTPS cert is for the hostname, not the IP.** `https://100.x` or `https://192.x`
  will always warn "not safe"; `https://blackwell-node-01.tail696aec.ts.net`
  gives the padlock. WebAuthn needs the clean padlock — "proceed anyway" won't do.
- **`sudo` strips the environment.** `sudo ... --enroll` lost `AUTHGATE_ORIGIN`
  (→ `example.ts.net` in the URL) and can write `state/` root-owned so the
  `rich-rob` service can't read it. Run `--enroll` as `rich-rob`.
- **"active (running)" ≠ doing its job** — third time this project has bitten
  (vast-watcher crash-loop, watchdog holding stale VM name, and tonight a stale
  `000` curl right after service start). Restart + check the journal after editing
  any long-running service; a bash service reads its vars once at startup.
- **Don't paste `https://` URLs into blackwell's shell** — it tries to run them.
  URLs go in the *browser*.

---

## Pre-flight checklist before you travel

1. ◻️ **`ssh rich-rob@100.100.199.84` from the Mac drops you at a blackwell prompt** ← the one that matters (RA-A)
2. ✅ Dashboard: `https://blackwell-node-01.tail696aec.ts.net/` → Touch ID → loads
3. ✅ Both passkeys enrolled (Mac + phone)
4. ◻️ (nice-to-have) Back up gateway state to NFS (RA-C)
5. ◻️ (nice-to-have) Phone SSH via Termius key (RA-B)

Items 2–3 are done. **Item 1 is the last real gap** — one command, tested from
the Mac, and you're genuinely covered.

---

*Recovery posture (post-panic-recovery, blackwell-watchdog, vast-watcher, kdump)
is unchanged and remains the real guardian while unattended. Tonight added a
private, biometric-gated way to reach the controls — it does not replace the
autonomous recovery, it sits alongside it.*
