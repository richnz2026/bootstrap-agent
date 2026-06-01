#!/usr/bin/env python3
"""
blackwell-node-01 Dashboard · v6 · Resource-Safe Edition
─────────────────────────────────────────────────────────
SAFEGUARDS:
  • All heavy fetchers run in BACKGROUND THREADS — never on HTTP request path
  • Staggered intervals prevent thundering-herd at startup
  • CPU/RAM thresholds auto-pause non-essential fetches
  • Chat: rate-limited, token-capped, rolling context window, idle timeout
  • Emergency KILL switch halts chat + non-essential fetches
  • /api/stats returns cached JSON instantly (no subprocess spawning)
  • AJAX polling replaces full meta-refresh page reloads
"""

import subprocess, re, json, time, threading, os, logging
from collections import deque
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template_string, request, jsonify
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dashboard")

app    = Flask(__name__)
from image_routes import image_bp
app.register_blueprint(image_bp)
NZDT   = timezone(timedelta(hours=12))

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION — tweak these, nothing else needs changing
# ═══════════════════════════════════════════════════════════════
CFG = {
    # Fetch intervals (seconds) — only in background thread
    "interval_gpu":    30,
    "interval_sys":    30,
    "interval_kvm":    30,
    "interval_xmrig":  30,
    "interval_docker": 20,
    "interval_vastai": 120,   # Vast.ai CLI is slow; 2min is fine
    "interval_price_charts": 300,  # CoinGecko 30d charts, 5min

    # Resource thresholds — pause non-essential fetches above these
    "cpu_warn":   65,   # % — amber alert
    "cpu_crit":   82,   # % — red alert, pause Vast.ai + KVM fetches
    "ram_warn":   75,   # %
    "ram_crit":   93,   # % — red alert (raised from 88 — 6GB free is safe)

    # Chat safeguards
    "chat_enabled":        True,
    "chat_max_tokens":     512,    # per response
    "chat_rate_msgs":      5,      # messages allowed per rate window
    "chat_rate_window_s":  60,     # rate window in seconds
    "chat_context_msgs":   12,     # rolling context window (messages kept)
    "chat_idle_pause_s":   300,    # pause chat if no message for 5 min
    "chat_model":          "claude-haiku-4-5-20251001",  # lightweight model
    "chat_api_timeout_s":  20,

    # Kill switch: when triggered, halts chat AND pauses all non-GPU fetches
    "kill_active": False,
}

# ═══════════════════════════════════════════════════════════════
# SHARED STATE  (protected by _lock)
# ═══════════════════════════════════════════════════════════════
_lock   = threading.Lock()
_state  = {
    "gpu":      {},
    "sys":      {},
    "kvm":      {},
    "xmrig":    {},
    "qrl":      {},
    "cfx":      {},
    "gaming":   {},
    "gaming_wallets": {},
    "top10": {},
    "price_charts": {},
    "fx": {"usd_nzd": 1.705},
    "mining": {},
    "kls":      {},
    "docker":   [],
    "vastai":   {},
    "alerts":   [],
    "updated":  {},   # key → epoch timestamp of last successful fetch
    "errors":   {},   # key → last error string
}

# XMRig 24hr rolling history — (timestamp, hashrate, cores)
_xmrig_history = deque(maxlen=2880)  # 2880 x 30s = 24hrs

# KVM lolMiner 24hr rolling history
_kvm_lol_history = deque(maxlen=2880)

# Chat state (separate lock to avoid contention)
_chat_lock    = threading.Lock()
_chat_history = deque(maxlen=CFG["chat_context_msgs"])
_chat_rate    = deque()          # timestamps of recent messages
_chat_last_ts = 0.0             # epoch of last chat message

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
def utc_to_nzdt(ts_str):
    ts_str = re.sub(r'(\.\d{6})\d+', r'\1', ts_str.strip())
    for fmt in ["%Y/%m/%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"]:
        try:
            dt = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(NZDT).strftime("%d %b %H:%M NZST")
        except: continue
    return ts_str

def parse_ts_utc(ts_str):
    ts_str = re.sub(r'(\.\d{6})\d+', r'\1', ts_str.strip())
    for fmt in ["%Y/%m/%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"]:
        try: return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except: continue
    return None

def mib_to_gb(s):
    try: return round(int(str(s).strip()) / 1024, 1)
    except: return None

def run(cmd, timeout=8):
    """Run a shell command safely. Returns stdout or '' on any failure."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return ""

def _set_state(key, value):
    with _lock:
        _state[key]           = value
        _state["updated"][key] = time.time()

def _set_error(key, msg):
    with _lock:
        _state["errors"][key] = msg

def get_cpu_pct():
    """Read cached CPU% — no lock, safe for reads."""
    return _state["sys"].get("cpu_pct", 0)

def resource_pressure():
    """Return 'ok', 'warn', or 'crit' based on CPU+RAM. No lock — reads only."""
    cpu = _state["sys"].get("cpu_pct", 0)
    ram = _state["sys"].get("mem_pct", 0)
    if cpu >= CFG["cpu_crit"] or ram >= CFG["ram_crit"]:
        return "crit"
    if cpu >= CFG["cpu_warn"] or ram >= CFG["ram_warn"]:
        return "warn"
    return "ok"

# ═══════════════════════════════════════════════════════════════
# FETCHERS  (called ONLY from background threads)
# ═══════════════════════════════════════════════════════════════
def fetch_gpu():
    out = run("nvidia-smi --query-gpu=temperature.gpu,power.draw,utilization.gpu,"
              "memory.used,memory.total,fan.speed,clocks_throttle_reasons.active "
              "--format=csv,noheader,nounits")
    if not out:
        _set_error("gpu", "nvidia-smi returned nothing"); return
    p = [x.strip() for x in out.split(",")]
    if len(p) < 7:
        _set_error("gpu", f"unexpected fields: {out[:80]}"); return
    mu, mt = int(p[3]), int(p[4])
    _set_state("gpu", {
        "temp": p[0], "power": p[1], "util": p[2],
        "mem_used": round(mu/1024, 1), "mem_total": round(mt/1024, 1),
        "mem_pct": int(mu/mt*100), "fan": p[5],
        "throttling": p[6].strip() != "0x0000000000000000",
    })

def _get_cpu_temp():
    try:
        return round(int(open('/sys/class/hwmon/hwmon4/temp1_input').read().strip())/1000, 1)
    except Exception:
        return None

def fetch_system():
    # Use /proc/stat for CPU% — much lighter than spawning top
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        fields = list(map(int, line.split()[1:]))
        idle  = fields[3] + fields[4]
        total = sum(fields)
        # We need two samples for accuracy; store previous
        prev = getattr(fetch_system, "_prev", None)
        fetch_system._prev = (total, idle)
        if prev:
            dt = total - prev[0]; di = idle - prev[1]
            cpu_pct = round((1 - di/dt)*100, 1) if dt else 0.0
        else:
            cpu_pct = 0.0
    except Exception as e:
        cpu_pct = 0.0

    mem_raw  = run("free -b | awk '/^Mem:/{print $2,$3,$4}'")
    swap_raw = run("free -b | awk '/^Swap:/{print $2,$3}'")

    # Net I/O
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()
        rx = tx = 0
        for line in lines[2:]:
            parts = line.split()
            iface = parts[0].rstrip(":")
            if iface not in ("lo",):
                rx += int(parts[1]); tx += int(parts[9])
        prev_net = getattr(fetch_system, "_prev_net", (rx, tx))
        fetch_system._prev_net = (rx, tx)
        net_rx = round((rx - prev_net[0])/1048576, 2)
        net_tx = round((tx - prev_net[1])/1048576, 2)
    except:
        net_rx = net_tx = 0
    mp = mem_raw.split()  if mem_raw  else []
    sp = swap_raw.split() if swap_raw else []
    def b2g(b):
        try: return round(int(b)/1073741824, 1)
        except: return 0
    mem_pct = int(int(mp[1])/int(mp[0])*100) if len(mp) > 1 else 0
    _set_state("sys", {
        "uptime":     run("uptime -p"),
        "load":       run(r"uptime | grep -oP 'load average: \K[^$]+'"),
        "cpu_cores":  run("nproc"),
        "cpu_pct":    cpu_pct,
        "mem_total":  b2g(mp[0]) if mp else 0,
        "mem_used":   b2g(mp[1]) if len(mp)>1 else 0,
        "mem_free":   b2g(mp[2]) if len(mp)>2 else 0,
        "mem_pct":    mem_pct,
        "swap_total": b2g(sp[0]) if sp else 0,
        "swap_used":  b2g(sp[1]) if len(sp)>1 else 0,
        "disk_used":  round(__import__('shutil').disk_usage('/').used/1073741824, 1),
        "disk_total": round(__import__('shutil').disk_usage('/').total/1073741824, 1),
        "disk_pct":   int(__import__('shutil').disk_usage('/').used/__import__('shutil').disk_usage('/').total*100),
        "net_rx_mb":  net_rx,
        "net_tx_mb":  net_tx,
    })
    _update_alerts(cpu_pct, mem_pct)

def _update_alerts(cpu_pct, mem_pct):
    alerts = []
    if cpu_pct >= CFG["cpu_crit"]:
        alerts.append({"level": "crit", "msg": f"CPU critical: {cpu_pct:.0f}%"})
    elif cpu_pct >= CFG["cpu_warn"]:
        alerts.append({"level": "warn", "msg": f"CPU elevated: {cpu_pct:.0f}%"})
    if mem_pct >= CFG["ram_crit"]:
        alerts.append({"level": "crit", "msg": f"RAM critical: {mem_pct}%"})
    elif mem_pct >= CFG["ram_warn"]:
        alerts.append({"level": "warn", "msg": f"RAM elevated: {mem_pct}%"})
    if CFG["kill_active"]:
        alerts.append({"level": "kill", "msg": "KILL SWITCH ACTIVE — all non-essential fetches halted"})
    with _lock:
        _state["alerts"] = alerts


def update_balance_history(coin, balance_val):
    """Store balance snapshots for growth chart."""
    import json as _j, time as _t, os as _os
    hist_file = _os.path.expanduser('~/mining_balance_history.json')
    try:
        hist = _j.loads(open(hist_file).read()) if _os.path.exists(hist_file) else {}
    except Exception:
        hist = {}
    if coin not in hist:
        hist[coin] = []
    now = int(_t.time())
    # Only add if value changed or >30 mins since last entry
    entries = hist[coin]
    if not entries or abs(entries[-1][1] - balance_val) > 0.0001 or now - entries[-1][0] > 1800:
        entries.append([now, balance_val])
        # Keep last 30 days
        cutoff = now - 30 * 86400
        hist[coin] = [e for e in entries if e[0] > cutoff]
        try:
            open(hist_file, 'w').write(_j.dumps(hist))
        except Exception:
            pass
    return hist.get(coin, [])



def update_value_history(coin, balance_val, price_usd, usd_to_nzd=1.0):
    """Store NZD value snapshots for growth chart."""
    import json as _j, time as _t, os as _os
    hist_file = _os.path.expanduser('~/mining_value_history.json')
    try:
        hist = _j.loads(open(hist_file).read()) if _os.path.exists(hist_file) else {}
    except Exception:
        hist = {}
    if coin not in hist:
        hist[coin] = []
    try:
        price = float(str(price_usd).replace('$','').replace(',','')) if price_usd and price_usd != '—' else 0
        nzd_val = round(balance_val * price * usd_to_nzd, 4)
    except:
        return hist.get(coin, [])
    if nzd_val <= 0:
        return hist.get(coin, [])
    now = int(_t.time())
    entries = hist[coin]
    if not entries or abs(entries[-1][1] - nzd_val) > 0.001 or now - entries[-1][0] > 1800:
        entries.append([now, nzd_val])
        cutoff = now - 30 * 86400
        hist[coin] = [e for e in entries if e[0] > cutoff]
        try:
            open(hist_file, 'w').write(_j.dumps(hist))
        except Exception:
            pass
    return hist.get(coin, [])

def fetch_mining():
    """Fetch unified mining data for all coins."""
    import urllib.request as _ur, json as _j, time as _t

    data = {}

    # ── QRL (from XMRig local API) ────────────────────────────────────────────
    try:
        with _ur.urlopen('http://localhost:18080/2/summary', timeout=5) as r:
            xd = _j.loads(r.read())
        hr = xd.get('hashrate', {}).get('total', [0, 0, 0])
        conn = xd.get('connection', {})
        qrl_state = dict(_state.get('qrl', {}))
        _qb = qrl_state.get('balance', 0)
        try:
            bal = float(_qb) if _qb and str(_qb) not in ('—', '', 'None') else 0
        except (ValueError, TypeError):
            bal = 0
        history = update_balance_history('QRL', bal)
        _fx = dict(_state.get('fx', {}))
        _qrl_price_usd = dict(_state.get('price_charts', {})).get('qrl', [[0,0]])[-1][1] if _state.get('price_charts', {}).get('qrl') else 0
        update_value_history('QRL', bal, _qrl_price_usd, _fx.get('usd_nzd', 1.705))
        data['QRL'] = {
            'balance': qrl_state.get('balance', '—'),
            'nzd': qrl_state.get('nzd', '—'),
            'usd': qrl_state.get('usd', '—'),
            'price_nzd': qrl_state.get('nzd_price', '—'),
            'hashrate': f"{hr[0]:.1f} H/s" if hr[0] else '—',
            'hashrate_60s': f"{hr[1]:.1f} H/s" if len(hr) > 1 and hr[1] else '—',
            'accepted': conn.get('accepted', 0),
            'rejected': conn.get('rejected', 0),
            'pool': conn.get('pool', '—'),
            'uptime': xd.get('connection', {}).get('uptime', 0),
            'machines': ['BLACKWELL'],
            'running': True,
            'balance_history': history[-60:],
            'value_history': update_value_history('QRL', bal, _qrl_price_usd, _fx.get('usd_nzd', 1.705))[-60:],
        }
    except Exception as e:
        data['QRL'] = {'running': False, 'error': str(e), 'machines': ['BLACKWELL']}

    # ── PRL (AlphaPool) ───────────────────────────────────────────────────────
    try:
        prl_addr = ""
        try:
            for _line in (Path.home() / ".mining_keys").read_text().splitlines():
                if _line.startswith("PRL_WALLET="):
                    prl_addr = _line.split("=",1)[1].strip()
        except: pass
        if not prl_addr: prl_addr = "prl1key"
        with _ur.urlopen(_ur.Request(
            f"https://pearl.alphapool.tech/api/miner/{prl_addr}",
            headers={"User-Agent":"Mozilla/5.0"}), timeout=8) as r:
            pd = _j.loads(r.read())
        bal = pd.get('balance_prl', 0)
        history = update_balance_history('PRL', bal)
        _fx2 = dict(_state.get('fx', {}))
        _prl_price = pd.get('price_usd', 0) or 0.50  # default 0.50 USD if no price source
        update_value_history('PRL', bal, _prl_price, _fx2.get('usd_nzd', 1.705))
        import time as _prl_time
        _now_ts = _prl_time.time()
        worker_data = pd.get('workers', [])
        workers = [w['name'] for w in worker_data]
        # Per-worker status: 'active', 'stale' (offline <30min), 'inactive' (offline >30min)
        worker_status = {}
        for w in worker_data:
            name = w.get('name', '')
            online = w.get('online', False)
            last_ts = w.get('time', 0)
            age_mins = (_now_ts - last_ts) / 60 if last_ts else 9999
            hr = w.get("hashrate", "0 H/s")
            has_hashrate = hr and hr not in ("0 H/s", "0H/s", "", "0")
            if online or has_hashrate:
                worker_status[name] = 'active'
            elif age_mins < 60:
                worker_status[name] = 'stale'
            else:
                worker_status[name] = 'inactive'
        machines = []
        if 'ghost-vm' in workers: machines.append('GHOST')
        if 'gaming-pc' in workers: machines.append('GAMING')
        # running = at least one worker active or stale
        any_active = any(v in ('active', 'stale') for v in worker_status.values())
        raw_series = pd.get('hashrate_series', [])
        if raw_series and isinstance(raw_series[0], dict):
            hr_series = [[e.get('time', e.get('t', 0)), e.get('hashrate', e.get('h', 0))] for e in raw_series]
        else:
            hr_series = [[e[0], e[1]] for e in raw_series if len(e) >= 2]
        data['PRL'] = {
            'balance': f"{bal:.4f} PRL",
            'balance_raw': bal,
            'paid': f"{pd.get('total_paid_prl', 0):.4f} PRL",
            'hashrate_1h': pd.get('estHash1h', '—'),
            'hashrate_24h': pd.get('estHash24h', '—'),
            'shares_24h': pd.get('shares24h', 0),
            'workers': workers,
            'worker_status': worker_status,
            'machines': machines or ['GHOST'],
            'running': any_active,
            'hashrate_series': hr_series[-24:],
            'balance_history': history[-60:],
            'value_history': update_value_history('PRL', bal, pd.get('price_usd', 0) or 0.50, dict(_state.get('fx',{})).get('usd_nzd',1.705))[-60:],
            'pool': 'sg1.alphapool.tech:5566',
            'fee': '5% PPLNS',
        }
    except Exception as e:
        import traceback
        import json as _pj
        _pval = []
        try:
            _ph = _pj.loads((Path.home()/'mining_value_history.json').read_text())
            _pval = _ph.get('PRL', [])[-60:]
        except: pass
        data['PRL'] = {'running': False, 'error': str(e), 'error_detail': traceback.format_exc()[-200:], 'machines': ['GHOST', 'GAMING'], 'value_history': _pval}
        log.warning(f"fetch_mining PRL error: {e}")

    # ── ERG ───────────────────────────────────────────────────────────────────
    try:
        gw = dict(_state.get('gaming_wallets', {}))
        bal_str = gw.get('erg_balance', '0')
        try:
            bal = float(bal_str) if bal_str else 0
        except (ValueError, TypeError):
            bal = 0
        history = update_balance_history('ERG', bal)
        kvm_lol = dict(_state.get('kvm', {}))
        gaming = dict(_state.get('gaming', {}))
        machines = []
        if kvm_lol.get('lol_algo', '').upper() in ('AUTOLYKOS', 'AUTOLYKOS2'): machines.append('GHOST')
        if gaming.get('algo', '').upper() in ('AUTOLYKOS2', 'ERG'): machines.append('GAMING')
        data['ERG'] = {
            'balance': f"{bal:.4f} ERG" if bal else '—',
            'balance_raw': bal,
            'nzd': gw.get('erg_nzd', '—'),
            'usd': gw.get('erg_usd', '—'),
            'price_usd': gw.get('erg_price_usd', '—'),
            'machines': machines or [],
            'running': len(machines) > 0,
            'pool': '46.4.102.169:1180',
            'balance_history': history[-60:],
        }
    except Exception as e:
        data['ERG'] = {'running': False, 'machines': []}

    # ── KLS ───────────────────────────────────────────────────────────────────
    try:
        kls_state = dict(_state.get('kls', {}))
        bal_str = kls_state.get('balance', '0')
        bal = float(str(bal_str).replace(' KLS','').strip()) if bal_str and bal_str not in ('—','') else 0
        history = update_balance_history('KLS', bal)
        kvm_lol = dict(_state.get('kvm', {}))
        running = kvm_lol.get('lol_algo', '').upper() in ('KARLSENHASH', 'KARLSENHASHV2', 'KLS')
        data['KLS'] = {
            'balance': kls_state.get('balance', '—'),
            'balance_raw': bal,
            'nzd': kls_state.get('nzd', '—'),
            'usd': kls_state.get('usd', '—'),
            'hashrate': kls_state.get('hashrate', '—'),
            'immature': kls_state.get('immature', '—'),
            'machines': ['GHOST'] if running else [],
            'running': running,
            'pool': 'pool.au.woolypooly.com:3132',
            'balance_history': history[-60:],
        }
    except Exception as e:
        data['KLS'] = {'running': False, 'machines': []}

    # ── IRON ──────────────────────────────────────────────────────────────────
    try:
        gw = dict(_state.get('gaming_wallets', {}))
        bal_str = gw.get('iron_balance', '0')
        bal = float(str(bal_str).replace(' IRON','').strip()) if bal_str and bal_str not in ('—','') else 0
        history = update_balance_history('IRON', bal)
        kvm_lol = dict(_state.get('kvm', {}))
        running = kvm_lol.get('lol_algo', '').upper() in ('FISHHASH', 'IRON')
        data['IRON'] = {
            'balance': gw.get('iron_balance', '—'),
            'balance_raw': bal,
            'nzd': gw.get('iron_nzd', '—'),
            'usd': gw.get('iron_usd', '—'),
            'price_usd': gw.get('iron_price_usd', '—'),
            'paid': gw.get('iron_paid', '—'),
            'hashrate_1h': gw.get('iron_hashrate', '—'),
            'machines': ['GHOST'] if running else [],
            'running': running,
            'pool': '5.9.111.187:443',
            'balance_history': history[-60:],
        }
    except Exception as e:
        data['IRON'] = {'running': False, 'machines': []}

    # ── NEXA ─────────────────────────────────────────────────────────────────────
    try:
        import subprocess as _sp
        nexa_wallet = ""
        keys_file = Path.home() / ".mining_keys"
        if keys_file.exists():
            for line in keys_file.read_text().splitlines():
                if line.startswith("NEXA_WALLET="):
                    nexa_wallet = line.split("=", 1)[1].strip()
        kvm_lol = dict(_state.get('kvm', {}))
        running = kvm_lol.get('lol_algo', '').upper() in ('NEXA', 'NEXAPOW')
        bal = 0
        # Try WoolyPooly API for authoritative pool balance
        # Wallet stored as "nexa:addr..." — strip prefix for API
        # API subdomain: api.woolypooly.com (woolypooly.com returns empty)
        wp_paid = 0
        wp_workers_online = 0
        try:
            import urllib.request as _ur5
            addr = nexa_wallet.replace('nexa:', '').strip()
            if addr:
                req5 = _ur5.Request(
                    f"https://api.woolypooly.com/api/nexa-1/accounts/{addr}",
                    headers={"User-Agent": "Mozilla/5.0"})
                with _ur5.urlopen(req5, timeout=8) as r5:
                    wp = json.loads(r5.read())
                stats = wp.get('stats', {})
                # balance is in whole NEXA (not satoshis)
                bal = float(stats.get('balance', 0) or 0)
                wp_paid = float(stats.get('paid', 0) or 0)
                wp_workers_online = int(wp.get('workersOnline', 0) or 0)
                if not running and wp_workers_online > 0:
                    running = True
        except Exception as e:
            log.debug(f"NEXA WoolyPooly fetch: {e}")
        # Fall back to KVM state if API gave nothing
        if bal == 0:
            bal_str = kvm_lol.get('nexa_balance', '0')
            try:
                bal = float(str(bal_str).replace(' NEXA', '').strip())                     if bal_str and bal_str not in ('—', '') else 0
            except Exception:
                bal = 0
        history = update_balance_history('NEXA', bal)
        _pc = _state.get('price_charts', {})
        _nexa_hist = _pc.get('nexa', [])
        _nexa_price = _nexa_hist[-1][1] if _nexa_hist else None
        data['NEXA'] = {
            'balance': f"{bal:.4f} NEXA" if bal else '—',
            'balance_raw': bal,
            'nzd': '—',
            'usd': f"${bal * _nexa_price:.4f}" if (bal and _nexa_price) else '—',
            'price_usd': f"${_nexa_price:.6f}" if _nexa_price else '—',
            'hashrate': kvm_lol.get('lol_hr', '—'),
            'machines': ['GHOST'] if running else [],
            'running': running,
            'pool': 'pool.au.woolypooly.com:3124',
            'paid': f"{wp_paid:.4f} NEXA" if wp_paid else '0 NEXA',
            'workers_online': wp_workers_online,
            'balance_history': history[-60:],
        }
    except Exception as e:
        data['NEXA'] = {'running': False, 'machines': []}

    # ── CFX ───────────────────────────────────────────────────────────────────
    try:
        cfx_state = dict(_state.get('cfx', {}))
        bal_str = cfx_state.get('balance', '0')
        bal = float(str(bal_str).replace(' CFX','').strip()) if bal_str and bal_str not in ('—','') else 0
        history = update_balance_history('CFX', bal)
        kvm_lol = dict(_state.get('kvm', {}))
        gaming = dict(_state.get('gaming', {}))
        machines = []
        if kvm_lol.get('lol_algo', '').upper() == 'OCTOPUS': machines.append('GHOST')
        if gaming.get('algo', '').upper() in ('OCTOPUS', 'CFX'): machines.append('GAMING')
        _cfx_pc = _state.get('price_charts', {}).get('cfx', [])
        _cfx_price = _cfx_pc[-1][1] if _cfx_pc else None
        data['CFX'] = {
            'balance': f"{bal:.4f} CFX" if bal else '—',
            'balance_raw': bal,
            'nzd': cfx_state.get('nzd', '—'),
            'usd': cfx_state.get('usd', '—'),
            'price_usd': cfx_state.get('usd_price') or (f"${_cfx_price:.4f}" if _cfx_price else '—'),
            'hashrate': gaming.get('hashrate', '—'),
            'machines': machines,
            'running': len(machines) > 0,
            'pool': 'cfx.f2pool.com:6800',
            'balance_history': history[-60:],
        }
    except Exception as e:
        data['CFX'] = {'running': False, 'machines': []}

    # ── FLUX ──────────────────────────────────────────────────────────────────
    try:
        flux_addr = "t1dEoyPJ4opjaGxpzFtVcYsPgfNBajw6U9U"
        with _ur.urlopen(_ur.Request(
            f"https://api.runonflux.io/explorer/balance?address={flux_addr}",
            headers={"User-Agent":"Mozilla/5.0"}), timeout=8) as r:
            fd = _j.loads(r.read())
        bal = float(fd.get('data', 0)) / 1e8  # satoshi to FLUX
        history = update_balance_history('FLUX', bal)
        gw = dict(_state.get('gaming_wallets', {}))
        gaming = dict(_state.get('gaming', {}))
        running = gaming.get('algo', '').upper() == 'FLUX'
        _flux_pc = _state.get('price_charts', {}).get('flux', [])
        _flux_price = _flux_pc[-1][1] if _flux_pc else None
        data['FLUX'] = {
            'balance': f"{bal:.4f} FLUX",
            'balance_raw': bal,
            'price_usd': f"${_flux_price:.4f}" if _flux_price else '—',
            'usd': f"${bal * _flux_price:.2f}" if (bal and _flux_price) else '—',
            'nzd': f"${bal * _flux_price * 1.705:.2f}" if (bal and _flux_price) else '—',
            'machines': ['GAMING'] if running else [],
            'running': running,
            'pool': 'us-flux.fluxpools.net:2001',
            'balance_history': history[-60:],
        }
    except Exception as e:
        data['FLUX'] = {'running': False, 'machines': []}

    # ── Enrich all coins with price from price_charts + qrl state ───────────
    pc = dict(_state.get('price_charts', {}))
    qrl_st = dict(_state.get('qrl', {}))
    PRICE_MAP = {
        'ERG':  ('erg',  pc.get('erg',  [])),
        'KLS':  ('kls',  pc.get('kls',  [])),
        'IRON': ('iron', pc.get('iron', [])),
        'CFX':  ('cfx',  pc.get('cfx',  [])),
        'FLUX': ('flux', pc.get('flux', [])),
    }
    for coin, (key, hist) in PRICE_MAP.items():
        if coin not in data:
            continue
        price_usd = hist[-1][1] if hist else None
        if price_usd and price_usd > 0:
            # Only set if not already populated
            if data[coin].get('price_usd') in (None, '—', '$0.000', '$0.0000'):
                data[coin]['price_usd'] = f"${price_usd:.4f}"
            bal_raw = data[coin].get('balance_raw', 0) or 0
            if bal_raw and data[coin].get('usd') in (None, '—'):
                data[coin]['usd'] = f"${bal_raw * price_usd:.2f}"
            if bal_raw and data[coin].get('nzd') in (None, '—'):
                # Approximate NZD — use 1.7x USD as fallback if no NZD price
                nzd_rate = dict(_state.get('fx', {})).get('usd_nzd', 1.705)
                data[coin]['nzd'] = f"${bal_raw * price_usd * nzd_rate:.2f}"
    # QRL — balance from qrl state, price from price_charts
    if 'QRL' in data:
        if data['QRL'].get('balance') in (None, '—'):
            data['QRL']['balance'] = qrl_st.get('balance', '—')
        _qrl_hist = pc.get('qrl', [])
        _qrl_price = _qrl_hist[-1][1] if _qrl_hist else None
        if _qrl_price:
            bal_raw = float(qrl_st.get('balance', 0) or 0)
            if data['QRL'].get('usd') in (None, '—'):
                data['QRL']['usd'] = f"${bal_raw * _qrl_price:.2f}" if bal_raw else '—'
            if data['QRL'].get('nzd') in (None, '—'):
                data['QRL']['nzd'] = f"${bal_raw * _qrl_price * 1.705:.2f}" if bal_raw else '—'
            if data['QRL'].get('price_usd') in (None, '—'):
                data['QRL']['price_usd'] = f"${_qrl_price:.4f}" 

    _set_state("mining", data)

def fetch_price_charts():
    """Fetch CoinGecko 30d price history for non-QRL mined coins.
    QRL already fetches its own history in fetch_qrl().
    Runs every 300s to stay within CoinGecko free-tier limits.
    """
    # CoinGecko IDs — verified May 2026
    # KLS: delisted/dead. NEXA: not on CoinGecko.
    # FLUX last — most likely to hit 429, extra delay before it
    # QRL included here so fetch_qrl doesn't need its own price call
    COIN_IDS = {
        'qrl':  'quantum-resistant-ledger',
        'erg':  'ergo',
        'iron': 'iron-fish',
        'cfx':  'conflux-token',
        'flux': 'flux',
    }
    EXTRA_DELAY = {'flux': 6}  # extra seconds before this coin
    import urllib.request as _ur3, time as _time3
    charts = {}
    for key, cg_id in COIN_IDS.items():
        try:
            url = (f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
                   f"?vs_currency=usd&days=30")
            req = _ur3.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with _ur3.urlopen(req, timeout=10) as r:
                hd = json.loads(r.read())
            prices = hd.get('prices', [])
            if prices:
                charts[key] = [[int(p[0] / 1000), round(p[1], 6)] for p in prices]
                log.info(f"fetch_price_charts {key}: {len(prices)} pts")
            else:
                log.warning(f"fetch_price_charts {key}: no prices in response")
            _time3.sleep(EXTRA_DELAY.get(key, 0) + 4)  # avoid CoinGecko rate limit
        except Exception as e:
            log.warning(f"fetch_price_charts {key}: {e}")
            _time3.sleep(6)  # back off on error
    with _lock:
        _state['price_charts'].update(charts)
        _state['updated']['price_charts'] = time.time()


def fetch_fx_rate():
    """Fetch daily USD/NZD rate from frankfurter.app."""
    try:
        import urllib.request as _ur_fx
        req = _ur_fx.Request(
            'https://api.frankfurter.app/latest?from=USD&to=NZD',
            headers={'User-Agent': 'Mozilla/5.0'})
        with _ur_fx.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())
        rate = float(d.get('rates', {}).get('NZD', 1.705))
        _set_state('fx', {'usd_nzd': rate})
        log.info(f"fetch_fx_rate: 1 USD = {rate} NZD")
    except Exception as e:
        log.warning(f"fetch_fx_rate: {e}")


def fetch_kvm():
    if CFG["kill_active"] or resource_pressure() == "crit":
        # Update state to show paused reason — don't leave panel stale
        with _lock:
            if _state.get("kvm"):
                _state["kvm"]["fetch_paused"] = True
                _state["kvm"]["fetch_paused_reason"] = "High memory pressure"
        return   # skip under pressure
    # Clear paused flag when running normally
    with _lock:
        if _state.get("kvm"):
            _state["kvm"]["fetch_paused"] = False

    state_out = run("sudo virsh domstate mining-ai-vm 2>/dev/null")
    stats = {"state": state_out.strip() or "unknown"}
    stats["running"] = (state_out.strip() in ("running", "paused"))

    if stats["running"]:
        domstats = run("sudo virsh domstats mining-ai-vm 2>/dev/null")
        ds = {}
        for line in domstats.split("\n"):
            if "=" in line:
                k, _, v = line.partition("=")
                ds[k.strip()] = v.strip()

        stats["vcpus"] = ds.get("vcpu.current", "8")
        balloon_max    = int(ds.get("balloon.maximum", 0))
        balloon_unused = int(ds.get("balloon.unused", 0))
        balloon_rss    = int(ds.get("balloon.rss", 0))
        if balloon_max > 0:
            mem_used_kb = balloon_max - balloon_unused
            stats.update({
                "mem_total_gb": round(balloon_max/1048576, 1),
                "mem_used_gb":  round(mem_used_kb/1048576, 1),
                "mem_pct":      int(mem_used_kb/balloon_max*100),
                "mem_rss_gb":   round(balloon_rss/1048576, 1),
            })

        rx = int(ds.get("net.0.rx.bytes", 0))
        tx = int(ds.get("net.0.tx.bytes", 0))
        stats["net_rx_mb"] = round(rx/1048576, 1)
        stats["net_tx_mb"] = round(tx/1048576, 1)

        blkinfo = run("sudo virsh domblkinfo mining-ai-vm vda 2>/dev/null")
        ba = bc = 0
        for line in blkinfo.split("\n"):
            if line.startswith("Allocation:"):
                ba = int(line.split(":")[1].strip())
            elif line.startswith("Capacity:"):
                bc = int(line.split(":")[1].strip())
        if bc > 0:
            stats.update({
                "disk_used_gb":  round(ba/1073741824, 1),
                "disk_total_gb": round(bc/1073741824, 1),
                "disk_pct":      int(ba/bc*100),
            })

        # RTX 5070 in VM — SSH with strict timeout
        gpu_out = run(
            "ssh -i /home/rich-rob/.ssh/id_ed25519_vm -o ConnectTimeout=3 -o BatchMode=yes "
            "root@192.168.122.143 "
            "'nvidia-smi --query-gpu=temperature.gpu,power.draw,utilization.gpu,"
            "memory.used,memory.total,fan.speed,clocks_throttle_reasons.active "
            "--format=csv,noheader,nounits' 2>/dev/null",
            timeout=8
        )
        if gpu_out:
            gp = [x.strip() for x in gpu_out.split(",")]
            if len(gp) >= 7:
                gmu, gmt = int(gp[3]), int(gp[4])
                stats.update({
                    "gpu_temp": gp[0], "gpu_power": gp[1], "gpu_util": gp[2],
                    "gpu_mem_used": round(gmu/1024, 1),
                    "gpu_mem_total": round(gmt/1024, 1),
                    "gpu_mem_pct": int(gmu/gmt*100),
                    "gpu_fan": gp[5],
                    "gpu_throttling": gp[6].strip() != "0x0000000000000000",
                })

        # Check what miner is running (lolMiner or SRBMiner)
        lol_out = run(
            "ssh -i /home/rich-rob/.ssh/id_ed25519_vm -o ConnectTimeout=3 -o BatchMode=yes "
            "root@192.168.122.143 "
            "'ps aux | grep SRBMiner | grep -v grep; ps aux | grep lolMiner | grep -v grep' 2>/dev/null",
            timeout=8
        )
        if lol_out:
            # lolMiner detection
            algo = re.search(r'--algo\s+(\S+)', lol_out)
            pool = re.search(r'--pool\s+(\S+)', lol_out)
            user = re.search(r'--user\s+(\S+)', lol_out)
            # SRBMiner detection
            if not algo:
                algo = re.search(r'--algorithm\s+(\S+)', lol_out)
            if not pool:
                pool = re.search(r'--pool\s+(\S+)', lol_out)
            if not user:
                user = re.search(r'--wallet\s+(\S+)', lol_out)
            stats["lol_algo"] = algo.group(1).upper() if algo else "—"
            stats["lol_pool"] = pool.group(1) if pool else "—"
            stats["lol_worker"] = user.group(1) if user else "—"
            stats["lol_running"] = True
        else:
            stats["lol_running"] = False

        # lolMiner shares + hashrate from journal (last 5 mins)
        try:
            import re as _re2
            import subprocess as _sp
            _jr = _sp.run(
                ['ssh', '-i', '/home/rich-rob/.ssh/id_ed25519_vm',
                 '-o', 'ConnectTimeout=3', '-o', 'BatchMode=yes',
                 'root@192.168.122.143',
                 'journalctl -u lolminer --since "5 minutes ago" --no-pager -q 2>/dev/null'],
                capture_output=True, text=True, timeout=10
            )
            journal_out = _jr.stdout
            if journal_out:
                accepted    = len(_re2.findall(r"Share accepted", journal_out))
                stale       = len(_re2.findall(r"Share is stale", journal_out))
                total       = accepted + stale
                accept_rate = round(accepted / total * 100) if total > 0 else 0
                # Parse pool hashrate from stats table (2nd speed column) — local speed shows -nan due to VFIO
                hr_matches  = _re2.findall(r"Total\s+[-\w.]+\s+([\d.]+)", journal_out)
                if not hr_matches:
                    # fallback to average speed
                    hr_matches = _re2.findall(r"Average speed .15s.: ([\d.]+)", journal_out)
                hr_val_ssh  = float(hr_matches[-1]) if hr_matches else 0.0
                stats["lol_accepted"]    = accepted
                stats["lol_stale"]       = stale
                stats["lol_total"]       = total
                stats["lol_accept_rate"] = accept_rate
                stats["lol_hr_mhs"]      = hr_val_ssh
        except Exception:
            pass

    # Record lolMiner hashrate history
    try:
        hr_out = __import__('subprocess').run(
            "ssh -i /home/rich-rob/.ssh/id_ed25519_vm -o ConnectTimeout=3 -o BatchMode=yes "
            "root@192.168.122.143 "
            "'curl -s http://localhost:3333/summary 2>/dev/null | python3 -c \'import json,sys; d=json.load(sys.stdin); print(d[\"Session\"][\"Performance\"][\"Hashrate_5min\"])\'  2>/dev/null'",
            shell=True, capture_output=True, text=True, timeout=8
        ).stdout.strip()
        hr_val = float(hr_out) if hr_out else 0.0
    except:
        hr_val = 0.0
    stats["lol_hr"] = hr_val
    _kvm_lol_history.append({"t": int(__import__('time').time()), "hr": hr_val})
    # Fetch PRL wallet data from AlphaPool
    try:
        import urllib.request as _ur2
        prl_addr = ""
        try:
            for _line in (Path.home() / ".mining_keys").read_text().splitlines():
                if _line.startswith("PRL_WALLET="):
                    prl_addr = _line.split("=",1)[1].strip()
        except: pass
        if not prl_addr: prl_addr = "prl1key"
        prl_url = f"https://pearl.alphapool.tech/api/miner/{prl_addr}"
        with _ur2.urlopen(_ur2.Request(prl_url, headers={"User-Agent":"Mozilla/5.0"}), timeout=8) as r:
            pd = json.loads(r.read())
        stats["prl_balance"]    = round(pd.get("balance_prl", 0), 4)
        stats["prl_paid"]       = round(pd.get("total_paid_prl", 0), 4)
        stats["prl_hashrate"]   = pd.get("estHash1h", "—")
        stats["prl_hashrate24"] = pd.get("estHash24h", "—")
        stats["prl_shares24"]   = pd.get("shares24h", 0)
    except Exception:
        pass

    # Pearl watchdog — only restart if enabled+active but stalled
    try:
        import time as _tw, re as _re2
        from datetime import datetime, timezone as _tz
        _ssh = "ssh -i /home/rich-rob/.ssh/id_ed25519_vm -o ConnectTimeout=3 -o BatchMode=yes root@192.168.122.143"
        _chk = run(f"{_ssh} 'systemctl is-enabled pearl && systemctl is-active pearl && docker logs pearl-miner 2>/dev/null | tail -1'", timeout=10)
        _lines = (_chk or '').strip().split('\n')
        _enabled = len(_lines) > 0 and _lines[0].strip() == 'enabled'
        _active  = len(_lines) > 1 and _lines[1].strip() == 'active'
        _lastlog = _lines[2] if len(_lines) > 2 else ''
        if _enabled and _active:
            _tsm = _re2.search(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', _lastlog)
            if _tsm:
                _last = datetime.fromisoformat(_tsm.group(1)).replace(tzinfo=_tz.utc)
                _age = (_tw.time() - _last.timestamp()) / 60
                if _age > 30:
                    log.warning(f"Pearl stalled {_age:.0f} mins — auto-restarting")
                    run(f"{_ssh} 'systemctl restart pearl'", timeout=10)
    except Exception:
        pass

    # Check pearl service status on VM
    try:
        pearl_status = run(
            "ssh -i /home/rich-rob/.ssh/id_ed25519_vm -o ConnectTimeout=3 -o BatchMode=yes "
            "root@192.168.122.143 'systemctl is-active pearl 2>/dev/null'",
            timeout=6
        )
        stats["pearl_running"] = pearl_status.strip() == "active"
    except Exception:
        stats["pearl_running"] = None

    # Read coin blacklist from VM
    try:
        import json as _json
        bl_out = run(
            "ssh -i /home/rich-rob/.ssh/id_ed25519_vm -o ConnectTimeout=3 -o BatchMode=yes "
            "root@192.168.122.143 "
            "'cat /home/rich-rob/coin_blacklist.json 2>/dev/null'",
            timeout=6
        )
        bl_data = _json.loads(bl_out) if bl_out else {}
        import time as _time
        now_ts = _time.time()
        stats["coin_blacklist"] = [k for k, v in bl_data.items() if v > now_ts]
    except Exception:
        stats["coin_blacklist"] = []
    _set_state("kvm", stats)

def fetch_xmrig():
    if CFG["kill_active"]:
        return
    customer_active = bool(run('docker ps --format "{{.Names}}" | grep "^C\\."'))
    for port in [18080, 18088, 3000]:
        import urllib.request as _ur_xmrig
        try:
            with _ur_xmrig.urlopen(f"http://localhost:{port}/2/summary", timeout=2) as _rx:
                out = _rx.read().decode()
        except Exception:
            out = ""
        out = out  # keep variable name consistent
        if out and "{" in out:
            try:
                data = json.loads(out)
                hr       = data.get("hashrate", {}).get("total", [None, None])
                conn     = data.get("connection", {})
                results  = data.get("results", {})
                uptime_s = data.get("uptime", 0)
                # Format uptime
                h, m = divmod(uptime_s // 60, 60)
                uptime_str = f"{h}h {m}m" if h else f"{m}m"
                # Record history
                _xmrig_history.append({
                    "t": int(__import__('time').time()),
                    "hr": hr[0] if hr[0] else 0,
                    "cores": len(data.get("hashrate", {}).get("threads", [])) or len(data.get("cpu", {}).get("enabled", []))
                })
                _set_state("xmrig", {
                    "paused":        False,
                    "running":       True,
                    "hashrate_10s":  f"{hr[0]:.1f}" if hr[0] else "—",
                    "hashrate_60s":  f"{hr[1]:.1f}" if hr[1] else "—",
                    "hashrate_peak": f"{data.get('hashrate', {}).get('highest', 0):.1f}",
                    "algo":          data.get("algo", "—"),
                    "pool":          conn.get("pool", "—"),
                    "ping":          conn.get("ping", "—"),
                    "accepted":      conn.get("accepted", 0),
                    "rejected":      conn.get("rejected", 0),
                    "shares_good":   results.get("shares_good", 0),
                    "shares_total":  results.get("shares_total", 0),
                    "uptime":        uptime_str,
                    "version":       data.get("version", "—"),
                    "cores":         len(data.get("hashrate", {}).get("threads", [])) or len(data.get("cpu", {}).get("enabled", [])),
                    "threads":       len(data.get("hashrate", {}).get("threads", [])),
                })
                return
            except: pass
    _set_state("xmrig", {"paused": False, "running": bool(run("pgrep xmrig")), "api_unavailable": True})

def fetch_qrl():
    """Fetch QRL wallet balance and price."""
    try:
        keys_file = Path.home() / ".mining_keys"
        wallet = ""
        if keys_file.exists():
            for line in keys_file.read_text().splitlines():
                if line.startswith("BLACKWELL_QRL_WALLET="):
                    wallet = line.split("=", 1)[1].strip()
        if not wallet:
            return
        import urllib.request as _ur_qrl
        balance_qrl = None
        usd_val = None
        nzd_val = None
        usd_price = 0
        nzd_price = 0
        # Fetch QRL balance from explorer
        try:
            req_bal = _ur_qrl.Request(
                f"https://explorer.theqrl.org/api/a/{wallet}",
                headers={"User-Agent": "Mozilla/5.0"})
            with _ur_qrl.urlopen(req_bal, timeout=8) as r_bal:
                d = json.loads(r_bal.read())
            shor = int(d.get("state", {}).get("balance", 0))
            balance_qrl = shor / 1_000_000_000
        except Exception as e:
            log.warning(f"fetch_qrl balance: {e}")
        # QRL price from price_charts state (fetched by fetch_price_charts)
        # Avoids duplicate CoinGecko call and rate limiting
        _qrl_hist = _state.get('price_charts', {}).get('qrl', [])
        usd_price = _qrl_hist[-1][1] if _qrl_hist else 0
        nzd_price = usd_price * 1.705  # NZD approximation
        if balance_qrl is not None and usd_price:
            usd_val = balance_qrl * usd_price
            nzd_val = balance_qrl * nzd_price
        # 30d price history now handled by fetch_price_charts (qrl key)
        price_hist = list(_state.get('price_charts', {}).get('qrl', []))

        _set_state("qrl", {
            "balance": f"{balance_qrl:.4f}" if balance_qrl is not None else "—",
            "usd": f"${usd_val:.2f}" if usd_val is not None else "—",
            "nzd": f"${nzd_val:.2f}" if nzd_val is not None else "—",
            "usd_price": f"${usd_price:.4f}" if usd_price else "—",
            "nzd_price": f"${nzd_price:.4f}" if nzd_price else "—",
            "price_history_30d": price_hist,
            "price_history_30d": price_hist,
        })
    except Exception as e:
        _set_state("qrl", {"balance": "—", "usd": "—", "nzd": "—"})

def fetch_image_capture():
    """Periodically capture latest image from active image-gen containers."""
    try:
        toggle_file = Path.home() / "container_history_commands" / ".image_capture_enabled"
        if not toggle_file.exists():
            return
        image_dir = Path.home() / "container_history_commands" / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        out = run('docker ps --format "{{.Names}}" 2>/dev/null')
        containers = [c for c in out.strip().split("\n") if c.startswith("C.")]
        for name in containers:
            output_paths = [
                "/ComfyUI/output", "/workspace/ComfyUI/output",
                "/workspace/outputs", "/app/outputs", "/root/outputs",
                "/workspace/stable-diffusion-webui/outputs"
            ]
            paths_str = " ".join(output_paths)
            latest = run(f"docker exec {name} find {paths_str} -maxdepth 4 -name '*.png' -not -name '*.preview.png' 2>/dev/null | sort | tail -1")
            if not latest or not latest.strip():
                continue
            latest = latest.strip()
            dest = image_dir / f"{name}_sample.png"
            # Check if same file as before by comparing size
            size_out = run(f"docker exec {name} stat -c%s '{latest}' 2>/dev/null")
            new_size = size_out.strip() if size_out else ""
            size_file = image_dir / f"{name}_last_size"
            old_size = size_file.read_text().strip() if size_file.exists() else ""
            if new_size and new_size == old_size:
                continue  # same image, skip
            import subprocess
            result = subprocess.run(["docker", "cp", f"{name}:{latest}", str(dest)], capture_output=True)
            if result.returncode == 0 and new_size:
                size_file.write_text(new_size)
    except Exception:
        pass

def fetch_cfx():
    """Fetch CFX balance from f2pool and price from CoinGecko."""
    try:
        keys_file = Path.home() / ".mining_keys"
        api_key = ""
        if keys_file.exists():
            for line in keys_file.read_text().splitlines():
                if line.startswith("F2POOL_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
        if not api_key:
            _set_state("cfx", {"balance": "—", "usd": "—", "nzd": "—"})
            return
        cfx_account = ""
        for line in keys_file.read_text().splitlines():
            if line.startswith("BLACKWELL_CFX_WALLET="):
                cfx_account = line.split("=", 1)[1].strip()
        if not cfx_account:
            cfx_account = "richrichrich26"
        import urllib.request as _ur_cfx
        balance_cfx = None
        # Fetch CFX balance from f2pool
        try:
            req_cfx = _ur_cfx.Request(
                f"https://api.f2pool.com/conflux/{cfx_account}",
                headers={"F2P-API-SECRET": api_key, "User-Agent": "Mozilla/5.0"})
            with _ur_cfx.urlopen(req_cfx, timeout=8) as r_cfx:
                d = json.loads(r_cfx.read())
            if "balance" in d:
                balance_cfx = float(d["balance"])
        except Exception as e:
            log.warning(f"fetch_cfx balance: {e}")
        # Price from price_charts state (no extra CoinGecko call needed)
        _cfx_ph = _state.get('price_charts', {}).get('cfx', [])
        usd_price = _cfx_ph[-1][1] if _cfx_ph else 0
        nzd_price = usd_price * 1.705
        _set_state("cfx", {
            "balance": f"{balance_cfx:.4f}" if balance_cfx is not None else "—",
            "usd": f"${balance_cfx * usd_price:.2f}" if balance_cfx is not None else "—",
            "nzd": f"${balance_cfx * nzd_price:.2f}" if balance_cfx is not None else "—",
            "usd_price": f"${usd_price:.4f}",
            "nzd_price": f"${nzd_price:.4f}",
        })
    except Exception:
        _set_state("cfx", {"balance": "—", "usd": "—", "nzd": "—"})

def fetch_kls():
    """Fetch KLS balance from WoolyPooly and price from CoinGecko."""
    try:
        wallet = "karlsen:qr53qqzqparx33ay97vtrccq74rrcf93nmp64zm4xw8ph2d99569xg837953p"
        import urllib.request as _ur_kls
        try:
            req_kls = _ur_kls.Request(f"https://api.woolypooly.com/api/kls-1/accounts/{wallet}", headers={"User-Agent":"Mozilla/5.0"})
            with _ur_kls.urlopen(req_kls, timeout=8) as _rk:
                bal_out = _rk.read().decode()
        except Exception:
            bal_out = ""
        # KLS price from price_charts state — no extra CoinGecko call needed
        _kls_pc = _state.get('price_charts', {}).get('kls', [])
        price_out = None  # not used — price comes from price_charts
        balance_kls = immature_kls = hashrate = 0
        if bal_out and "{" in bal_out:
            d = json.loads(bal_out)
            stats = d.get("stats", {})
            balance_kls = float(stats.get("balance", 0))
            immature_kls = float(stats.get("immature_balance", 0))
            hr = d.get("mode_stats", {}).get("pplns", {}).get("default", {}).get("currentHashrate", 0)
            hashrate = round(hr / 1e6, 2)
        usd_price = nzd_price = 0
        if price_out and "{" in price_out:
            p = json.loads(price_out)
            usd_price = p.get("karlsen", {}).get("usd", 0)
            nzd_price = p.get("karlsen", {}).get("nzd", 0)
        _set_state("kls", {
            "balance":   f"{balance_kls:.3f}" if balance_kls else "—",
            "immature":  f"{immature_kls:.3f}" if immature_kls else "—",
            "usd":       f"${balance_kls * usd_price:.2f}" if balance_kls else "—",
            "nzd":       f"${balance_kls * nzd_price:.2f}" if balance_kls else "—",
            "usd_price": f"${usd_price:.6f}",
            "hashrate":  f"{hashrate} MH/s" if hashrate else "—",
        })
    except Exception as e:
        _set_state("kls", {"balance": "—", "usd": "—", "nzd": "—"})

def fetch_gaming_wallets():
    """Fetch gaming PC wallet balances and prices."""
    try:
        keys_file = Path.home() / ".mining_keys"
        erg_wallet = ""
        if keys_file.exists():
            for line in keys_file.read_text().splitlines():
                if line.startswith("GAMING_ERG_WALLET="):
                    erg_wallet = line.split("=", 1)[1].strip()

        import urllib.request as _ur_gw
        # ERG balance
        erg_bal = None
        if erg_wallet:
            try:
                req_erg = _ur_gw.Request(
                    f"https://api.ergoplatform.com/api/v1/addresses/{erg_wallet}/balance/confirmed",
                    headers={"User-Agent": "Mozilla/5.0"})
                with _ur_gw.urlopen(req_erg, timeout=8) as r_erg:
                    d = json.loads(r_erg.read())
                nano = int(d.get("nanoErgs", 0))
                erg_bal = nano / 1_000_000_000
            except Exception as e:
                log.warning(f"fetch_gaming_wallets ERG: {e}")

        # Prices — use price_charts state (already fetched by fetch_price_charts)
        # Avoids duplicate CoinGecko calls and rate limiting
        pc = dict(_state.get('price_charts', {}))
        erg_hist  = pc.get('erg',  [])
        flux_hist = pc.get('flux', [])
        erg_usd   = erg_hist[-1][1]  if erg_hist  else 0
        flux_usd  = flux_hist[-1][1] if flux_hist else 0
        # NZD approximation — 1.705x USD (avoids extra API call)
        erg_nzd  = erg_usd  * 1.705
        flux_nzd = flux_usd * 1.705

        _set_state("gaming_wallets", {
            "erg_balance": f"{erg_bal:.4f}" if erg_bal is not None else "—",
            "erg_usd": f"${erg_bal * erg_usd:.2f}" if (erg_bal and erg_usd) else "—",
            "erg_nzd": f"${erg_bal * erg_nzd:.2f}" if (erg_bal and erg_nzd) else "—",
            "erg_price_usd": f"${erg_usd:.3f}",
            "flux_balance": "—",
            "flux_usd": "—",
            "flux_nzd": "—",
            "flux_price_usd": f"${flux_usd:.4f}" if flux_usd else "—",
            "iron_balance": "—",
            "iron_usd": "—",
            "iron_nzd": "—",
            "iron_paid": "—",
            "iron_hashrate": "—",
            "iron_price_usd": "—",
        })

        # Iron wallet from HeroMiners
        try:
            iron_addr = "4ea82762d16a587cd581cc7365872242fec25514cd0ae0bbfea74608c99eb12d"
            iron_url  = f"https://ironfish.herominers.com/api/stats_address?address={iron_addr}&longpoll=false"
            import urllib.request as _ur
            with _ur.urlopen(_ur.Request(iron_url, headers={"User-Agent":"Mozilla/5.0"}), timeout=10) as _r:
                _id = json.loads(_r.read())
            _is = _id.get("stats", {})
            iron_bal_raw  = int(_is.get("balance", 0))
            iron_paid_raw = int(_is.get("paid", 0))
            iron_bal      = round(iron_bal_raw  / 1e8, 4)
            iron_paid     = round(iron_paid_raw / 1e8, 4)
            iron_hr_1h    = _is.get("hashrate_1h", 0)
            iron_hr_str   = f"{round(iron_hr_1h/1e6, 2)} MH/s" if iron_hr_1h else "0 MH/s"
            try:
                with _ur.urlopen(_ur.Request(
                    "https://api.coingecko.com/api/v3/simple/price?ids=iron-fish&vs_currencies=usd,nzd",
                    headers={"User-Agent":"Mozilla/5.0"}), timeout=8) as _pr:
                    _pd = json.loads(_pr.read())
                iron_usd_price = _pd.get("ironfish", {}).get("usd", 0)
                iron_nzd_price = _pd.get("ironfish", {}).get("nzd", 0)
            except Exception:
                iron_usd_price = iron_nzd_price = 0
            with _lock:
                _state["gaming_wallets"].update({
                    "iron_balance":   f"{iron_bal:.4f} IRON",
                    "iron_usd":       f"${iron_bal * iron_usd_price:.2f}" if iron_usd_price else "—",
                    "iron_nzd":       f"${iron_bal * iron_nzd_price:.2f}" if iron_nzd_price else "—",
                    "iron_paid":      f"{iron_paid:.4f} IRON",
                    "iron_hashrate":  iron_hr_str,
                    "iron_price_usd": f"${iron_usd_price:.4f}" if iron_usd_price else "—",
                })
        except Exception:
            pass

    except Exception:
        _set_state("gaming_wallets", {})

def fetch_top10():
    """Fetch top 10 mining coins from WhatToMine for RTX 5070 profile."""
    try:
        import urllib.request as _ur
        url = (
            "https://whattomine.com/coins.json?utf8=%E2%9C%93"
            "&adapt_q_octopus=85&adapt_q_autolykos=200&adapt_q_kawpow=42"
            "&adapt_q_fishhash=60&adapt_q_etchash=450&adapt_q_karlsenhash=50"
            "&adapt_q_progpow=42&adapt_q_progpowz=41"
        )
        with _ur.urlopen(_ur.Request(url, headers={"User-Agent":"Mozilla/5.0"}), timeout=15) as r:
            data = json.loads(r.read())
        coins = data.get("coins", {})

        # Get BTC price for USD conversion
        try:
            with _ur.urlopen(_ur.Request(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                headers={"User-Agent":"Mozilla/5.0"}), timeout=8) as r:
                btc_usd = json.loads(r.read()).get("bitcoin", {}).get("usd", 80000)
        except Exception:
            btc_usd = 80000

        # Supported coins set
        # Coins lolminer can mine
        lolminer_supported = {"CFX","ERG","IRON","KLS","FLUX","RVN","ZANO","EPIC","MEWC"}
        supported = lolminer_supported

        top = []
        for name, c in coins.items():
            btc_rev = float(c.get("btc_revenue24") or c.get("btc_revenue") or 0)
            if btc_rev <= 0:
                continue
            usd_day = round(btc_rev * btc_usd, 3)
            tag = c.get("tag", name[:4]).upper()
            algo = c.get("algorithm", "—")[:14]
            top.append({
                "tag": tag,
                "name": name,
                "algo": algo,
                "usd_day": usd_day,
                "supported": tag in supported,
                "lolminer": tag in lolminer_supported,
            })

        top.sort(key=lambda x: x["usd_day"], reverse=True)
        _set_state("top10", {"coins": top[:10], "btc_usd": btc_usd})
    except Exception as e:
        _set_state("top10", {"coins": [], "btc_usd": 0})

def fetch_gaming_pc():
    """Fetch gaming PC GPU stats and current mining coin via SSH."""
    try:
        keys_file = Path.home() / ".mining_keys"
        gaming_ip = "192.168.50.172"
        if keys_file.exists():
            for line in keys_file.read_text().splitlines():
                if line.startswith("GAMING_PC_IP="):
                    gaming_ip = line.split("=", 1)[1].strip()

        # Detect OS first — try Linux user, fall back to Windows
        os_cmd = (f"ssh -o ConnectTimeout=5 -o BatchMode=yes rich-rob@{gaming_ip} "
                  f"'uname -s 2>/dev/null'")
        os_out = run(os_cmd + " 2>/dev/null")
        is_windows = False
        if not os_out or "Linux" not in os_out:
            win_cmd = (f"ssh -o ConnectTimeout=5 -o BatchMode=yes richr@{gaming_ip} "
                       f"'echo WINDOWS 2>nul'")
            win_out = run(win_cmd + " 2>/dev/null")
            if win_out and "WINDOWS" in win_out:
                os_out = "WINDOWS"
                is_windows = True

        # GPU stats — nvidia-smi works on both OS
        ssh_user = f"richr@{gaming_ip}" if is_windows else f"rich-rob@{gaming_ip}"
        cmd = (f"ssh -o ConnectTimeout=5 -o BatchMode=yes {ssh_user} "
               f"'nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,"
               f"memory.used,memory.total,power.draw --format=csv,noheader,nounits'")
        gpu_out = run(cmd + " 2>/dev/null")

        # Current coin from process list
        proc_cmd = (f"ssh -o ConnectTimeout=5 -o BatchMode=yes rich-rob@{gaming_ip} "
                    f"'ps aux | grep lolMiner | grep -v grep'")
        proc_out = run(proc_cmd + " 2>/dev/null")

        gpu_name = temp = util = mem_used = mem_total = power = "—"
        if gpu_out and "," in gpu_out:
            parts = [p.strip() for p in gpu_out.split(",")]
            if len(parts) >= 6:
                gpu_name, temp, util, mem_used, mem_total, power = parts[:6]

        coin = algo = "—"
        hashrate = "—"
        if proc_out:
            algo_m = re.search(r'--algo\s+(\S+)', proc_out)
            if algo_m:
                algo = algo_m.group(1)
                algo_map = {"AUTOLYKOS2": "ERG", "OCTOPUS": "CFX",
                           "FISHHASH": "IRON", "FLUX": "FLUX"}
                coin = algo_map.get(algo, algo)

        # CPU, RAM, disk via single SSH call
        ssh_base = f"ssh -o ConnectTimeout=5 -o BatchMode=yes rich-rob@{gaming_ip}"
        sysinfo_out = run(f"{ssh_base} 'cat /proc/stat | head -1 && grep -E MemTotal\|MemAvailable /proc/meminfo && df / | tail -1' 2>/dev/null", timeout=12)
        cpu_pct = ram_used = ram_total = disk_used = disk_total = "—"
        if sysinfo_out:
            lines = sysinfo_out.strip().split("\n")
            try:
                # CPU from first line
                cpu_fields = list(map(int, lines[0].split()[1:8]))
                idle = cpu_fields[3]; total = sum(cpu_fields)
                prev = getattr(fetch_gaming_pc, "_prev_cpu", None)
                fetch_gaming_pc._prev_cpu = (total, idle)
                if prev:
                    dt = total - prev[0]; di = idle - prev[1]
                    cpu_pct = str(round((1 - di/dt)*100, 1)) if dt else "—"
            except: pass
            try:
                mt = re.search(r'MemTotal:\s+(\d+)', sysinfo_out)
                ma = re.search(r'MemAvailable:\s+(\d+)', sysinfo_out)
                if mt and ma:
                    ram_total = round(int(mt.group(1))/1048576, 1)
                    ram_used = round((int(mt.group(1))-int(ma.group(1)))/1048576, 1)
            except: pass
            try:
                disk_line = [l for l in lines if '/dev/' in l]
                if disk_line:
                    dp = disk_line[0].split()
                    disk_used = round(int(dp[2])/1048576, 1)
                    disk_total = round(int(dp[1])/1048576, 1)
            except: pass

        # lolMiner hashrate via API
        hr_cmd = (f"ssh -o ConnectTimeout=5 -o BatchMode=yes rich-rob@{gaming_ip} "
                  f"'curl -s http://localhost:3333/summary 2>/dev/null'")
        hr_out = run(hr_cmd + " 2>/dev/null", timeout=8)
        if hr_out and "{{" not in hr_out:
            try:
                hr_data = json.loads(hr_out)
                algos = hr_data.get("Algorithms", [])
                hr_val = algos[0].get("Total_Performance", 0) * algos[0].get("Performance_Factor", 1) if algos else 0
                hashrate = f"{round(hr_val/1e6, 2)} MH/s" if hr_val else "—"
            except: pass

        _set_state("gaming", {
            "online": bool(gpu_out),
            "os": "UBUNTU" if os_out and "Linux" in os_out else "WINDOWS 11" if gpu_out else "—",
            "pearl_running": (lambda: run(f"ssh -o ConnectTimeout=3 -o BatchMode=yes rich-rob@{gaming_ip} 'systemctl is-active pearl 2>/dev/null'", timeout=5).strip() == "active")(),
            "gpu": gpu_name,
            "temp": temp,
            "util": util,
            "mem_used": mem_used,
            "mem_total": mem_total,
            "power": power,
            "coin": coin,
            "algo": algo,
            "cpu_pct": cpu_pct,
            "ram_used": ram_used,
            "ram_total": ram_total,
            "disk_used": disk_used,
            "disk_total": disk_total,
            "hashrate": hashrate,
        })
    except Exception as e:
        _set_state("gaming", {"online": False, "error": str(e)})

def fetch_docker():
    if CFG["kill_active"]:
        return
    out = run('docker ps --format "{{.Names}}|{{.Status}}|{{.Image}}|{{.RunningFor}}"')
    containers = []
    for line in out.strip().split("\n"):
        if line:
            p = line.split("|")
            if len(p) >= 3:
                c = {
                    "name": p[0], "status": p[1],
                    "image": p[2], "running_for": p[3] if len(p)>3 else ""
                }
                # For customer containers, grab the top CPU process
                if p[0].startswith("C."):
                    top_out = run(f"COLUMNS=512 docker top {p[0]} -eo pid,pcpu,args --sort=-pcpu 2>/dev/null")
                    lines = top_out.strip().split("\n")
                    # Skip header, get first process name
                    for tline in lines[1:]:
                        parts = tline.split()
                        if len(parts) >= 3:
                            c["top_process"] = parts[2]
                            c["top_cpu"] = parts[1]
                            break
                    # Full process list for live view
                    proc_lines = []
                    for tline in lines[1:6]:
                        parts = tline.split(None, 3)
                        if len(parts) >= 4:
                            proc_lines.append(f"{parts[1]:>6}%  {parts[3][:60]}")
                        elif len(parts) >= 3:
                            proc_lines.append(f"{parts[1]:>6}%  {parts[2][:60]}")
                    c["top_procs"] = "\n".join(proc_lines) if proc_lines else "—"
                containers.append(c)
    _set_state("docker", containers)

def fetch_vastai():
    if CFG["kill_active"] or resource_pressure() == "crit":
        return  # Vast.ai CLI is slow — skip under pressure
    out = run("/home/rich-rob/.local/bin/vastai show machines 2>/dev/null", timeout=25)
    lines = out.strip().split("\n")
    if len(lines) >= 2:
        h = lines[0].split(); v = lines[1].split()
        if len(v) >= len(h):
            _set_state("vastai", dict(zip(h, v)))
            return
    _set_state("vastai", {})

# ═══════════════════════════════════════════════════════════════
# BACKGROUND POLLER  (one thread per fetcher, staggered starts)
# ═══════════════════════════════════════════════════════════════
def _poll_loop(name, fn, interval, stagger=0):
    """Generic polling loop with error isolation."""
    time.sleep(stagger)    # stagger startup to avoid thundering herd
    while True:
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            _set_error(name, str(e))
            log.warning(f"[{name}] fetch error: {e}")
        elapsed = time.time() - t0
        sleep_for = max(1, interval - elapsed)
        time.sleep(sleep_for)

def start_background_pollers():
    pollers = [
        ("sys",    fetch_system,  CFG["interval_sys"],    0),
        ("price_charts", fetch_price_charts, CFG["interval_price_charts"], 45),
        ("fx_rate", fetch_fx_rate, 86400, 60),
        ("gpu",    fetch_gpu,     CFG["interval_gpu"],    2),
        ("docker", fetch_docker,  CFG["interval_docker"], 4),
        ("xmrig",  fetch_xmrig,   CFG["interval_xmrig"],  6),
        ("qrl",    fetch_qrl,     300,                    8),
        ("kls",    fetch_kls,     30,                     12),
        ("cfx",    fetch_cfx,     300,                    10),
        ("imgcap", fetch_image_capture, 300,              15),
        ("gaming", fetch_gaming_pc,    30,               20),
        ("gaming_wallets", fetch_gaming_wallets, 300, 25),
        ("top10",          fetch_top10,          300, 30),
        ("mining",         fetch_mining,         60,  35),
        ("kvm",    fetch_kvm,     CFG["interval_kvm"],    10),
        ("vastai", fetch_vastai,  CFG["interval_vastai"], 20),  # last — most expensive
    ]
    for name, fn, interval, stagger in pollers:
        t = threading.Thread(
            target=_poll_loop,
            args=(name, fn, interval, stagger),
            daemon=True, name=f"poll-{name}"
        )
        t.start()
        log.info(f"Started poller: {name} every {interval}s (stagger {stagger}s)")

# ═══════════════════════════════════════════════════════════════
# CHAT SAFEGUARDS
# ═══════════════════════════════════════════════════════════════
def chat_rate_check():
    """Returns (allowed, remaining, reset_in_s)."""
    now = time.time()
    window = CFG["chat_rate_window_s"]
    with _chat_lock:
        # Purge old timestamps
        while _chat_rate and now - _chat_rate[0] > window:
            _chat_rate.popleft()
        count = len(_chat_rate)
        allowed = count < CFG["chat_rate_msgs"]
        remaining = max(0, CFG["chat_rate_msgs"] - count)
        reset_in = round(window - (now - _chat_rate[0])) if _chat_rate else window
        return allowed, remaining, reset_in

def chat_idle_check():
    """True if chat has been idle long enough to consider paused."""
    with _chat_lock:
        if _chat_last_ts == 0:
            return False
        return (time.time() - _chat_last_ts) > CFG["chat_idle_pause_s"]

def call_claude(messages):
    """
    Call Anthropic API safely.
    Uses requests directly so no extra SDK dependency.
    Returns (reply_text, error_string).
    """
    import urllib.request
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None, "ANTHROPIC_API_KEY not set in environment"

    payload = {
        "model":      CFG["chat_model"],
        "max_tokens": CFG["chat_max_tokens"],
        "system": (
            "You are a concise ops assistant for blackwell-node-01, a GPU compute node "
            "running Vast.ai rentals and crypto mining in Porirua, NZ. "
            "Keep answers brief and technical. "
            "If asked about resource usage, remind the user to check the dashboard stats."
        ),
        "messages": messages,
    }
    data    = json.dumps(payload).encode()
    headers = {
        "Content-Type":      "application/json",
        "X-API-Key":         api_key,
        "anthropic-version": "2023-06-01",
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=CFG["chat_api_timeout_s"]) as resp:
            body = json.loads(resp.read().decode())
            text = body.get("content", [{}])[0].get("text", "")
            return text, None
    except Exception as e:
        return None, str(e)

# ═══════════════════════════════════════════════════════════════
# TELEMETRY  (log parsing — unchanged from v5)
# ═══════════════════════════════════════════════════════════════
def parse_all_temp_entries():
    log_file = Path.home() / "temp_history.log"
    entries  = []
    if not log_file.exists():
        return entries
    for line in log_file.read_text(errors="replace").strip().split("\n"):
        if not line.strip():
            continue
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue
        ts = parse_ts_utc(p[0])
        if not ts:
            continue
        try:
            temp  = float(p[1])
            power = float(p[2].replace(" W", ""))
            util  = float(p[3].replace(" %", ""))
            if len(p) >= 9:
                fan      = float(p[4].replace(" %","")) if p[4].strip() not in ["—",""] else None
                throttle = p[5].strip() != "0x0000000000000000"
                vram     = mib_to_gb(p[6].replace(" MiB",""))
                cpu      = float(p[7]) if p[7].strip() not in ["—",""] else None
                ram      = float(p[8]) if p[8].strip() not in ["—",""] else None
            elif len(p) == 7:
                fan      = float(p[4].replace(" %","")) if p[4].strip() not in ["—",""] else None
                throttle = p[5].strip() != "0x0000000000000000"
                vram     = mib_to_gb(p[6].replace(" MiB",""))
                cpu = ram = None
            else:
                fan      = None
                throttle = p[4].strip() != "0x0000000000000000"
                vram     = mib_to_gb(p[5].replace(" MiB",""))
                cpu = ram = None
            entries.append({
                "ts": ts, "temp": temp, "power": power, "util": util,
                "fan": fan, "throttle": throttle, "vram": vram, "cpu": cpu, "ram": ram
            })
        except:
            continue
    return entries

def get_container_history():
    log_file = Path.home() / "container_history.log"
    if not log_file.exists():
        return []
    containers = []
    for block in log_file.read_text(errors="replace").split("==================================="):
        if "Container:" not in block:
            continue
        c = {}
        for line in block.strip().split("\n"):
            line = line.strip()
            if   line.startswith("Container:"):
                c["name"]       = line.split(":",1)[1].strip()
            elif line.startswith("Started:"):
                raw = line.split(":",1)[1].strip()
                c["started"]     = utc_to_nzdt(raw) if raw else "—"
                c["started_raw"] = raw
                c["started_dt"]  = parse_ts_utc(raw)
            elif line.startswith("Image:"):
                c["image"]      = line.split(":",1)[1].strip()
            elif line.startswith("Type:"):
                c["type"]       = line.split(":",1)[1].strip()
            elif line.startswith("Runtype:"):
                c["runtype"]    = line.split(":",1)[1].strip()
            elif line.startswith("Duration:"):
                c["duration"]   = line.split(":",1)[1].strip()
            elif line.startswith("ExitCode:"):
                c["exit_code"]  = line.split(":",1)[1].strip()
            elif line.startswith("Finished:"):
                raw = line.split(":",1)[1].strip()
                c["finished"]   = utc_to_nzdt(raw) if raw else "—"
                c["finished_dt"]= parse_ts_utc(raw) if raw else None
        if "name" in c:
            containers.append(c)
    return containers

def build_customer_charts(containers, all_temps, running_names=None):
    if running_names is None:
        running_names = set()
    real  = [c for c in containers if "self-test" not in c.get("image","").lower()]    
    last6 = list(reversed(real[-10:] if len(real)>10 else real))
    now_utc = datetime.now(timezone.utc)
    charts  = []
    for c in last6:
        start_dt = c.get("started_dt")
        end_dt   = c.get("finished_dt") or now_utc
        if not start_dt:
            continue
        entries = [e for e in all_temps
                   if start_dt - timedelta(minutes=2) <= e["ts"] <= end_dt + timedelta(minutes=2)]
        labels, temps, powers, utils, fans, vrams, cpus, rams, throttles = [],[],[],[],[],[],[],[],[]
        for e in entries:
            labels.append(e["ts"].astimezone(NZDT).strftime("%H:%M"))
            temps.append(e["temp"])
            powers.append(round(e["power"]/575*100, 1))
            utils.append(e["util"])
            fans.append(e["fan"])
            vrams.append(e["vram"])
            cpus.append(e["cpu"])
            rams.append(e["ram"])
            throttles.append(1 if e["throttle"] else 0)
        charts.append({
            "name": c.get("name",""),
            "image": c.get("image",""),
            "started_iso": c.get("started_raw", c.get("started","")),
                "runtype":    c.get("runtype", "—"),
                "short_image": c.get("image","").split("/")[-1][:20],
            "is_image_gen": any(k in c.get("image","").lower() for k in ["comfy","echo-ai","stable-diffusion","a1111","invoke","fooocus","forge","swarm","sdnext","vladmandic"]),
            "type": c.get("type",""),
            "started":  c.get("started","—"),
            "finished": c.get("finished","—"),
            "duration": c.get("duration","—"),
     "active":   c.get("name","") in running_names,  # docker state is authoritative
	    "has_data": len(labels) > 0,
            "labels": labels, "temps": temps, "powers": powers,
            "utils": utils, "fans": fans, "vrams": vrams,
            "cpus": cpus, "rams": rams, "throttles": throttles,
        })

    # Only mark the most recent session of each container as active
    seen_active = set()
    for chart in charts:
        name = chart["name"]
        if chart["active"]:
            if name in seen_active:
                chart["active"] = False
            else:
                seen_active.add(name)

    # Sort: active containers first, then by most recent
    charts.sort(key=lambda c: (0 if c["active"] else 1))

    return charts

# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index():
    """Serve the shell — no data fetching here."""
    all_temps = parse_all_temp_entries()
    running_out = run('docker ps --format "{{.Names}}"')
    running_names = set(running_out.strip().split("\n"))
    charts = build_customer_charts(get_container_history(), all_temps, running_names)[:15]
    all_containers = list(reversed(get_container_history()[-20:]))
    return render_template_string(
        TEMPLATE,
        charts=charts,
        chart_data_json=json.dumps(charts, default=str),
        all_containers=all_containers,
    )

def get_xmrig_threads():
    try:
        import json as _jj
        cfg = _jj.loads(open('/home/rich-rob/xmrig-6.25.0/config.json').read())
        return len(cfg.get('cpu', {}).get('rx', []))
    except Exception:
        return None

@app.route("/api/stats")
def api_stats():
    """
    Instant cached stats — zero subprocess calls.
    Browser polls this every 15s instead of full page reload.
    """
    pressure = resource_pressure()   # read outside lock to avoid deadlock
    home = Path.home()
    with _lock:
        data = {
            "gpu":     dict(_state["gpu"]),
            "sys":     dict(_state["sys"]),
            "cpu_temp":   _get_cpu_temp(),
            "xmrig_threads": get_xmrig_threads(),
            "kvm":     dict(_state["kvm"]),
            "xmrig":   dict(_state["xmrig"]),
            "qrl":     dict(_state.get("qrl", {})),
            "cfx":     dict(_state.get("cfx", {})),
            "kls":     dict(_state.get("kls", {})),
            "gaming":  dict(_state.get("gaming", {})),
            "gaming_wallets": dict(_state.get("gaming_wallets", {})),
            "top10":          dict(_state.get("top10", {})),
            "mining":         dict(_state.get("mining", {})),
            "fx":             dict(_state.get("fx", {})),
            # 30d CoinGecko price histories — consumed by JS renderMiningChart
            "qrl_price_history":  list(_state.get("price_charts", {}).get("qrl", [])),
            "erg_price_history":  list(_state.get("price_charts", {}).get("erg", [])),
            "kls_price_history":  list(_state.get("price_charts", {}).get("kls", [])),
            "iron_price_history": list(_state.get("price_charts", {}).get("iron", [])),
            "cfx_price_history":  list(_state.get("price_charts", {}).get("cfx", [])),
            "nexa_price_history": list(_state.get("price_charts", {}).get("nexa", [])),
            "flux_price_history": list(_state.get("price_charts", {}).get("flux", [])),
            "docker":  list(_state["docker"]),
            "vastai":  dict(_state["vastai"]),
            "alerts":  list(_state["alerts"]),
            "updated": {k: round(time.time() - v, 0)
                        for k, v in _state["updated"].items()},
            "errors":  dict(_state["errors"]),
            "now_nzdt": datetime.now(NZDT).strftime("%d %b %Y %H:%M:%S NZST"),
            "pressure": pressure,
            "kill_active": CFG["kill_active"],
            "chat_enabled": CFG["chat_enabled"],
            "xmrig_locked":    (home / "disable-xmrig").exists(),
            "xmrig_mode":      (home / "xmrig-mode").read_text().strip() if (home / "xmrig-mode").exists() else "auto",
            "xmrig_force_on":  (home / "enable-xmrig").exists(),
            "xmrig_force_off": (home / "disable-xmrig").exists(),
            "lolminer_locked": (home / "disable-lolminer-5090").exists(),
            "kvm_locked":      (home / "disable-kvm").exists(),
            "kvm_mode":        (home / "kvm-mode").read_text().strip() if (home / "kvm-mode").exists() else "auto",
            "kvm_suspended":   (home / "suspend-kvm").exists(),
            "kvm_force_on":    (home / "enable-kvm").exists(),
            "kvm_force_off":   (home / "disable-kvm").exists(),
        }
    return jsonify(data)

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """
    Safe chat endpoint with layered safeguards:
    1. Kill switch check
    2. Chat-enabled check
    3. Rate limit check
    4. Resource pressure check (warn only, doesn't block)
    5. Token-capped API call
    6. Rolling context window
    """
    global _chat_last_ts

    # 1. Kill switch
    if CFG["kill_active"]:
        return jsonify({"error": "Kill switch active — chat disabled"}), 503

    # 2. Chat enabled
    if not CFG["chat_enabled"]:
        return jsonify({"error": "Chat is disabled"}), 503

    # 3. Rate limit
    allowed, remaining, reset_in = chat_rate_check()
    if not allowed:
        return jsonify({
            "error": f"Rate limit: {CFG['chat_rate_msgs']} msgs per {CFG['chat_rate_window_s']}s. "
                     f"Reset in {reset_in}s."
        }), 429

    body = request.get_json(silent=True) or {}
    user_msg = str(body.get("message", "")).strip()[:2000]  # hard cap on input length
    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    # 4. Resource pressure advisory
    pressure = resource_pressure()
    pressure_note = ""
    if pressure == "crit":
        pressure_note = " ⚠️ System under high load — response may be slow."

    # Build context window from rolling history
    with _chat_lock:
        _chat_rate.append(time.time())
        _chat_last_ts = time.time()
        history_snapshot = list(_chat_history)

    messages = history_snapshot + [{"role": "user", "content": user_msg}]

    # 5. Call API (runs synchronously but with timeout)
    reply, err = call_claude(messages)

    if err:
        log.warning(f"Chat API error: {err}")
        return jsonify({"error": f"API error: {err}"}), 502

    # 6. Update rolling context
    with _chat_lock:
        _chat_history.append({"role": "user",      "content": user_msg})
        _chat_history.append({"role": "assistant",  "content": reply})

    _, remaining_after, reset_in = chat_rate_check()

    return jsonify({
        "reply":          reply + pressure_note,
        "rate_remaining": remaining_after,
        "rate_reset_s":   reset_in,
        "context_msgs":   len(_chat_history),
    })

@app.route("/api/kill", methods=["POST"])
def api_kill():
    """Kill switch — halts fetchers, suspends KVM, stops miners."""
    home = Path.home()
    CFG["kill_active"]  = True
    CFG["chat_enabled"] = False
    # Suspend KVM
    run("sudo virsh suspend mining-ai-vm 2>/dev/null")
    (home / "disable-kvm").touch()
    # Stop miners via lockfiles
    (home / "disable-lolminer-5090").touch()
    (home / "disable-xmrig").touch()
    _update_alerts(get_cpu_pct(), 0)
    log.warning("KILL SWITCH ACTIVATED — KVM suspended, miners stopped")
    return jsonify({"status": "killed"})

@app.route("/api/kill", methods=["DELETE"])
def api_kill_reset():
    """Reset kill switch — resumes KVM and miners."""
    home = Path.home()
    CFG["kill_active"]  = False
    CFG["chat_enabled"] = True
    # Resume KVM
    run("sudo virsh resume mining-ai-vm 2>/dev/null")
    (home / "disable-kvm").unlink(missing_ok=True)
    # Resume miners
    (home / "disable-lolminer-5090").unlink(missing_ok=True)
    (home / "disable-xmrig").unlink(missing_ok=True)
    _update_alerts(get_cpu_pct(), 0)
    log.info("Kill switch reset — KVM resumed, miners started")
    return jsonify({"status": "reset"})

@app.route("/api/control/kvm", methods=["POST"])
def api_control_kvm():
    """Legacy toggle — kept for compatibility."""
    home = Path.home()
    with _lock:
        kvm_state = _state["kvm"].get("state", "")
    if kvm_state == "running":
        run("sudo virsh suspend mining-ai-vm 2>/dev/null")
        (home / "suspend-kvm").touch()
        log.info("KVM suspended via dashboard (legacy)")
        return jsonify({"status": "suspended"})
    elif kvm_state in ("paused", "suspended"):
        run("sudo virsh resume mining-ai-vm 2>/dev/null")
        (home / "suspend-kvm").unlink(missing_ok=True)
        log.info("KVM resumed via dashboard (legacy)")
        return jsonify({"status": "resumed"})
    else:
        return jsonify({"status": "unknown", "state": kvm_state}), 400

@app.route("/api/control/kvm/suspend", methods=["POST"])
def api_kvm_suspend():
    """Suspend the Ghost-VM (kill switch)."""
    home = Path.home()
    run("sudo virsh suspend mining-ai-vm 2>/dev/null")
    (home / "suspend-kvm").touch()
    log.info("KVM suspended via dashboard")
    return jsonify({"status": "suspended"})

@app.route("/api/control/kvm/resume", methods=["POST"])
def api_kvm_resume():
    """Resume the Ghost-VM."""
    home = Path.home()
    run("sudo virsh resume mining-ai-vm 2>/dev/null")
    (home / "suspend-kvm").unlink(missing_ok=True)
    log.info("KVM resumed via dashboard")
    return jsonify({"status": "resumed"})

@app.route("/api/control/kvm/mode", methods=["POST"])
def api_kvm_mode():
    """Set KVM lolminer mode: auto, force_on, force_off."""
    home = Path.home()
    data = request.get_json() or {}
    mode = data.get("mode")
    if mode == "auto":
        (home / "kvm-mode").write_text("auto")
        (home / "enable-kvm").unlink(missing_ok=True)
        (home / "disable-kvm").unlink(missing_ok=True)
        log.info("KVM mode set to auto")
        return jsonify({"status": "auto"})
    elif mode == "force_on":
        (home / "kvm-mode").write_text("forced")
        (home / "enable-kvm").touch()
        (home / "disable-kvm").unlink(missing_ok=True)
        log.info("KVM mode set to forced-on")
        return jsonify({"status": "force_on"})
    elif mode == "force_off":
        (home / "kvm-mode").write_text("forced")
        (home / "disable-kvm").touch()
        (home / "enable-kvm").unlink(missing_ok=True)
        log.info("KVM mode set to forced-off")
        return jsonify({"status": "force_off"})
    else:
        return jsonify({"status": "unknown", "mode": mode}), 400

@app.route("/api/control/xmrig", methods=["POST"])
def api_control_xmrig():
    """Legacy toggle — kept for compatibility."""
    home = Path.home()
    lockfile = home / "disable-xmrig"
    enable_file = home / "enable-xmrig"
    if lockfile.exists():
        lockfile.unlink()
        enable_file.touch()
        log.info("XMRig lockfile removed (legacy toggle)")
        return jsonify({"status": "started"})
    else:
        lockfile.touch()
        enable_file.unlink(missing_ok=True)
        log.info("XMRig lockfile created (legacy toggle)")
        return jsonify({"status": "stopped"})

@app.route("/api/control/gaming/reboot", methods=["POST"])
def api_gaming_reboot():
    """Reboot gaming PC (back to Ubuntu default)."""
    import subprocess
    host = "192.168.50.172"
    cmd  = f"ssh -o BatchMode=yes {host} 'sudo reboot'"
    try:
        subprocess.run(cmd, shell=True, timeout=15)
        return jsonify({"status": "rebooting"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/control/gaming/reboot-from-windows", methods=["POST"])
def api_gaming_reboot_from_windows():
    """Reboot gaming PC from Windows back to Ubuntu."""
    import subprocess
    host = "richr@192.168.50.172"
    cmd  = f"ssh -o BatchMode=yes {host} 'shutdown /r /t 0'"
    try:
        subprocess.run(cmd, shell=True, timeout=15)
        return jsonify({"status": "rebooting"})
    except Exception as e:
        return jsonify({{"error": str(e)}}), 500


@app.route("/api/control/gaming/reboot-windows", methods=["POST"])
def api_gaming_reboot_windows():
    """Reboot gaming PC into Windows via grub-reboot."""
    import subprocess
    host = "192.168.50.172"
    key  = "/home/rich-rob/.ssh/id_ed25519"
    cmd  = f"ssh -o BatchMode=yes {host} 'sudo grub-reboot 4 && sudo reboot'"
    try:
        subprocess.run(cmd, shell=True, timeout=15)
        return jsonify({"status": "rebooting"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/control/prl/<target>/<action>", methods=["POST"])
def api_prl_control(target, action):
    """Start/stop Pearl miner on ghost-vm or gaming-pc."""
    import subprocess
    if target == "kvm":
        host = "root@192.168.122.143"
        key  = "/home/rich-rob/.ssh/id_ed25519_vm"
        cmd  = f"ssh -i {key} -o BatchMode=yes {host} 'systemctl {action} pearl'"
    elif target == "gaming":
        host = "192.168.50.172"
        key  = "/home/rich-rob/.ssh/id_ed25519"
        cmd  = f"ssh -o BatchMode=yes {host} 'sudo systemctl {action} pearl'"
    else:
        return jsonify({"error": "unknown target"}), 400
    try:
        subprocess.run(cmd, shell=True, timeout=10)
        log.info(f"PRL {action} on {target}")
        return jsonify({"status": action})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/control/xmrig/mode", methods=["POST"])
def api_xmrig_mode():
    """Set XMRig mode: auto, force_on, force_off."""
    home = Path.home()
    data = request.get_json() or {}
    mode = data.get("mode")
    if mode == "auto":
        (home / "xmrig-mode").write_text("auto")
        (home / "enable-xmrig").unlink(missing_ok=True)
        (home / "disable-xmrig").unlink(missing_ok=True)
        log.info("XMRig mode set to auto")
        return jsonify({"status": "auto"})
    elif mode == "force_on":
        (home / "xmrig-mode").write_text("forced")
        (home / "enable-xmrig").touch()
        (home / "disable-xmrig").unlink(missing_ok=True)
        log.info("XMRig mode set to forced-on")
        return jsonify({"status": "force_on"})
    elif mode == "force_off":
        (home / "xmrig-mode").write_text("forced")
        (home / "disable-xmrig").touch()
        (home / "enable-xmrig").unlink(missing_ok=True)
        log.info("XMRig mode set to forced-off")
        return jsonify({"status": "force_off"})
    else:
        return jsonify({"status": "unknown", "mode": mode}), 400

@app.route("/api/control/unblacklist/<coin>", methods=["POST"])
def api_unblacklist(coin):
    """Remove a coin from the Ghost-VM coin blacklist."""
    allowed = {'ERG', 'KLS', 'IRON', 'NEXA', 'FLUX', 'CFX'}
    if coin.upper() not in allowed:
        return jsonify({"ok": False, "error": "coin not allowed"}), 400
    try:
        ssh = "ssh -i /home/rich-rob/.ssh/id_ed25519_vm -o ConnectTimeout=5 -o BatchMode=yes root@192.168.122.143"
        script = (
            f"python3 -c \"import json,os; "
            f"f='/home/rich-rob/coin_blacklist.json'; "
            f"d=json.load(open(f)) if os.path.exists(f) else {{}}; "
            f"d.pop('{coin.upper()}', None); "
            f"json.dump(d, open(f,'w')); "
            f"print('ok')\"")
        out = run(f"{ssh} \"{script}\"", timeout=10)
        return jsonify({"ok": "ok" in out, "result": out.strip()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/control/lolminer", methods=["POST"])
def api_control_lolminer():
    """Toggle lolMiner via lockfile."""
    home = Path.home()
    lockfile = home / "disable-lolminer-5090"
    if lockfile.exists():
        lockfile.unlink()
        log.info("lolMiner lockfile removed — miner will resume")
        return jsonify({"status": "started"})
    else:
        lockfile.touch()
        log.info("lolMiner lockfile created — miner will stop")
        return jsonify({"status": "stopped"})

@app.route("/api/procs/log", methods=["POST"])
def api_procs_log():
    """Append live process snapshot to active container log."""
    data = request.get_json()
    name = data.get("name", "")
    entry = data.get("entry", "")
    if not name or not entry:
        return jsonify({"status": "ignored"})
    # Only write if container is still running
    running_names = [c.get("name","") for c in _state.get("docker", [])]
    if name not in running_names:
        return jsonify({"status": "container_gone"})
    active_dir = Path.home() / "container_history_commands" / "active"
    active_dir.mkdir(parents=True, exist_ok=True)
    log_path = active_dir / f"{name}.procs.log"
    with open(log_path, "a") as f:
        f.write(entry)
    return jsonify({"status": "ok"})

@app.route("/api/chart_data")
def api_chart_data():
    """Fresh chart data for live updates."""
    all_temps = parse_all_temp_entries()
    with _lock:
        running = [c.get("name","") for c in _state["docker"]]
    charts = build_customer_charts(get_container_history(), all_temps, set(running))
    return jsonify(charts)

@app.route("/api/procs/read/<name>")
def api_procs_read(name):
    """Read saved procs log for a container."""
    hist_dir = Path.home() / "container_history_commands"
    # Check active first, then completed
    for path in [hist_dir / "active" / f"{name}.procs.log", hist_dir / f"{name}.procs.log"]:
        if path.exists():
            return jsonify({"log": path.read_text()})
    return jsonify({"log": ""})

@app.route("/api/procs/history/<name>", methods=["GET"])
def api_procs_history_get(name):
    """Return proc history — tries history.json first, falls back to parsing .txt file."""
    hist_dir = Path.home() / "container_history_commands"
    container_id = name.split("_")[0]
    if not container_id.startswith("C."):
        container_id = name

    # Try history.json first — only use if it has real process paths (contain / or spaces)
    def has_real_procs(h):
        return any("/" in k or len(k) > 20 for k in h.keys())

    for d in [hist_dir / "active", hist_dir]:
        if not d.exists():
            continue
        exact = d / f"{container_id}.history.json"
        if exact.exists():
            h = json.loads(exact.read_text())
            if h and has_real_procs(h):
                return jsonify({"history": h})
        matches = sorted(d.glob(f"{container_id}_*.history.json"))
        if matches:
            h = json.loads(matches[-1].read_text())
            if h and has_real_procs(h):
                return jsonify({"history": h})

    # Fall back to parsing .txt file
    txt_path = hist_dir / f"{container_id}.txt"
    if not txt_path.exists():
        return jsonify({"history": {}})

    history = {}
    try:
        for line in txt_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("===") or line.startswith("PID"):
                continue
            parts = line.split(None, 2)
            if len(parts) == 3:
                try:
                    pct = float(parts[1])
                    cmd = parts[2].strip()[:80]
                    if cmd not in history:
                        history[cmd] = {"cmd": cmd, "active": False, "lastPct": pct, "startTick": 0, "hist": []}
                    history[cmd]["hist"].append(pct)
                    if pct > history[cmd]["lastPct"]:
                        history[cmd]["lastPct"] = pct
                except ValueError:
                    pass
    except Exception:
        pass

    return jsonify({"history": history})

@app.route("/api/procs/history/<name>", methods=["POST"])
def api_procs_history_save(name):
    """Save proc history JSON for a container."""
    data = request.get_json()
    history = data.get("history", {})
    if not name or not history:
        return jsonify({"status": "ignored"})
    hist_dir = Path.home() / "container_history_commands"
    hist_dir.mkdir(parents=True, exist_ok=True)
    path = hist_dir / f"{name}.history.json"
    with open(path, "w") as f:
        json.dump(history, f)
    return jsonify({"status": "ok"})

@app.route("/api/analyse/<name>", methods=["POST"])
def api_analyse_container(name):
    """Use Claude to analyse what a container is doing."""
    data = request.get_json()
    processes = data.get("processes", [])
    image = data.get("image", "unknown")
    container_type = data.get("type", "unknown")

    if not processes:
        return jsonify({"error": "no process data"}), 400

    proc_text = '\n'.join([f"  {p['pct']}% - {p['cmd']}" for p in processes[:15]])

    prompt = f"""GPU server rental. What is this customer doing? Be concise — 3-4 short punchy sentences max.

Image: {image} | Type: {container_type}
Processes: {proc_text}

Cover: task, tech/model used, likely purpose. No bullet points. Plain sentences only."""

    try:
        import urllib.request, json as jsonlib
        payload = jsonlib.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        # Load API key from keys file
        api_key = ""
        keys_file = Path.home() / ".mining_keys"
        if keys_file.exists():
            for line in keys_file.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            result = jsonlib.loads(resp.read())
            text_out = result.get("content", [{}])[0].get("text", "No response")
            # Calculate cost (claude-sonnet-4: $3/MTok in, $15/MTok out)
            usage = result.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            cost_usd = (input_tokens * 3 + output_tokens * 15) / 1_000_000
            # Track cumulative usage
            usage_file = Path.home() / ".anthropic_usage"
            total = 0.0
            if usage_file.exists():
                try: total = float(usage_file.read_text().strip())
                except: pass
            total += cost_usd
            usage_file.write_text(f"{total:.6f}")
            return jsonify({
                "analysis": text_out,
                "cost_usd": round(cost_usd, 5),
                "total_usd": round(total, 4),
                "tokens": {"in": input_tokens, "out": output_tokens}
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/container/<name>/audio")
def api_container_audio(name):
    """List audio files in a running container."""
    search_dirs = ['/app/outputs', '/workspace/outputs', '/outputs', '/tmp/outputs', '/root/outputs']
    files = []
    for d in search_dirs:
        out = run(f"docker exec {name} find {d} -maxdepth 1 -type f \\( -name '*.wav' -o -name '*.mp3' -o -name '*.ogg' -o -name '*.flac' \\) -printf '%f\\t%s\\t%TY-%Tm-%Td %TH:%TM\\n' 2>/dev/null")
        if out:
            for line in out.strip().split('\n'):
                if '\t' in line:
                    parts = line.split('\t')
                    if len(parts) >= 3:
                        files.append({
                            "dir": d,
                            "name": parts[0],
                            "size": parts[1],
                            "date": parts[2]
                        })
    return jsonify({"files": files})

@app.route("/api/container/<name>/audio/<path:filename>")
def api_container_audio_stream(name, filename):
    """Stream an audio file from a running container."""
    import tempfile, subprocess as sp
    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tmp.close()
    sp.run(['docker', 'cp', f'{name}:{filename}', tmp.name], capture_output=True)
    from flask import send_file
    return send_file(tmp.name, mimetype='audio/wav', as_attachment=False)


@app.route("/api/chat/clear", methods=["POST"])
def api_chat_clear():
    """Clear chat context window — frees memory."""
    with _chat_lock:
        _chat_history.clear()
    return jsonify({"status": "cleared"})

@app.route("/api/kvm_lol_history")
def api_kvm_lol_history():
    """Return KVM lolMiner 24hr hashrate history."""
    with _lock:
        data = list(_kvm_lol_history)
    return jsonify(data)

@app.route("/api/analyse/save", methods=["POST"])
def api_analyse_save():
    import json as _j
    data = request.get_json()
    name = data.get("name")
    entry = data.get("entry")
    if not name or not entry:
        return jsonify({"error": "missing"}), 400
    hist_file = Path.home() / "container_analyses.json"
    try:
        hist = _j.loads(hist_file.read_text()) if hist_file.exists() else {}
    except Exception:
        hist = {}
    if name not in hist:
        hist[name] = []
    hist[name].insert(0, entry)
    hist[name] = hist[name][:5]
    hist_file.write_text(_j.dumps(hist))
    return jsonify({"status": "saved"})

@app.route("/api/analyse/load")
def api_analyse_load():
    import json as _j
    hist_file = Path.home() / "container_analyses.json"
    if not hist_file.exists():
        return jsonify({})
    try:
        return jsonify(_j.loads(hist_file.read_text()))
    except Exception:
        return jsonify({})

@app.route("/api/container_desc/generate", methods=["POST"])
def api_container_desc_generate():
    import json as _j, urllib.request, urllib.error
    data = request.get_json()
    container_name = data.get("name", "")
    image = data.get("image", "")
    if not container_name:
        return jsonify({"error": "missing name"}), 400
    # Read ANTHROPIC_API_KEY from ~/.mining_keys  (line format: ANTHROPIC_API_KEY=sk-ant-...)
    api_key = ""
    keys_file = Path.home() / ".mining_keys"
    if keys_file.exists():
        for line in keys_file.read_text().splitlines():
            if "ANTHROPIC" in line.upper() and "=" in line:
                api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not api_key:
        return jsonify({"error": "No ANTHROPIC_API_KEY found in ~/.mining_keys"}), 500
    prompt = (
        "In about 50 words, describe what this Vast.ai rental container is likely being used for, "
        "based on its name and Docker image. "
        f"Container: \"{container_name}\", Image: \"{image}\". "
        "Be concise and factual — one short paragraph, no bullet points."
    )
    payload = _j.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 150,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = _j.loads(resp.read())
        desc = result["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        return jsonify({"error": f"Anthropic API {e.code}: {e.read().decode()}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    # Persist under __desc__ prefix in container_analyses.json
    hist_file = Path.home() / "container_analyses.json"
    try:
        hist = _j.loads(hist_file.read_text()) if hist_file.exists() else {}
    except Exception:
        hist = {}
    hist["__desc__" + container_name] = [desc]
    hist_file.write_text(_j.dumps(hist))
    return jsonify({"description": desc})

@app.route("/api/vast_history")
def api_vast_history():
    """Return container booking history stats for VAST tab charts."""
    import re
    from collections import defaultdict
    from datetime import datetime, timezone
    log_file = Path.home() / "container_history.log"
    if not log_file.exists():
        return jsonify({})
    
    text = log_file.read_text(errors="replace")
    blocks = text.split("===================================")
    
    sessions_by_day = defaultdict(int)
    hours_by_day = defaultdict(float)
    repeat_counts = defaultdict(int)
    total_sessions = 0
    total_hours = 0.0
    
    for block in blocks:
        if "Container:" not in block:
            continue
        # Container prefix
        cm = re.search(r'Container:\s+(C\.\d+)', block)
        if cm:
            prefix = cm.group(1)
            repeat_counts[prefix] += 1
        
        # Started date
        sm = re.search(r'Started:\s+(\d{4}-\d{2}-\d{2})', block)
        if sm:
            day = sm.group(1)
            sessions_by_day[day] += 1
            total_sessions += 1
        
        # Duration
        dm = re.search(r'Duration:\s+(\d+)h\s+(\d+)m', block)
        if dm:
            h = int(dm.group(1)) + int(dm.group(2)) / 60
            if h > 0:
                hours_by_day[day] += h
                total_hours += h
    
    # Last 7 days
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    days7 = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    sessions7 = [[d, sessions_by_day.get(d, 0)] for d in days7]
    hours7    = [[d, round(hours_by_day.get(d, 0), 1)] for d in days7]
    
    # Top repeat containers
    repeats = sorted([(v, k) for k, v in repeat_counts.items() if v > 1], reverse=True)[:5]

    # Hourly occupancy for last 7 days
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    sessions_timed = []
    for block in blocks:
        if "Container:" not in block:
            continue
        sm2 = re.search(r'Started:\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', block)
        fm2 = re.search(r'Finished:\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', block)
        dm2 = re.search(r'Duration:\s+(\d+)h\s+(\d+)m', block)
        if sm2:
            try:
                start = datetime.fromisoformat(sm2.group(1)).replace(tzinfo=timezone.utc)
                if fm2 and fm2.group(1):
                    end = datetime.fromisoformat(fm2.group(1)).replace(tzinfo=timezone.utc)
                elif dm2:
                    dur = int(dm2.group(1))*3600 + int(dm2.group(2))*60
                    end = start + timedelta(seconds=max(dur, 60))
                else:
                    # No Finished: — check if it's the active container (started within 7 days)
                    # Only include if started recently (active session), cap to current hour
                    if start < week_ago:
                        continue  # old session never finished — skip (logger missed it)
                    end = now.replace(minute=0, second=0, microsecond=0)
                    if end <= start:
                        continue  # started this hour, skip
                if end > week_ago:
                    sessions_timed.append((max(start, week_ago), min(end, now)))
            except Exception:
                pass

    hourly_minutes = [0.0] * 168
    slot_start = week_ago.replace(minute=0, second=0, microsecond=0)
    for start, end in sessions_timed:
        for i in range(168):
            s = slot_start + timedelta(hours=i)
            e = s + timedelta(hours=1)
            ov_s = max(start, s)
            ov_e = min(end, e)
            if ov_e > ov_s:
                hourly_minutes[i] = min(60.0, hourly_minutes[i] + (ov_e - ov_s).total_seconds() / 60)

    hourly_data = [[int((slot_start + timedelta(hours=i)).timestamp()), round(m, 1)]
                   for i, m in enumerate(hourly_minutes)]

    return jsonify({
        "sessions_7d": sessions7,
        "hours_7d": hours7,
        "hourly_minutes": hourly_data,
        "total_sessions": total_sessions,
        "total_hours": round(total_hours, 1),
        "top_repeats": [{"prefix": k, "count": v} for v, k in repeats],
        "avg_session_h": round(total_hours / total_sessions, 2) if total_sessions else 0,
        "all_counts": {k: v for k, v in repeat_counts.items()},
    })

@app.route("/api/xmrig_history")
def api_xmrig_history():
    """Return XMRig 24hr hashrate + cores history."""
    with _lock:
        data = list(_xmrig_history)
    return jsonify(data)

@app.route("/api/config", methods=["GET"])
def api_config():
    """Return safe subset of config (no secrets)."""
    return jsonify({
        "chat_max_tokens":   CFG["chat_max_tokens"],
        "chat_rate_msgs":    CFG["chat_rate_msgs"],
        "chat_rate_window_s":CFG["chat_rate_window_s"],
        "chat_context_msgs": CFG["chat_context_msgs"],
        "chat_model":        CFG["chat_model"],
        "cpu_warn":          CFG["cpu_warn"],
        "cpu_crit":          CFG["cpu_crit"],
        "ram_warn":          CFG["ram_warn"],
        "ram_crit":          CFG["ram_crit"],
        "intervals": {
            "gpu":    CFG["interval_gpu"],
            "sys":    CFG["interval_sys"],
            "kvm":    CFG["interval_kvm"],
            "xmrig":  CFG["interval_xmrig"],
            "docker": CFG["interval_docker"],
            "vastai": CFG["interval_vastai"],
        }
    })

# ═══════════════════════════════════════════════════════════════
# HTML TEMPLATE
# ═══════════════════════════════════════════════════════════════
TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- NO meta refresh — we use AJAX polling instead -->
<title>blackwell-node-01</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Exo+2:wght@300;400;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{
  --bg:#060810;--bg2:#0b0f1a;--bg3:#111827;--border:#1e2d4a;
  --accent:#00d4ff;--accent2:#7c3aed;--green:#00ff88;--yellow:#fbbf24;
  --red:#ff4444;--text:#c8d8f0;--dim:#4a6080;
  --mono:'Share Tech Mono',monospace;--sans:'Exo 2',sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;
  background-image:radial-gradient(ellipse at 20% 0%,rgba(0,212,255,.05) 0%,transparent 50%),
  radial-gradient(ellipse at 80% 100%,rgba(124,58,237,.05) 0%,transparent 50%);}

/* ── Header ── */
.header{border-bottom:1px solid var(--border);padding:.75rem 1.25rem;display:flex;
  align-items:center;justify-content:space-between;background:rgba(11,15,26,.9);
  backdrop-filter:blur(10px);position:sticky;top:0;z-index:100;}
.logo{font-family:var(--mono);font-size:1rem;color:var(--accent);}
.logo span{color:var(--accent2);}
.livdot{width:7px;height:7px;border-radius:50%;background:var(--green);
  box-shadow:0 0 8px var(--green);animation:pulse 2s infinite;flex-shrink:0;}
.livdot.warn{background:var(--yellow);box-shadow:0 0 8px var(--yellow);}
.livdot.crit{background:var(--red);box-shadow:0 0 8px var(--red);animation:blink .5s infinite;}
.rg2x4{display:grid;grid-template-columns:1fr 1fr;gap:.3rem;margin-bottom:.4rem;}
.wtabs{display:flex;gap:0;margin-top:.5rem;border-top:1px solid var(--border);padding-top:.5rem;}
.wtab{flex:1;text-align:center;font-size:.58rem;padding:.25rem .2rem;cursor:pointer;border-bottom:2px solid transparent;color:var(--dim);font-family:var(--mono);letter-spacing:.05em;transition:all .2s;}
.wtab:hover{color:var(--text);}
.wtab.active{color:var(--accent);border-bottom:2px solid var(--accent);}
.wtab.mining{color:var(--green);border-bottom:2px solid var(--green);}
.wpanel{display:none;padding-top:.4rem;}
.wpanel.active{display:block;}
.health-bar{display:flex;flex-wrap:wrap;gap:.4rem .8rem;padding:.4rem 1.25rem;
  background:rgba(11,15,26,.7);border-bottom:1px solid var(--border);font-size:.6rem;
  font-family:var(--mono);color:var(--dim);}
.hitem{display:flex;align-items:center;gap:.3rem;}
.hdot{width:5px;height:5px;border-radius:50%;background:var(--dim);flex-shrink:0;}
.hdot.ok{background:var(--green);box-shadow:0 0 5px var(--green);}
.hdot.warn{background:var(--yellow);box-shadow:0 0 5px var(--yellow);}
.hdot.err{background:var(--red);box-shadow:0 0 5px var(--red);}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.1}}
.hrow{display:flex;align-items:center;gap:.75rem;}
.ts{font-family:var(--mono);font-size:.7rem;color:var(--dim);}

/* ── Alert bar ── */
#alert-bar{display:none;padding:.4rem 1.25rem;font-family:var(--mono);font-size:.72rem;
  border-bottom:1px solid var(--border);gap:.5rem;flex-wrap:wrap;}
.alert-chip{padding:2px 10px;border-radius:3px;font-size:.68rem;}
.alert-chip.warn{background:rgba(251,191,36,.12);color:var(--yellow);border:1px solid rgba(251,191,36,.3);}
.alert-chip.crit{background:rgba(255,68,68,.12);color:var(--red);border:1px solid rgba(255,68,68,.3);}
.alert-chip.kill{background:rgba(255,68,68,.25);color:var(--red);border:1px solid var(--red);}

/* ── Grid ── */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));
  gap:.9rem;padding:.9rem 1.25rem;max-width:1500px;margin:0 auto;}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow:hidden;position:relative;}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.4;}
.ch{padding:.6rem .9rem;border-bottom:1px solid var(--border);display:flex;
  align-items:center;justify-content:space-between;
  font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:var(--accent);}
.ch-left{display:flex;align-items:center;gap:.4rem;}
.staleness{font-size:.6rem;color:var(--dim);font-weight:400;text-transform:none;letter-spacing:0;}
.cb{padding:.9rem;}
.sg{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;}
.st{background:var(--bg3);border:1px solid var(--border);border-radius:5px;padding:.6rem;}
.sl{font-size:.6rem;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);margin-bottom:.2rem;}
.sv{font-family:var(--mono);font-size:1.2rem;color:var(--accent);line-height:1;}
.sv.hot{color:var(--red)}.sv.warm{color:var(--yellow)}.sv.cool{color:var(--green)}
.su{font-size:.6rem;color:var(--dim);margin-left:2px;}
.bw{margin-top:.35rem;height:3px;background:var(--bg);border-radius:2px;overflow:hidden;}
.b{height:100%;border-radius:2px;background:var(--accent);transition:width .8s;}
.b.hot{background:var(--red)}.b.warm{background:var(--yellow)}.b.grn{background:var(--green)}
.tbadge{display:inline-block;padding:1px 7px;border-radius:3px;font-family:var(--mono);font-size:.62rem;font-weight:600;}
.tbadge.y{background:rgba(255,68,68,.2);color:var(--red);border:1px solid var(--red);}
.tbadge.n{background:rgba(0,255,136,.1);color:var(--green);border:1px solid var(--green);}
.vr{display:flex;justify-content:space-between;align-items:center;
  padding:.35rem 0;border-bottom:1px solid var(--border);font-size:.82rem;}
.vr:last-child{border-bottom:none}
.vk{color:var(--dim);font-size:.72rem;}
.vv{font-family:var(--mono)}.vv.g{color:var(--green)}
.ob{padding:2px 9px;border-radius:3px;font-family:var(--mono);font-size:.72rem;font-weight:600;}
.ob.D{background:rgba(0,255,136,.15);color:var(--green);border:1px solid var(--green);}
.ob.I{background:rgba(251,191,36,.15);color:var(--yellow);border:1px solid var(--yellow);}
.ob.x{background:rgba(74,96,128,.15);color:var(--dim);border:1px solid var(--border);}
.mtabs{display:flex;border-bottom:1px solid var(--border);background:var(--bg3);}
.mtab{padding:.45rem .75rem;font-size:.64rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.07em;cursor:pointer;color:var(--dim);border-bottom:2px solid transparent;
  transition:all .15s;user-select:none;white-space:nowrap;}
.mtab:hover{color:var(--text)}.mtab.active{color:var(--accent);border-bottom-color:var(--accent);}
.mpanel{display:none;padding:.8rem}.mpanel.active{display:block;}
.cbox{display:flex;align-items:flex-start;gap:.5rem;padding:.55rem;
  background:rgba(0,255,136,.04);border:1px solid rgba(0,255,136,.15);
  border-radius:5px;margin-bottom:.5rem;}
.cbox:last-child{margin-bottom:0}
.cn{font-family:var(--mono);font-size:.76rem;color:var(--green);}
.ci{font-size:.67rem;color:var(--dim);margin-top:2px;}.ci.hl{color:var(--text);}
.pill{padding:2px 8px;border-radius:20px;font-size:.65rem;font-family:var(--mono);font-weight:600;}
.pill.on{background:rgba(0,255,136,.12);color:var(--green);border:1px solid rgba(0,255,136,.25);}
.pill.off{background:rgba(74,96,128,.1);color:var(--dim);border:1px solid var(--border);}
.paused-msg{display:flex;align-items:center;gap:.5rem;padding:.75rem;
  background:rgba(251,191,36,.05);border:1px solid rgba(251,191,36,.15);
  border-radius:5px;font-size:.75rem;color:var(--yellow);font-family:var(--mono);}
.gpu-sub{background:var(--bg3);border:1px solid var(--border);border-radius:5px;
  padding:.6rem;margin-top:.6rem;}
.gpu-sub-title{font-size:.6rem;text-transform:uppercase;letter-spacing:.08em;
  color:var(--accent2);margin-bottom:.5rem;font-weight:600;}

/* ── Chat card ── */
.chat-card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;
  overflow:hidden;position:relative;display:flex;flex-direction:column;max-height:480px;}
.chat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--accent2),transparent);opacity:.4;}
.chat-msgs{flex:1;overflow-y:auto;padding:.75rem;display:flex;flex-direction:column;gap:.5rem;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;}
.chat-msgs::-webkit-scrollbar{width:4px;}
.chat-msgs::-webkit-scrollbar-thumb{background:var(--border);}
.msg{padding:.5rem .75rem;border-radius:6px;font-size:.78rem;line-height:1.5;max-width:92%;word-break:break-word;}
.msg.user{background:rgba(0,212,255,.08);border:1px solid rgba(0,212,255,.15);
  color:var(--text);align-self:flex-end;font-family:var(--mono);}
.msg.assistant{background:rgba(124,58,237,.08);border:1px solid rgba(124,58,237,.2);color:var(--text);}
.msg.system{background:rgba(74,96,128,.08);border:1px solid var(--border);
  color:var(--dim);font-family:var(--mono);font-size:.7rem;align-self:center;}
.chat-footer{padding:.6rem;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:.4rem;}
.chat-meta{display:flex;justify-content:space-between;align-items:center;
  font-size:.62rem;color:var(--dim);font-family:var(--mono);}
.chat-input-row{display:flex;gap:.5rem;}
#chat-input{flex:1;background:var(--bg3);border:1px solid var(--border);border-radius:5px;
  padding:.45rem .65rem;color:var(--text);font-family:var(--mono);font-size:.78rem;
  outline:none;resize:none;}
#chat-input:focus{border-color:var(--accent2);}
#chat-input:disabled{opacity:.4;cursor:not-allowed;}
.btn{padding:.4rem .85rem;border-radius:5px;font-family:var(--mono);font-size:.72rem;
  font-weight:600;cursor:pointer;border:none;transition:all .15s;}
.btn-send{background:rgba(124,58,237,.2);color:var(--accent2);border:1px solid rgba(124,58,237,.3);}
.btn-send:hover{background:rgba(124,58,237,.35);}
.btn-send:disabled{opacity:.3;cursor:not-allowed;}
.btn-kill{background:rgba(255,68,68,.12);color:var(--red);border:1px solid rgba(255,68,68,.3);font-size:.65rem;}
.btn-kill:hover{background:rgba(255,68,68,.25);}
.btn-kill.active{background:rgba(255,68,68,.3);border-color:var(--red);}
.btn-ctrl{padding:2px 8px;border-radius:4px;font-family:var(--mono);font-size:.62rem;font-weight:600;
  cursor:pointer;border:1px solid var(--border2);background:rgba(255,255,255,.04);color:var(--dim);transition:all .15s;}
.btn-ctrl:hover{background:rgba(255,255,255,.08);color:var(--text);}
.btn-ctrl.active{background:rgba(0,212,255,.1);color:var(--accent);border-color:var(--accent);pointer-events:none;cursor:default;}
.btn-ctrl.stopped{background:rgba(255,68,68,.1);color:var(--red);border-color:rgba(255,68,68,.3);}
.btn-clear{background:rgba(74,96,128,.1);color:var(--dim);border:1px solid var(--border);font-size:.65rem;}
.btn-clear:hover{color:var(--text);}
.rate-bar{height:2px;background:var(--border);border-radius:1px;overflow:hidden;}
.rate-fill{height:100%;background:var(--green);transition:width .3s;}
.rate-fill.half{background:var(--yellow)}.rate-fill.full{background:var(--red);}
.typing{display:none;padding:.3rem .5rem;font-size:.68rem;color:var(--dim);font-family:var(--mono);}
.typing.show{display:block;}

/* ── Bottom panels ── */
.bottom{padding:.9rem 1.25rem;max-width:1500px;margin:0 auto;}
.bigcard{background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow:hidden;position:relative;}
.bigcard::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--accent),transparent);opacity:.4;}
.otabs{display:flex;border-bottom:1px solid var(--border);}
.otab{padding:.6rem 1.2rem;font-size:.72rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.08em;cursor:pointer;color:var(--dim);border-bottom:2px solid transparent;
  transition:all .2s;user-select:none;}
.otab:hover{color:var(--text)}.otab.active{color:var(--accent);border-bottom-color:var(--accent);}
.opanel{display:none;}.opanel.active{display:flex;}
.vtab-wrap{display:flex;width:100%;min-height:460px;}
.vtabs{display:flex;flex-direction:column;width:150px;flex-shrink:0;
  border-right:1px solid var(--border);background:var(--bg);}
.vtab{padding:.6rem .7rem;cursor:pointer;border-left:2px solid transparent;transition:all .15s;user-select:none;}
.vtab:hover{background:rgba(0,212,255,.04);}
.vtab.active{background:rgba(0,212,255,.06);border-left-color:var(--accent);}
.vtab-name{font-family:var(--mono);font-size:.68rem;color:var(--text);}
.vtab-img{font-size:.58rem;color:var(--dim);margin-top:2px;word-break:break-all;line-height:1.4;}
.vtab-badge{display:inline-block;padding:1px 5px;border-radius:2px;font-size:.56rem;font-family:var(--mono);margin-top:3px;}
.vtab-badge.live{background:rgba(0,255,136,.15);color:var(--green);border:1px solid rgba(0,255,136,.3);}
.vtab-badge.done{background:rgba(74,96,128,.1);color:var(--dim);border:1px solid var(--border);}
.vtab-badge.img{background:rgba(168,85,247,.15);color:#c084fc;border:1px solid rgba(168,85,247,.3);margin-left:3px;}
.vpanel{display:none;flex:1;padding:1rem;flex-direction:column;gap:.75rem;overflow:hidden;}
.vpanel.active{display:flex;}
.cust-meta{display:flex;flex-wrap:wrap;gap:.5rem;}
.cust-meta-item{background:var(--bg3);border:1px solid var(--border);border-radius:4px;padding:.4rem .6rem;}
.cust-meta-label{font-size:.58rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);display:block;}
.cust-meta-val{font-family:var(--mono);font-size:.72rem;color:var(--text);}
.chart-row{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;}
.chart-box{background:var(--bg3);border:1px solid var(--border);border-radius:5px;padding:.6rem;margin:.3rem;}.chart-box canvas{height:180px !important;}.chart-row .chart-box{min-height:210px;}
.chart-title{font-size:.62rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:.4rem;}
.no-data{color:var(--dim);font-size:.8rem;text-align:center;padding:2rem;}
.jobpanel{overflow-x:auto;padding:.75rem;}
table{width:100%;border-collapse:collapse;font-size:.72rem;}
th{color:var(--dim);text-transform:uppercase;letter-spacing:.06em;font-size:.6rem;
  padding:.5rem .75rem;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap;}
td{padding:.5rem .75rem;border-bottom:1px solid var(--border);}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.015);}
.tt{display:inline-block;padding:1px 6px;border-radius:3px;font-family:var(--mono);font-size:.6rem;}
.tt.sv2{background:rgba(124,58,237,.15);color:var(--accent2);border:1px solid rgba(124,58,237,.3);}
.tt.jup{background:rgba(251,191,36,.1);color:var(--yellow);border:1px solid rgba(251,191,36,.2);}
.tt.ssh{background:rgba(0,212,255,.08);color:var(--accent);border:1px solid rgba(0,212,255,.2);}
.stale-warning{font-size:.62rem;color:var(--yellow);font-family:var(--mono);}
</style>
</head>
<body>

<div class="header">
  <div class="hrow">
    <div id="livdot" class="livdot"></div>
    <div class="logo">blackwell&#8209;<span>node&#8209;01</span></div>
    <div class="ts" id="ts-now">—</div>
  </div>
  <div class="hrow">
    <div class="ts" id="fetch-age" style="color:var(--dim)"></div>
    <button class="btn btn-kill" id="kill-btn" onclick="toggleKill()">⛔ KILL</button>
  </div>
</div>

<div id="alert-bar" style="display:flex;"></div>
<div class="health-bar" id="health-bar">
  <div class="hitem"><div class="hdot" id="h-gpu"></div><span>GPU</span></div>
  <div class="hitem"><div class="hdot" id="h-xmrig"></div><span>XMRig</span></div>
  <div class="hitem"><div class="hdot" id="h-kvm"></div><span>KVM</span></div>
  <div class="hitem"><div class="hdot" id="h-vast"></div><span>Vast</span></div>
  <div class="hitem"><div class="hdot" id="h-docker"></div><span>Docker</span></div>
  <div class="hitem"><div class="hdot" id="h-gaming"></div><span>Gaming PC</span></div>
  <div class="hitem"><div class="hdot" id="h-lolminer"></div><span>lolMiner</span></div>
  <div class="hitem"><div class="hdot" id="h-qrl"></div><span>QRL API</span></div>
  <div class="hitem"><div class="hdot" id="h-cfx"></div><span>CFX API</span></div>
  <div class="hitem"><div class="hdot" id="h-coingecko"></div><span>CoinGecko</span></div>
</div>

<div class="grid">

  <!-- ── RTX 5090 + VAST.AI ── -->
  <div class="card">
    <div class="ch">
      <div class="ch-left" style="flex:1;min-height:3rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span>🖥 BLACKWELL / RTX 5090 / VAST.AI</span>
          <div style="display:flex;gap:.25rem;">
            <span class="staleness" id="stale-gpu"></span>
            <span class="staleness" id="stale-sys"></span>
            <span class="staleness" id="stale-vastai"></span>
            <span class="staleness" id="stale-docker"></span>
          </div>
        </div>
        <div style="margin-top:.3rem;">
          <button class="btn-ctrl btn-sm" id="lolminer-btn" onclick="toggleLolminer()">⛏ lolMiner</button>
        </div>
      </div>
    </div>
    <div class="cb">
      <!-- 2x4 resource grid -->
      <div class="rg2x4">
        <div class="st"><div class="sl">TEMP</div><div class="sv" id="gpu-temp">—</div><div class="bw"><div class="b" id="gpu-temp-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">POWER</div><div class="sv" id="gpu-power">—</div><div class="bw"><div class="b" id="gpu-power-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">GPU UTIL</div><div class="sv" id="gpu-util">—</div><div class="bw"><div class="b" id="gpu-util-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">VRAM</div><div class="sv" id="gpu-mem">—</div><div class="bw"><div class="b" id="gpu-mem-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">CPU</div><div class="sv" id="sys-cpu">—</div><div class="bw"><div class="b" id="sys-cpu-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">RAM</div><div class="sv" id="sys-ram">—</div><div class="bw"><div class="b" id="sys-ram-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">DISK</div><div class="sv" id="sys-disk">—</div><div class="bw"><div class="b" id="sys-disk-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">NET I/O</div><div class="sv" id="sys-net">—</div></div>
      </div>
      <div style="display:flex;gap:.75rem;align-items:center;font-size:.6rem;color:var(--dim);margin-bottom:.3rem;">
        <span>Fan <span class="vv" id="gpu-fan" style="font-size:.65rem">—</span></span>
        <span>Throttle <span id="gpu-throttle">—</span></span>
        <span>Load <span class="vv" id="sys-load" style="font-size:.65rem">—</span></span>
        <span id="sys-swap" style="display:none">—</span><span>Up <span class="vv" id="sys-uptime" style="font-size:.65rem">—</span></span>
      </div>
      <!-- Vast moved to MINING panel -->
    </div>
  </div>




    <!-- ── KVM / Ghost-VM ── -->
  <div class="card">
    <div class="ch">
      <div class="ch-left" style="flex:1;min-height:3rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span>🖥 BLACKWELL / RTX 5070 / GHOST-VM <span id="kvm-mine-status" style="font-size:.58rem;margin-left:.3rem;padding:.1rem .35rem;border-radius:3px;background:rgba(255,255,255,.05);color:var(--dim)">—</span></span>
          <span class="staleness" id="stale-kvm"></span>
        </div>
        <div style="display:flex;gap:.25rem;margin-top:.3rem;flex-wrap:wrap;">
          <button class="btn-ctrl btn-sm" id="kvm-suspend-btn" onclick="kvmSuspend()">Suspend</button>
          <button class="btn-ctrl btn-sm" id="kvm-resume-btn" onclick="kvmResume()">Resume</button>
          <button class="btn-ctrl btn-sm" id="kvm-mode-auto-btn" onclick="kvmSetMode('auto')">Auto</button>
          <button class="btn-ctrl btn-sm" id="kvm-mode-on-btn" onclick="kvmSetMode('force_on')">Force ON</button>
          <button class="btn-ctrl btn-sm" id="kvm-mode-off-btn" onclick="kvmSetMode('force_off')">Force OFF</button>
        </div>
      </div>
    </div>
    <div class="cb" id="kvm-body">
      <div id="kvm-detail" style="display:none;">
        <!-- 2x4 resource grid -->
        <div class="rg2x4" style="margin-top:.4rem;">
          <div class="st"><div class="sl">TEMP</div><div class="sv" id="kvm-gpu-temp">—</div><div class="bw"><div class="b" id="kvm-gpu-temp-bar" style="width:0%"></div></div></div>
          <div class="st"><div class="sl">POWER</div><div class="sv" id="kvm-gpu-power">—</div><div class="bw"><div class="b" id="kvm-gpu-power-bar" style="width:0%"></div></div></div>
          <div class="st"><div class="sl">GPU UTIL</div><div class="sv" id="kvm-gpu-util">—</div><div class="bw"><div class="b" id="kvm-gpu-util-bar" style="width:0%"></div></div></div>
          <div class="st"><div class="sl">VRAM</div><div class="sv" id="kvm-gpu-vram">—</div><div class="bw"><div class="b" id="kvm-gpu-vram-bar" style="width:0%"></div></div></div>
          <div class="st"><div class="sl">CPU</div><div class="sv" id="kvm-cpu">—</div><div class="bw"><div class="b" id="kvm-cpu-bar" style="width:0%"></div></div></div>
          <div class="st"><div class="sl">RAM</div><div class="sv" id="kvm-ram">—</div><div class="bw"><div class="b" id="kvm-ram-bar" style="width:0%"></div></div></div>
          <div class="st"><div class="sl">DISK</div><div class="sv" id="kvm-disk">—</div><div class="bw"><div class="b" id="kvm-disk-bar" style="width:0%"></div></div></div>
          <div class="st"><div class="sl">HASHRATE</div><div class="sv" id="kvm-lol-hr">—</div></div>

        </div>
        <div id="kvm-gpu-block" class="gpu-sub" style="display:none;">
        </div>


      </div>
    </div>
  </div>

<!-- ── Vast.ai / Docker ── -->



  <!-- ── Gaming PC ── -->
  <div class="card">
    <div class="ch">
      <div class="ch-left" style="flex:1;min-height:3rem;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span>🎮 GAMING PC / RTX 5070</span>
          <div style="display:flex;align-items:center;gap:.5rem;">
            <span id="gaming-mine-status" style="font-size:.58rem;padding:.1rem .35rem;border-radius:3px;background:rgba(255,255,255,.05);color:var(--dim)">—</span>
            <span id="gaming-online" style="font-size:.6rem;color:var(--dim)">—</span>
            <span class="staleness" id="stale-gaming"></span>
            <button onclick="rebootGamingToWindows()" id="btn-gaming-win"
              style="font-size:.55rem;padding:.1rem .4rem;background:rgba(0,120,212,.1);border:1px solid rgba(0,120,212,.3);color:#60a5fa;border-radius:3px;cursor:pointer;"
              title="Reboot Gaming PC to Windows">⊞ Windows</button>
            <button onclick="rebootGaming()" id="btn-gaming-reboot"
              style="font-size:.55rem;padding:.1rem .4rem;background:rgba(0,212,255,.05);border:1px solid rgba(0,212,255,.2);color:var(--dim);border-radius:3px;cursor:pointer;"
              title="Reboot Gaming PC (Ubuntu)">↺ Reboot</button>
            <button onclick="rebootGamingFromWindows()" id="btn-gaming-ubuntu"
              style="display:none;font-size:.55rem;padding:.1rem .4rem;background:rgba(255,165,0,.1);border:1px solid rgba(255,165,0,.3);color:#f59e0b;border-radius:3px;cursor:pointer;"
              title="Reboot Gaming PC to Ubuntu">🐧 Ubuntu</button>
          </div>
        </div>
        <div style="margin-top:.3rem;font-size:.65rem;font-weight:600;color:var(--accent);" id="gaming-os">—</div>
      </div>
    </div>
    <div class="cb" id="gaming-body">
      <div class="rg2x4">
        <div class="st"><div class="sl">TEMP</div><div class="sv" id="gaming-temp">—</div><div class="bw"><div class="b" id="gaming-temp-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">POWER</div><div class="sv" id="gaming-power">—</div><div class="bw"><div class="b" id="gaming-power-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">GPU UTIL</div><div class="sv" id="gaming-util">—</div><div class="bw"><div class="b" id="gaming-util-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">VRAM</div><div class="sv" id="gaming-mem">—</div><div class="bw"><div class="b" id="gaming-mem-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">CPU</div><div class="sv" id="gaming-cpu">—</div><div class="bw"><div class="b" id="gaming-cpu-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">RAM</div><div class="sv" id="gaming-ram">—</div><div class="bw"><div class="b" id="gaming-ram-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">DISK</div><div class="sv" id="gaming-disk">—</div><div class="bw"><div class="b" id="gaming-disk-bar" style="width:0%"></div></div></div>
        <div class="st"><div class="sl">HASHRATE</div><div class="sv" id="gaming-hashrate">—</div></div>
      </div>

      </div>
    </div>
  </div>


<!-- ── MINING PANEL ── -->
</div><!-- close auto grid -->
<div style="padding:.9rem 1.25rem;max-width:1500px;margin:0 auto;">
<div class="card" id="mining-panel">
  <div class="ch">
    <div class="ch-left">☁ VAST &nbsp;+&nbsp; ⛏ MINING</div>
    <span class="staleness" id="stale-mining"></span>
  </div>
  <div class="cb">
    <!-- Horizontal coin tabs -->
    <div style="display:flex;gap:.25rem;flex-wrap:wrap;margin-bottom:.75rem;border-bottom:1px solid rgba(255,255,255,.08);padding-bottom:.5rem;">
      <div class="mtab active" id="mtab-VAST" onclick="miningTab('VAST')">
        <span id="mtab-dot-VAST" style="margin-right:.2rem">☁</span>VAST
      </div>
      <div class="mtab" id="mtab-QRL" onclick="miningTab('QRL')">
        <span id="mtab-dot-QRL" style="margin-right:.2rem">○</span>QRL
      </div>
      <div class="mtab" id="mtab-PRL" onclick="miningTab('PRL')">
        <span id="mtab-dot-PRL" style="margin-right:.2rem">○</span>PRL
      </div>
      <div class="mtab" id="mtab-ERG" onclick="miningTab('ERG')">
        <span id="mtab-dot-ERG" style="margin-right:.2rem">○</span>ERG
      </div>
      <div class="mtab" id="mtab-KLS" onclick="miningTab('KLS')">
        <span id="mtab-dot-KLS" style="margin-right:.2rem">○</span>KLS
      </div>
      <div class="mtab" id="mtab-IRON" onclick="miningTab('IRON')">
        <span id="mtab-dot-IRON" style="margin-right:.2rem">○</span>IRON
      </div>
      <div class="mtab" id="mtab-CFX" onclick="miningTab('CFX')">
        <span id="mtab-dot-CFX" style="margin-right:.2rem">○</span>CFX
      </div>
      <div class="mtab" id="mtab-NEXA" onclick="miningTab('NEXA')">
        <span id="mtab-dot-NEXA" style="margin-right:.2rem">○</span>NEXA
      </div>
      <div class="mtab" id="mtab-FLUX" onclick="miningTab('FLUX')">
        <span id="mtab-dot-FLUX" style="margin-right:.2rem">○</span>FLUX
      </div>
    </div>

    <!-- Coin panels -->
    <div id="mpanel-VAST" class="mpanel active">
      <div style="display:grid;grid-template-columns:.85fr 1.1fr 1.4fr;gap:.4rem .75rem;">

        <!-- Col 1: Machine + Pricing combined -->
        <div style="background:rgba(0,212,255,.04);border-radius:6px;padding:.5rem;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.4rem;">
            <span style="font-size:.62rem;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;">Machine &amp; Pricing</span>
            <span id="m-vast-status" style="font-size:.68rem;">—</span>
          </div>
          <div style="display:grid;grid-template-columns:auto 1fr;gap:.2rem .5rem;font-size:.72rem;">
            <span style="color:var(--dim)">ID</span><span id="m-vast-id" style="color:var(--accent);text-align:right">—</span>
            <span style="color:var(--dim)">GPU</span><span id="m-vast-gpu" style="color:var(--text);text-align:right">—</span>
            <span style="color:var(--dim)">GPUs</span><span id="m-vast-gpus" style="color:var(--text);text-align:right">—</span>
            <span style="color:var(--dim)">Driver</span><span id="m-vast-driver" style="color:var(--text);text-align:right">—</span>
            <span style="color:var(--dim)">Location</span><span id="m-vast-listed" style="color:var(--text);text-align:right">—</span>
            <span style="color:var(--dim)">IP</span><span id="m-vast-ip" style="color:var(--dim);text-align:right;font-size:.6rem">—</span>
            <span style="color:var(--dim)">Disk</span><span id="m-vast-disk" style="color:var(--text);text-align:right">—</span>
            <span style="color:var(--dim)">Verified</span><span id="m-vast-veri" style="color:#00ff88;text-align:right">—</span>
            <div style="grid-column:1/-1;border-top:1px solid rgba(255,255,255,.08);margin:.25rem 0;"></div>
            <span style="color:var(--dim)">Reliability</span><span id="m-vast-reliability" style="color:#00ff88;text-align:right;font-weight:600">—</span>
            <span style="color:var(--dim)">On-Demand</span><span id="m-vast-score" style="color:#fbbf24;text-align:right;font-weight:600">—</span>
            <span style="color:var(--dim)">Interruptible</span><span id="m-vast-interruptible" style="color:var(--text);text-align:right">—</span>
            <span style="color:var(--dim)">Net Down</span><span id="m-vast-netd" style="color:var(--text);text-align:right">—</span>
            <span style="color:var(--dim)">Net Up</span><span id="m-vast-netu" style="color:var(--text);text-align:right">—</span>
            <span style="color:var(--dim)">Reports</span><span id="m-vast-reports" style="color:var(--dim);text-align:right">—</span>
          </div>
        </div>

        <!-- Col 3: Active container -->
        <div style="background:rgba(52,211,153,.04);border-radius:6px;padding:.5rem;">
          <div style="font-size:.62rem;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;margin-bottom:.4rem;">Active Container</div>
          <div id="m-vast-container-row" style="display:none;">
            <div style="display:grid;grid-template-columns:auto 1fr;gap:.2rem .5rem;font-size:.72rem;">
              <span style="color:var(--dim)">Name</span><span id="m-vast-container" style="color:var(--accent);text-align:right">—</span>
              <span style="color:var(--dim)">Image</span><span id="m-vast-image" style="color:var(--text);text-align:right;font-size:.58rem;word-break:break-all">—</span>
              <span style="color:var(--dim)">Process</span><span id="m-vast-process" style="color:var(--text);text-align:right;font-size:.6rem">—</span>
              <span style="color:var(--dim)">Running</span><span id="m-vast-running" style="color:var(--text);text-align:right">—</span>
              <span style="color:var(--dim)">Times booked</span><span id="m-vast-times-booked" style="color:#fbbf24;text-align:right;font-weight:600">—</span>
            </div>

          </div>
          <!-- AI container description -->
          <div id="m-vast-ai-desc-wrap" style="display:none;margin-top:.6rem;border-top:1px solid rgba(255,255,255,.07);padding-top:.5rem;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.3rem;">
              <span style="font-size:.6rem;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;">AI Summary</span>
              <button id="m-vast-desc-btn" onclick="updateVastContainerDesc()" style="font-size:.58rem;padding:.15rem .45rem;background:rgba(0,212,255,.1);border:1px solid rgba(0,212,255,.4);color:var(--accent);border-radius:3px;cursor:pointer;line-height:1.4;">&#x21BB; Update</button>
            </div>
            <div id="m-vast-ai-desc" style="font-size:.68rem;color:var(--text);line-height:1.5;opacity:.85;">&#x2014;</div>
          </div>
          <div id="m-vast-no-customer" style="font-size:.65rem;color:var(--dim)">No active customer</div>
          <div style="margin-top:.6rem;" id="m-docker-list"></div>
        </div>

        <!-- Col 4: Hourly occupancy -->
        <div style="background:rgba(167,139,250,.04);border-radius:6px;padding:.5rem;">
          <div style="font-size:.62rem;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;margin-bottom:.3rem;">Minutes Booked / Hour (7d)</div>
          <div style="position:relative;height:200px;"><canvas id="m-vast-hourly-chart"></canvas></div>

        </div>

      </div>
    </div>

    {% for coin in ['QRL','PRL','ERG','KLS','IRON','CFX','NEXA','FLUX'] %}
    <div id="mpanel-{{ coin }}" class="mpanel {% if coin == 'QRL' %}active{% endif %}">
      <!-- Controls row -->
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem;">
        <div style="display:flex;gap:.25rem;align-items:center;">
          {% if coin == 'QRL' %}
          <button class="btn-ctrl btn-sm" id="m-qrl-stop-btn" onclick="xmrigSetMode('force_off')">Stop</button>
          <button class="btn-ctrl btn-sm" id="m-qrl-start-btn" onclick="xmrigSetMode('force_on')">Start</button>
          <button class="btn-ctrl btn-sm" id="m-qrl-auto-btn" onclick="xmrigSetMode('auto')">Auto</button>
          <button class="btn-ctrl btn-sm" id="m-qrl-forceon-btn" onclick="xmrigSetMode('force_on')">Force ON</button>
          <button class="btn-ctrl btn-sm" id="m-qrl-forceoff-btn" onclick="xmrigSetMode('force_off')">Force OFF</button>
          {% elif coin == 'PRL' %}
          <span style="font-size:.6rem;color:var(--dim);margin-right:.3rem;">Ghost:</span>
          <button class="btn-ctrl btn-sm" id="m-prl-ghost-stop" onclick="prlControl('kvm','stop')">Stop</button>
          <button class="btn-ctrl btn-sm" id="m-prl-ghost-start" onclick="prlControl('kvm','start')">Start</button>
          <span style="font-size:.6rem;color:var(--dim);margin-left:.4rem;margin-right:.3rem;">Gaming:</span>
          <button class="btn-ctrl btn-sm" id="m-prl-gaming-stop" onclick="prlControl('gaming','stop')">Stop</button>
          <button class="btn-ctrl btn-sm" id="m-prl-gaming-start" onclick="prlControl('gaming','start')">Start</button>
          {% endif %}
          {% if coin not in ('QRL', 'PRL', 'VAST') %}
          <button class="btn-ctrl btn-sm" id="m-unbl-{{ coin }}"
            onclick="unblacklist('{{ coin }}')"
            style="display:none;border-color:#f59e0b;color:#f59e0b;">⬛ Unblacklist</button>
          {% endif %}
        </div>
        <div id="m-machines-{{ coin }}" style="display:flex;gap:.25rem;font-size:.6rem;"></div>
      </div>

      <!-- Stats + Charts grid -->
      <div style="display:grid;grid-template-columns:.4fr 1fr;gap:.75rem;">
        <!-- Left: stats -->
        <div>
          <div id="m-stats-{{ coin }}" style="font-size:.72rem;"></div>
        </div>
        <!-- Right: 2x2 charts grid -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:.5rem;">
          <div>
            <div style="font-size:.58rem;color:var(--dim);margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.06em;">
              {% if coin == 'QRL' %}24h Hashrate{% elif coin == 'PRL' %}Pool Hashrate{% else %}30d Price (USD){% endif %}
            </div>
            <div style="position:relative;height:70px;">
              <canvas id="m-price-chart-{{ coin }}"></canvas>
            </div>
          </div>
          <div>
            <div style="font-size:.58rem;color:var(--dim);margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.06em;">Balance Growth</div>
            <div style="position:relative;height:70px;">
              <canvas id="m-balance-chart-{{ coin }}"></canvas>
            </div>
          </div>
          <div>
            <div style="font-size:.58rem;color:var(--dim);margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.06em;">
              {% if coin == 'QRL' %}Value (NZD){% elif coin == 'PRL' %}Value (NZD){% else %}Hashrate 24h{% endif %}
            </div>
            <div style="position:relative;height:70px;">
              <canvas id="m-hr-chart-{{ coin }}"></canvas>
            </div>
          </div>
          <div>
            <div style="font-size:.58rem;color:var(--dim);margin-bottom:.2rem;text-transform:uppercase;letter-spacing:.06em;">Earnings / Day (NZD)</div>
            <div style="position:relative;height:70px;">
              <canvas id="m-earnings-chart-{{ coin }}"></canvas>
            </div>
          </div>
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
</div>
<!-- ── END MINING PANEL ── -->
</div><!-- close mining wrapper -->

<!-- ── Bottom: Charts + Jobs (unchanged from v5) ── -->
<div class="bottom">
  <div class="bigcard">
    <div class="otabs">
      <div class="otab active" onclick="switchOuter('charts',this)">📈 Customer Charts</div>
      <div class="otab" onclick="switchOuter('jobs',this)">📋 Job History</div>
    </div>

    <div id="opanel-charts" class="opanel active">
      <div class="vtab-wrap">
        <div class="vtabs" id="vtabs">
          {% for c in charts %}
          <div class="vtab {% if loop.first %}active{% endif %}" onclick="switchCustomer({{ loop.index0 }}, this)">
            <div class="vtab-name">{{ c.name[-9:] }}</div>
            <div class="vtab-img" title="{{ c.image }}">{{ c.short_image }}</div>
	    <div><span id="vtab-badge-{{ loop.index0 }}" class="vtab-badge {% if c.active %}live{% else %}done{% endif %}"{% if c.active %} data-started="{{ c.started_iso }}"{% endif %}>{% if c.active %}● LIVE{% else %}{{ c.duration if c.duration != '—' else 'done' }}{% endif %}</span>{% if c.is_image_gen %}<span class="vtab-badge img">IMG</span>{% endif %}</div>
          </div>
          {% endfor %}
          {% if not charts %}<div style="padding:.75rem;color:var(--dim);font-size:.7rem;">No data</div>{% endif %}
        </div>

        {% for c in charts %}
        <div id="vpanel-{{ loop.index0 }}" class="vpanel {% if loop.first %}active{% endif %}">
          <div class="cust-meta">
            <div class="cust-meta-item"><span class="cust-meta-label">Container</span><span class="cust-meta-val">{{ c.name }}</span></div>
            <div class="cust-meta-item"><span class="cust-meta-label">Type</span><span class="cust-meta-val">{{ c.type }}</span></div>
            {% if c.get('runtype') %}<div class="cust-meta-item"><span class="cust-meta-label">Mode</span><span class="cust-meta-val" style="color:{% if 'interruptible' in c.get('runtype','').lower() %}#f59e0b{% elif 'demand' in c.get('runtype','').lower() %}#00ff88{% else %}var(--text){% endif %}">{{ c.get('runtype','—') }}</span></div>{% endif %}
            <div class="cust-meta-item"><span class="cust-meta-label">Started</span><span class="cust-meta-val">{{ c.started }}</span></div>
            <div class="cust-meta-item"><span class="cust-meta-label">Finished</span><span class="cust-meta-val">{{ c.finished }}</span></div>
            {% if c.duration != '—' %}<div class="cust-meta-item"><span class="cust-meta-label">Duration</span><span class="cust-meta-val">{{ c.duration }}</span></div>{% endif %}
          </div>
          {% if c.has_data %}

          <div class="chart-box" id="procs-box-{{ loop.index0 }}" style="margin-top:.75rem;">
            <div class="chart-title" style="display:flex;align-items:center;justify-content:space-between;">
              <span>⚙️ Live Processes</span>
              <button onclick="analyseContainer('{{ c.name }}', {{ loop.index0 }})" 
                style="font-family:var(--mono);font-size:.55rem;padding:2px 8px;background:rgba(0,212,255,0.08);border:1px solid rgba(0,212,255,0.3);color:var(--accent);cursor:pointer;letter-spacing:.1em;border-radius:2px;"
                id="analyse-btn-{{ loop.index0 }}">⬡ ANALYSE</button>
            </div>
            <div id="analyse-collapse-{{ loop.index0 }}" style="display:none;margin:.4rem 0;">
              <div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;padding:.2rem .4rem;background:rgba(0,212,255,0.08);border-radius:3px 3px 0 0;" onclick="toggleAnalyse({{ loop.index0 }})">
                <span style="font-size:.62rem;color:var(--accent);font-weight:600;">⬡ ANALYSIS</span>
                <span id="analyse-toggle-{{ loop.index0 }}" style="font-size:.62rem;color:var(--dim);">▼</span>
              </div>
              <div id="analyse-result-{{ loop.index0 }}" style="padding:.6rem .8rem;background:rgba(0,212,255,0.05);border-left:2px solid var(--accent);font-size:.82rem;color:var(--text);line-height:1.6;border-radius:0 0 3px 3px;"></div>
              <div id="analyse-history-{{ loop.index0 }}" style="padding:.3rem .8rem;background:rgba(255,255,255,0.02);border-left:2px solid var(--dim);font-size:.62rem;color:var(--dim);display:none;"></div>
            </div>
            <div id="chart-procs-{{ loop.index0 }}" style="font-size:.6rem;color:var(--text);overflow-y:auto;max-height:200px;padding:.3rem 0;"></div>
          </div>

          <div class="chart-row">
            <div class="chart-box" style="height:250px"><div class="chart-title">🌡 Temperature &amp; Fan (%)</div><canvas id="chart-tf-{{ loop.index0 }}" style="height:180px"></canvas></div>
            <div class="chart-box" style="height:250px"><div class="chart-title">⚡ Power % &amp; GPU Util %</div><canvas id="chart-pu-{{ loop.index0 }}" style="height:180px"></canvas></div>
          </div>
          <div class="chart-row">
            <div class="chart-box" style="height:250px"><div class="chart-title">💾 VRAM (GB)</div><canvas id="chart-vr-{{ loop.index0 }}" style="height:180px"></canvas></div>
            <div class="chart-box" style="height:250px"><div class="chart-title">🖥 CPU % &amp; RAM %</div><canvas id="chart-cr-{{ loop.index0 }}" style="height:180px"></canvas></div>
          </div>

          {% else %}
          <div class="no-data">No telemetry data for this session</div>
          {% endif %}


          <div class="chart-box" style="margin-top:.75rem;">
            <div class="chart-title">🔊 Audio Output</div>
            <div id="audio-panel-{{ loop.index0 }}" style="font-size:.7rem;color:var(--dim);">
              <button onclick="loadAudio('{{ c.name }}', {{ loop.index0 }})" style="font-size:.6rem;padding:2px 8px;cursor:pointer;background:var(--bg3);border:1px solid var(--border);border-radius:3px;color:var(--text);margin-bottom:.4rem;">🔄 Load Files</button>
              <div id="audio-list-{{ loop.index0 }}">—</div>
            </div>
          </div>

        </div>
        {% endfor %}
        {% if not charts %}<div class="vpanel active" style="flex:1;"><div class="no-data">No customer sessions found</div></div>{% endif %}
      </div>
    </div>

    <div id="opanel-jobs" class="opanel">
      <div class="jobpanel" style="width:100%;">
        {% if all_containers %}
        <table>
          <thead><tr><th>Container</th><th>Type</th><th>Image</th><th>Started (NZST)</th><th>Finished (NZST)</th><th>Duration</th><th>Exit</th></tr></thead>
          <tbody>
            {% for c in all_containers %}
            <tr>
              <td style="color:var(--accent);white-space:nowrap">{{ c.name }}</td>
              <td>{% set t = c.get('type','') %}{% set rt = c.get('runtype','') %}
                {% if 'SERVERLESS' in t %}<span class="tt sv2">serverless</span>
                {% elif 'JUPYTER' in t %}<span class="tt jup">jupyter</span>
                {% elif 'SSH' in t %}<span class="tt ssh">ssh</span>
                {% else %}<span style="color:var(--dim)">{{ t }}</span>{% endif %}
                {% if rt and rt != '—' %}<span style="font-size:.6rem;color:var(--dim);margin-left:.3rem;">({{ rt }})</span>{% endif %}
              </td>
              <td class="dim" style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{{ c.get('image','') }}">{{ c.get('image','—')[:38] }}{% if c.get('image','')|length > 38 %}…{% endif %}</td>
              <td class="dim" style="white-space:nowrap">{{ c.get('started','—') }}</td>
              <td class="dim" style="white-space:nowrap">{{ c.get('finished','—') }}</td>
              <td style="white-space:nowrap">{{ c.get('duration','—') }}</td>
              <td>{% set ec = c.get('exit_code','') %}
                {% if ec == '0' %}<span style="color:var(--green)">0</span>
                {% elif ec == '137' %}<span style="color:var(--dim)">137</span>
                {% elif ec %}<span style="color:var(--red)">{{ ec }}</span>
                {% else %}<span style="color:var(--dim)">—</span>{% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
        {% else %}<div class="no-data">No container history available</div>{% endif %}
      </div>
    </div>
  </div>
</div>

<script>
// ── CHART DATA (baked in at page load, static) ──────────────────────────────
const CHART_DATA = {{ chart_data_json|safe }};
const BASE = {responsive:true,maintainAspectRatio:false,animation:false,
  plugins:{
    legend:{labels:{color:'#4a6080',font:{family:'Share Tech Mono',size:10},boxWidth:10,padding:8}},
    tooltip:{backgroundColor:'#111827',borderColor:'#1e2d4a',borderWidth:1,
      titleColor:'#c8d8f0',bodyColor:'#c8d8f0',
      titleFont:{family:'Share Tech Mono',size:10},bodyFont:{family:'Share Tech Mono',size:10}}
  },
  scales:{
    x:{ticks:{color:'#4a6080',font:{family:'Share Tech Mono',size:9},maxTicksLimit:8,maxRotation:0},grid:{color:'rgba(30,45,74,0.5)'}},
    y:{ticks:{color:'#4a6080',font:{family:'Share Tech Mono',size:9}},grid:{color:'rgba(30,45,74,0.5)'}}
  }
};

const _charts = {};
function mkChart(id,labels,datasets,yMax){
  const el=document.getElementById(id);if(!el)return null;
  if(_charts[id]){_charts[id].destroy();}
  const cfg=JSON.parse(JSON.stringify(BASE));

  if(yMax){cfg.scales.y.max=yMax;cfg.scales.y.min=0;}

  el.style.height='200px';
  el.style.width='100%';
  el.style.display='block';
  const c=new Chart(el,{type:'line',data:{labels,datasets},options:cfg});

  _charts[id]=c;
  return c;
}
// ── XMRig 24hr chart ──────────────────────────────────────────
let _xmrigChart = null;
const _xmrigHr = [];
const _xmrigCores = [];
const _xmrigLabels = [];

function initXmrigChart() {
  const el = document.getElementById('xmrig-chart'); if (!el) return;
  if (!el) return;
  if (_xmrigChart) { _xmrigChart.destroy(); }
  _xmrigChart = new Chart(el, {
    data: {
      labels: _xmrigLabels,
      datasets: [
        {
          type: 'line',
          label: 'H/s',
          data: _xmrigHr,
          borderColor: '#00d4ff',
          backgroundColor: 'rgba(0,212,255,.08)',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.3,
          fill: true,
          yAxisID: 'y'
        },
        {
          type: 'bar',
          label: 'Cores',
          data: _xmrigCores,
          backgroundColor: 'rgba(124,58,237,.4)',
          borderWidth: 0,
          yAxisID: 'y2'
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { display: false },
        y: {
          position: 'left',
          min: 0,
          grid: { color: 'rgba(255,255,255,.05)' },
          ticks: { color: '#4a6080', font: { size: 9 }, maxTicksLimit: 4,
                   callback: v => v >= 1000 ? (v/1000).toFixed(1)+'K' : v }
        },
        y2: {
          position: 'right',
          min: 0, max: 24,
          grid: { display: false },
          ticks: { color: '#4a6080', font: { size: 9 }, maxTicksLimit: 3 }
        }
      }
    }
  });
}

async function updateXmrigChart() {
  try {
    const r = await fetch('/api/xmrig_history');
    if (!r.ok) return;
    const data = await r.json();
    _xmrigLabels.length = 0;
    _xmrigHr.length = 0;
    _xmrigCores.length = 0;
    for (const p of data) {
      const d = new Date(p.t * 1000);
      _xmrigLabels.push(d.toLocaleTimeString('en-NZ', {hour:'2-digit', minute:'2-digit', hour12:false}));
      _xmrigHr.push(p.hr);
      _xmrigCores.push(p.cores);
    }
    if (!_xmrigChart) initXmrigChart();
    else _xmrigChart.update('none');
  } catch(e) { console.warn('XMRig chart update failed:', e); }
}

// ── Wallet tab switchers ─────────────────────────────────────
function kvmWalletTab(coin) {
  ['cfx','kls','erg','iron','prl'].forEach(c => {
    document.getElementById('kvm-wtab-'+c).classList.remove('active','mining');
    document.getElementById('kvm-wpanel-'+c).classList.remove('active');
  });
  document.getElementById('kvm-wtab-'+coin).classList.add('active');
  document.getElementById('kvm-wpanel-'+coin).classList.add('active');
}

function gamingWalletTab(coin) {
  ['erg','cfx','flux','top10','prl'].forEach(c => {
    document.getElementById('gaming-wtab-'+c) && document.getElementById('gaming-wtab-'+c).classList.remove('active','mining');
    document.getElementById('gaming-wpanel-'+c) && document.getElementById('gaming-wpanel-'+c).classList.remove('active');
  });
  document.getElementById('gaming-wtab-'+coin).classList.add('active');
  document.getElementById('gaming-wpanel-'+coin).classList.add('active');
}

function setKvmMiningTab(coin) {
  ['cfx','kls','erg','iron'].forEach(c => {
    const t = document.getElementById('kvm-wtab-'+c);
    if (t) t.classList.remove('mining');
  });
  const active = document.getElementById('kvm-wtab-'+coin);
  if (active) active.classList.add('mining');
}

// Init and poll every 60s
initXmrigChart();
updateXmrigChart();
setInterval(updateXmrigChart, 60000);

// ── KVM lolMiner 24hr chart ─────────────────────────────────
let _kvmLolChart = null;
const _kvmLolHr = [];
const _kvmLolLabels = [];

function initKvmLolChart() {
  const el = document.getElementById('kvm-lol-chart');
  if (!el) return;
  if (_kvmLolChart) { _kvmLolChart.destroy(); }
  _kvmLolChart = new Chart(el, {
    type: 'line',
    data: {
      labels: _kvmLolLabels,
      datasets: [{
        label: 'H/s',
        data: _kvmLolHr,
        borderColor: '#00ff88',
        backgroundColor: 'rgba(0,255,136,.08)',
        borderWidth: 1.5,
        pointRadius: 0,
        tension: 0.3,
        fill: true
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { display: false },
        y: {
          min: 0,
          grid: { color: 'rgba(255,255,255,.05)' },
          ticks: { color: '#4a6080', font: { size: 9 }, maxTicksLimit: 4,
                   callback: v => v >= 1000000 ? (v/1000000).toFixed(1)+'M' : v >= 1000 ? (v/1000).toFixed(1)+'K' : v }
        }
      }
    }
  });
}

async function updateKvmLolChart() {
  try {
    const r = await fetch('/api/kvm_lol_history');
    if (!r.ok) return;
    const data = await r.json();
    _kvmLolLabels.length = 0;
    _kvmLolHr.length = 0;
    for (const p of data) {
      const d = new Date(p.t * 1000);
      _kvmLolLabels.push(d.toLocaleTimeString('en-NZ', {hour:'2-digit', minute:'2-digit', hour12:false}));
      _kvmLolHr.push(p.hr);
    }
    if (!_kvmLolChart) initKvmLolChart();
    else _kvmLolChart.update('none');
  } catch(e) { console.warn('KVM lol chart update failed:', e); }
}

initKvmLolChart();
updateKvmLolChart();
setInterval(updateKvmLolChart, 60000);

function buildCharts(idx){
  const d=CHART_DATA[idx];if(!d||!d.has_data)return;
  const L=d.labels,tp=d.throttles.map((t,i)=>t?d.temps[i]:null);
  mkChart('chart-tf-'+idx,L,[
    {label:'Temp °C',data:d.temps,borderColor:'#ff4444',backgroundColor:'rgba(255,68,68,.08)',borderWidth:1.5,pointRadius:0,tension:0.3,fill:true},
    {label:'Fan %',data:d.fans,borderColor:'#00d4ff',backgroundColor:'transparent',borderWidth:1.5,pointRadius:0,tension:0.3,borderDash:[4,2]},
    {label:'Throttle',data:tp,borderColor:'transparent',backgroundColor:'rgba(255,68,68,.7)',pointRadius:4,pointStyle:'triangle',showLine:false}
  ],100);
  mkChart('chart-pu-'+idx,L,[
    {label:'Power %',data:d.powers,borderColor:'#fbbf24',backgroundColor:'rgba(251,191,36,.08)',borderWidth:1.5,pointRadius:0,tension:0.3,fill:true},
    {label:'GPU Util %',data:d.utils,borderColor:'#7c3aed',backgroundColor:'transparent',borderWidth:1.5,pointRadius:0,tension:0.3}
  ],100);
  mkChart('chart-vr-'+idx,L,[
    {label:'VRAM GB',data:d.vrams,borderColor:'#00ff88',backgroundColor:'rgba(0,255,136,.08)',borderWidth:1.5,pointRadius:0,tension:0.3,fill:true}
  ],32);
  mkChart('chart-cr-'+idx,L,[
    {label:'CPU %',data:d.cpus,borderColor:'#00d4ff',backgroundColor:'rgba(0,212,255,.08)',borderWidth:1.5,pointRadius:0,tension:0.3,fill:true},
    {label:'RAM %',data:d.rams,borderColor:'#a78bfa',backgroundColor:'transparent',borderWidth:1.5,pointRadius:0,tension:0.3}
  ],100);
}
function updateCharts(idx, d){
  if(!d||!d.has_data)return;
  const L=d.labels,tp=d.throttles.map((t,i)=>t?d.temps[i]:null);
  [
    ['chart-tf-'+idx, [d.temps,d.fans,tp], 100],
    ['chart-pu-'+idx, [d.powers,d.utils], 100],
    ['chart-vr-'+idx, [d.vrams], 32],
    ['chart-cr-'+idx, [d.cpus,d.rams], 100]
  ].forEach(([id,datasets])=>{
    const c=_charts[id];if(!c)return;
    c.data.labels=L;
    datasets.forEach((ds,i)=>{if(c.data.datasets[i])c.data.datasets[i].data=ds;});
    c.update('none');
  });
}

let built={};

function toggleAnalyse(idx) {
  const resultEl  = document.getElementById('analyse-result-' + idx);
  const histEl    = document.getElementById('analyse-history-' + idx);
  const toggleEl  = document.getElementById('analyse-toggle-' + idx);
  if (!resultEl) return;
  const hidden = resultEl.style.display === 'none';
  resultEl.style.display  = hidden ? 'block' : 'none';
  if (histEl) histEl.style.display = hidden ? (histEl.innerHTML ? 'block' : 'none') : 'none';
  if (toggleEl) toggleEl.textContent = hidden ? '▼' : '▶';
}

async function analyseContainer(name, idx) {
  const btn = document.getElementById('analyse-btn-' + idx);
  const resultEl = document.getElementById('analyse-result-' + idx);
  if (!btn || !resultEl) return;

  // Gather current process data
  const sessionKey = CHART_DATA[idx] && CHART_DATA[idx].name + ':' + (CHART_DATA[idx].started || 'unknown');
  const history = window._procHistory && window._procHistory[sessionKey];
  if (!history || Object.keys(history).length === 0) {
    resultEl.style.display = 'block';
    resultEl.textContent = 'No process data available yet.';
    return;
  }

  const processes = Object.values(history)
    .filter(p => p.active || p.lastPct > 0.5)
    .sort((a, b) => (b.lastPct || 0) - (a.lastPct || 0))
    .slice(0, 15)
    .map(p => ({ pct: (p.lastPct || 0).toFixed(1), cmd: p.cmd }));

  const image = CHART_DATA[idx] && CHART_DATA[idx].image || 'unknown';
  const type = CHART_DATA[idx] && CHART_DATA[idx].type || 'unknown';

  const collapseEl = document.getElementById('analyse-collapse-' + idx);
  btn.textContent = '⟳ ANALYSING...';
  btn.disabled = true;
  if (collapseEl) collapseEl.style.display = 'block';
  resultEl.style.display = 'block';
  resultEl.textContent = 'Asking Claude...';

  try {
    const r = await fetch('/api/analyse/' + name, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({processes, image, type})
    });
    const d = await r.json();
    if (d.analysis) {
      const ts = new Date().toLocaleTimeString();
      const entry = { ts, text: d.analysis, cost: d.cost_usd };

      // Store in session history
      if (!window._analyseHistory) window._analyseHistory = {};
      if (!window._analyseHistory[idx]) window._analyseHistory[idx] = [];
      window._analyseHistory[idx].unshift(entry);

      // Persist to server
      fetch('/api/analyse/save', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name, entry: entry})
      });

      // Show latest
      resultEl.innerHTML = d.analysis.split(String.fromCharCode(10)).join('<br>') +
        (d.cost_usd ? `<div style="margin-top:.5rem;font-size:.6rem;color:var(--dim);">` +
        `${ts} &nbsp;·&nbsp; $${d.cost_usd} &nbsp;·&nbsp; session: $${d.total_usd}</div>` : '');

      // Show history if more than 1
      const histEl = document.getElementById('analyse-history-' + idx);
      if (histEl && window._analyseHistory[idx].length > 1) {
        histEl.style.display = 'block';
        histEl.innerHTML = '<div style="margin-bottom:.2rem;color:var(--accent)">▸ Previous analyses</div>' +
          window._analyseHistory[idx].slice(1).map(e =>
            `<div style="margin-bottom:.4rem;padding-bottom:.4rem;border-bottom:1px solid rgba(255,255,255,.05)">` +
            `<span style="color:var(--dim)">${e.ts}</span><br>${e.text.split(String.fromCharCode(10)).join('<br>')}</div>`
          ).join('');
      }
    } else {
      resultEl.textContent = d.error || 'No response';
    }
  } catch(e) {
    resultEl.textContent = 'Analysis failed: ' + e.message;
  } finally {
    btn.textContent = '⬡ ANALYSE';
    btn.disabled = false;
  }
}

async function loadAudio(containerName, idx) {
  const listEl = document.getElementById('audio-list-' + idx);
  listEl.innerHTML = 'Loading...';
  try {
    const r = await fetch('/api/container/' + containerName + '/audio');
    const data = await r.json();
    if (!data.files || data.files.length === 0) {
      listEl.innerHTML = 'No audio files found';
      return;
    }
    listEl.innerHTML = data.files.map(f => `
      <div style="margin-bottom:.5rem;border-bottom:1px solid var(--border);padding-bottom:.4rem;">
        <div style="font-size:.65rem;color:var(--text);margin-bottom:.3rem;">${f.name} <span style="color:var(--dim)">${f.date} · ${Math.round(f.size/1024)}KB</span></div>
        <audio controls style="width:100%;height:28px;" src="/api/container/${containerName}/audio${f.dir}/${f.name}"></audio>
      </div>`).join('');
  } catch(e) {
    listEl.innerHTML = 'Error loading files';
  }
}

function renderHistoryProcs(idx, sessionKey) {
  const SPARK_W_H = 320;
  const procsEl = document.getElementById('chart-procs-' + idx);
  if(!procsEl || !window._procHistory || !window._procHistory[sessionKey]) return;
  procsEl.innerHTML = '';
  const tsHeader = document.createElement('div');
  tsHeader.style.cssText = 'font-size:.58rem;color:var(--dim);margin-bottom:6px;';
  tsHeader.textContent = 'Restored from history';
  procsEl.appendChild(tsHeader);
  const allKeys = Object.keys(window._procHistory[sessionKey]);
  allKeys.sort((a,b) => {
    const ha = window._procHistory[sessionKey][a];
    const hb = window._procHistory[sessionKey][b];
    if(ha.active && !hb.active) return -1;
    if(!ha.active && hb.active) return 1;
    return (hb.lastPct||0) - (ha.lastPct||0);
  });
  allKeys.forEach(key => {
    const ph = window._procHistory[sessionKey][key];
    const hist = ph.hist || [];
    const max = Math.max(...hist, 1);
    const pDiv = document.createElement('div');
    pDiv.style.cssText = `display:grid;grid-template-columns:55px 1fr ${SPARK_W_H}px;gap:6px;align-items:center;margin-bottom:3px;`;
    const pctDiv = document.createElement('div');
    const lastPct = ph.lastPct || 0;
    const pctColor = !ph.active ? 'var(--dim)' : lastPct > 50 ? '#ff4444' : lastPct > 10 ? '#fbbf24' : '#00ff88';
    pctDiv.style.cssText = `font-size:.6rem;color:${pctColor};text-align:right;white-space:nowrap;`;
    pctDiv.textContent = ph.active ? lastPct.toFixed(1) + '%' : 'stopped';
    const cmdDiv = document.createElement('div');
    cmdDiv.style.cssText = `font-size:.6rem;color:${ph.active ? 'var(--text)' : 'var(--dim)'};word-break:break-all;line-height:1.4;`;
    cmdDiv.textContent = ph.cmd;
    const sparkDiv = document.createElement('div');
    sparkDiv.style.cssText = 'display:flex;align-items:flex-end;gap:2px;height:24px;';
    hist.forEach(v => {
      const bar = document.createElement('div');
      const h = Math.max(2, Math.round((v/max)*24));
      const color = !ph.active ? '#4a6080' : v>50 ? '#ff4444' : v>10 ? '#fbbf24' : '#00ff88';
      bar.style.cssText = `width:12px;height:${h}px;background:${color};opacity:0.8;border-radius:1px;`;
      sparkDiv.appendChild(bar);
    });
    pDiv.appendChild(pctDiv);
    pDiv.appendChild(cmdDiv);
    pDiv.appendChild(sparkDiv);
    procsEl.appendChild(pDiv);
  });
}

function switchCustomer(idx,el){
  document.querySelectorAll('.vpanel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('#vtabs .vtab').forEach(t=>t.classList.remove('active'));
  document.getElementById('vpanel-'+idx).classList.add('active');
  el.classList.add('active');
  if(!built[idx]){buildCharts(idx);built[idx]=true;}
  // Re-render history procs if available and panel is empty
  const procsEl = document.getElementById('chart-procs-' + idx);
  const hasRealContent = procsEl && procsEl.querySelectorAll && procsEl.querySelectorAll('div > div').length > 1;
  if(procsEl && !hasRealContent) {
    const name = CHART_DATA[idx] && CHART_DATA[idx].name;
    const started = CHART_DATA[idx] && CHART_DATA[idx].started;
    if(name) {
      const sessionKey = name + ':' + (started || 'unknown');
      if(window._procHistory && window._procHistory[sessionKey]) {
        renderHistoryProcs(idx, sessionKey);
      } else {
        // Fetch and render
        fetch('/api/procs/history/' + encodeURIComponent(sessionKey.replace(':','_')))
          .then(r => r.json())
          .then(data => {
            if(data.history && Object.keys(data.history).length > 0) {
              if(!window._procHistory) window._procHistory = {};
              if(!window._procAllKeys) window._procAllKeys = {};
              window._procHistory[sessionKey] = data.history;
              window._procAllKeys[sessionKey] = Object.keys(data.history);
              renderHistoryProcs(idx, sessionKey);
            }
          }).catch(()=>{});
      }
    }
  }
}
function switchOuter(name,el){
  document.querySelectorAll('.opanel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.otab').forEach(t=>t.classList.remove('active'));
  document.getElementById('opanel-'+name).classList.add('active');
  el.classList.add('active');
}
// ── Dynamic Vtab Injection ──────────────────────────────────────────────────
function injectVtab(idx, d) {
  const vtabs = document.getElementById('vtabs');
  if (!vtabs) return;
  const div = document.createElement('div');
  div.className = 'vtab';
  div.id = 'vtab-' + idx;
  div.onclick = function() { switchCustomer(idx, this); };
  const shortImg = (d.short_image || d.image || '').slice(0, 22);
  const isImg = d.is_image_gen;
  div.innerHTML = `
    <div class="vtab-name">${(d.name || '').slice(-9)}</div>
    <div class="vtab-img" title="${d.image || ''}">${shortImg}</div>
    <div>
      <span id="vtab-badge-${idx}" class="vtab-badge ${d.active ? 'live' : 'done'}" ${d.active ? 'data-started="' + (d.started_iso || '') + '"' : ''}>
        ${d.active ? '● LIVE' : (d.duration && d.duration !== '—' ? d.duration : 'done')}
      </span>
      ${isImg ? '<span class="vtab-badge img">IMG</span>' : ''}
    </div>`;
  // Insert at top (newest first)
  vtabs.insertBefore(div, vtabs.firstChild);
}

function injectVpanel(idx, d) {
  const wrap = document.querySelector('.vtab-wrap');
  if (!wrap) return;
  const existing = document.getElementById('vpanel-' + idx);
  if (existing) return;

  const nzst = t => {
    if (!t || t === '—') return '—';
    try {
      return new Date(t).toLocaleString('en-NZ', {timeZone:'Pacific/Auckland',
        month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
    } catch(e) { return t; }
  };

  const typeLabel = (() => {
    const t = (d.type || '').toUpperCase();
    if (t.includes('SERVERLESS')) return '<span class="tt sv2">serverless</span>';
    if (t.includes('JUPYTER'))    return '<span class="tt jup">jupyter</span>';
    if (t.includes('SSH'))        return '<span class="tt ssh">ssh</span>';
    return `<span style="color:var(--dim)">${d.type || '—'}</span>`;
  })();

  const panel = document.createElement('div');
  panel.className = 'vpanel';
  panel.id = 'vpanel-' + idx;
  panel.innerHTML = `
    <div class="cust-meta" style="display:flex;flex-wrap:wrap;gap:.4rem .75rem;padding:.5rem 0 .75rem;border-bottom:1px solid var(--border);margin-bottom:.5rem;">
      <div class="cust-meta-item"><span class="cust-meta-label">Container</span><span class="cust-meta-val" style="color:var(--accent)">${d.name || '—'}</span></div>
      <div class="cust-meta-item"><span class="cust-meta-label">Type</span><span class="cust-meta-val">${typeLabel}</span></div>
      ${(() => {
        const occup = (window._lastVastOccup || '');
        const isActive = d.active;
        let modeLabel, modeColor;
        if (isActive && occup) {
          modeLabel = occup === 'I_' ? 'interruptible' : occup === 'D_' ? 'demand' : (d.runtype || '—');
          modeColor = occup === 'I_' ? '#f59e0b' : '#00ff88';
        } else {
          // Map runtype to readable label for completed containers
          const rtMap = {'args':'serverless','ssh':'ssh','jupyter_direc':'jupyter','jupyter':'jupyter'};
          modeLabel = rtMap[d.runtype] || d.runtype || '—';
          modeColor = 'var(--dim)';
        }
        return `<div class="cust-meta-item"><span class="cust-meta-label">Mode</span><span class="cust-meta-val" style="color:${modeColor}">${modeLabel}</span></div>`;
      })()}
      <div class="cust-meta-item"><span class="cust-meta-label">Started</span><span class="cust-meta-val">${d.started || nzst(d.started_iso)}</span></div>
      <div class="cust-meta-item"><span class="cust-meta-label">Finished</span><span class="cust-meta-val" id="vpanel-finished-${idx}">${d.active ? '—' : (d.finished || '—')}</span></div>
      <div class="cust-meta-item"><span class="cust-meta-label">Duration</span><span class="cust-meta-val" id="vpanel-duration-${idx}">${d.duration || '—'}</span></div>
    </div>
    <div class="chart-box" id="procs-box-${idx}" style="margin-top:.75rem;">
      <div class="chart-title" style="display:flex;align-items:center;justify-content:space-between;">
        <span>⚙️ Live Processes</span>
        <button onclick="analyseContainer('${d.name}', ${idx})"
          style="font-family:var(--mono);font-size:.55rem;padding:2px 8px;background:rgba(0,212,255,0.08);border:1px solid rgba(0,212,255,0.3);color:var(--accent);cursor:pointer;letter-spacing:.1em;border-radius:2px;"
          id="analyse-btn-${idx}">⬡ ANALYSE</button>
      </div>
      <div id="analyse-collapse-${idx}" style="display:none;margin:.4rem 0;">
        <div style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;padding:.2rem .4rem;background:rgba(0,212,255,0.08);border-radius:3px 3px 0 0;" onclick="toggleAnalyse(${idx})">
          <span style="font-size:.62rem;color:var(--accent);font-weight:600;">⬡ ANALYSIS</span>
          <span id="analyse-toggle-${idx}" style="font-size:.62rem;color:var(--dim);">▼</span>
        </div>
        <div id="analyse-result-${idx}" style="padding:.6rem .8rem;background:rgba(0,212,255,0.05);border-left:2px solid var(--accent);font-size:.82rem;color:var(--text);line-height:1.6;border-radius:0 0 3px 3px;"></div>
        <div id="analyse-history-${idx}" style="padding:.3rem .8rem;background:rgba(255,255,255,0.02);border-left:2px solid var(--dim);font-size:.62rem;color:var(--dim);display:none;"></div>
      </div>
      <div id="chart-procs-${idx}" style="font-size:.6rem;color:var(--text);overflow-y:auto;max-height:200px;padding:.3rem 0;">
        <span style="color:var(--dim);font-size:.62rem;">Click tab to load process data</span>
      </div>
    </div>
    <div class="chart-row">
      <div class="chart-box" style="height:250px"><div class="chart-title">🌡 Temperature &amp; Fan (%)</div><canvas id="chart-tf-${idx}" style="height:180px"></canvas></div>
      <div class="chart-box" style="height:250px"><div class="chart-title">⚡ Power % &amp; GPU Util %</div><canvas id="chart-pu-${idx}" style="height:180px"></canvas></div>
    </div>
    <div class="chart-row">
      <div class="chart-box" style="height:250px"><div class="chart-title">💾 VRAM (GB)</div><canvas id="chart-vr-${idx}" style="height:180px"></canvas></div>
      <div class="chart-box" style="height:250px"><div class="chart-title">🖥 CPU % &amp; RAM %</div><canvas id="chart-cr-${idx}" style="height:180px"></canvas></div>
    </div>`;
  wrap.appendChild(panel);
}

// ── Live Elapsed Timer ───────────────────────────────────────────────────────
function fmtElapsed(isoStart) {
  if (!isoStart) return '● LIVE';
  try {
    const ms = Date.now() - new Date(isoStart).getTime();
    if (ms < 0) return '● LIVE';
    const s = Math.floor(ms / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const ss = s % 60;
    if (h > 0) return `● ${h}h ${String(m).padStart(2,'0')}m`;
    return `● ${String(m).padStart(2,'0')}m ${String(ss).padStart(2,'0')}s`;
  } catch(e) { return '● LIVE'; }
}

function tickElapsedTimers() {
  document.querySelectorAll('.vtab-badge.live').forEach(badge => {
    const started = badge.dataset.started;
    if (started) badge.textContent = fmtElapsed(started);
  });
}

// Tick every second
setInterval(tickElapsedTimers, 1000);

async function rebootGamingFromWindows() {
  const btn = document.getElementById('btn-gaming-ubuntu');
  if (!confirm('Reboot Gaming PC to Ubuntu?')) return;
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    const r = await fetch('/api/control/gaming/reboot-from-windows', {method:'POST'});
    if (btn) { btn.textContent = '✓ Rebooting'; }
    setTimeout(() => { if(btn) { btn.textContent = '🐧 Ubuntu'; btn.disabled = false; }}, 5000);
  } catch(e) {
    if (btn) { btn.textContent = '✗ Failed'; btn.disabled = false; }
  }
}

async function rebootGaming() {
  const btn = document.getElementById('btn-gaming-reboot');
  if (!confirm('Reboot Gaming PC?')) return;
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    const r = await fetch('/api/control/gaming/reboot', {method:'POST'});
    if (btn) { btn.textContent = '✓ Rebooting'; }
    setTimeout(() => { if(btn) { btn.textContent = '↺ Reboot'; btn.disabled = false; }}, 5000);
  } catch(e) {
    if (btn) { btn.textContent = '✗ Failed'; btn.disabled = false; }
  }
}

async function rebootGamingToWindows() {
  const btn = document.getElementById('btn-gaming-win');
  if (!confirm('Reboot Gaming PC to Windows?')) return;
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    const r = await fetch('/api/control/gaming/reboot-windows', {method:'POST'});
    const d = await r.json();
    if (btn) { btn.textContent = '✓ Rebooting'; }
    setTimeout(() => { if(btn) { btn.textContent = '⊞ Windows'; btn.disabled = false; }}, 5000);
  } catch(e) {
    if (btn) { btn.textContent = '✗ Failed'; btn.disabled = false; }
  }
}

function switchMini(name,el){
  const card=el.closest('.card');
  card.querySelectorAll('.mpanel').forEach(p=>p.classList.remove('active'));
  card.querySelectorAll('.mtab').forEach(t=>t.classList.remove('active'));
  const panel=card.querySelector('#mpanel-'+name);
  if(panel)panel.classList.add('active');
  el.classList.add('active');
}

window.addEventListener('load', async () => {
  buildCharts(0); built[0] = true;
  // Pre-load saved procs logs for all containers

  for(let i=0; i<CHART_DATA.length; i++){
    const name = CHART_DATA[i] && CHART_DATA[i].name;
    const started = CHART_DATA[i] && CHART_DATA[i].started;
    if(!name) continue;
    const sessionKey = name + ':' + (started || 'unknown');

    try {
      const r = await fetch('/api/procs/history/' + encodeURIComponent(sessionKey.replace(':','_')));
      const data = await r.json();
      if(data.history && Object.keys(data.history).length > 0) {
        if(!window._procHistory) window._procHistory = {};
        if(!window._procAllKeys) window._procAllKeys = {};
        window._procHistory[sessionKey] = data.history;
        window._procAllKeys[sessionKey] = Object.keys(data.history);

        // Render immediately

        const procsEl = document.getElementById('chart-procs-' + i);
        if(procsEl) {
          procsEl.innerHTML = '';

	  const tsHeader = document.createElement('div');
          tsHeader.style.cssText = 'font-size:.58rem;color:var(--dim);margin-bottom:6px;';
          tsHeader.textContent = 'Restored from history';
          procsEl.appendChild(tsHeader);


          const allKeys = [...window._procAllKeys[sessionKey]];
          allKeys.sort((a,b) => {
            const ha = window._procHistory[sessionKey][a];
            const hb = window._procHistory[sessionKey][b];
            if(ha.active && !hb.active) return -1;
            if(!ha.active && hb.active) return 1;
            return hb.lastPct - ha.lastPct;
          });
          allKeys.forEach(key => {
            const ph = window._procHistory[sessionKey][key];
            const hist = ph.hist;
            const max = Math.max(...hist, 1);
            const pDiv = document.createElement('div');
            pDiv.style.cssText = `display:grid;grid-template-columns:55px minmax(200px,1fr) 320px;gap:8px;align-items:center;margin-bottom:3px;`;
            const pctDiv = document.createElement('div');
            const lastPct = ph.lastPct || 0;
            const pctColor = !ph.active ? 'var(--dim)' : lastPct > 50 ? '#ff4444' : lastPct > 10 ? '#fbbf24' : '#00ff88';
            pctDiv.style.cssText = `font-size:.6rem;color:${pctColor};text-align:right;white-space:nowrap;`;
            pctDiv.textContent = ph.active ? lastPct.toFixed(1) + '%' : 'stopped';
            const cmdDiv = document.createElement('div');
            cmdDiv.style.cssText = `font-size:.6rem;color:${ph.active ? 'var(--text)' : 'var(--dim)'};word-break:break-all;line-height:1.4;`;
            cmdDiv.textContent = ph.cmd;
            const sparkDiv = document.createElement('div');
            sparkDiv.style.cssText = 'display:flex;align-items:flex-end;gap:2px;height:24px;';
            hist.forEach(v => {
              const bar = document.createElement('div');
              const h = Math.max(2, Math.round((v/max)*24));
              const color = !ph.active ? '#4a6080' : v>50 ? '#ff4444' : v>10 ? '#fbbf24' : '#00ff88';
              bar.style.cssText = `width:8px;height:${h}px;background:${color};opacity:0.8;border-radius:1px;`;
              sparkDiv.appendChild(bar);
            });

	    rightDiv.appendChild(cmdDiv);
            pDiv.appendChild(pctDiv);
            pDiv.appendChild(rightDiv);
            pDiv.appendChild(sparkDiv);

            procsEl.appendChild(pDiv);
          });
        }
      }
    } catch(e){}

  }
});

// Poll immediately when tab becomes visible again
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    setTimeout(async () => {
      try {
        const r = await fetch('/api/chart_data');
        const freshCharts = await r.json();
        const existingNames = new Set(CHART_DATA.map(d => d && d.name).filter(Boolean));
        freshCharts.forEach((d, i) => {
          if (!d || !d.name) return;
          if (!existingNames.has(d.name)) {
            existingNames.add(d.name);
            CHART_DATA.push(d);
            const newIdx = CHART_DATA.length - 1;
            injectVtab(newIdx, d);
            injectVpanel(newIdx, d);
            built[newIdx] = false;
          }
        });
        tickElapsedTimers();
      } catch(e) {}
    }, 500);
  }
});

// Refresh charts every 30s for active container
setInterval(async () => {
  try {
    const r = await fetch('/api/chart_data');
    const freshCharts = await r.json();
    // Track existing containers by name BEFORE updating CHART_DATA
    const existingNames = new Set(CHART_DATA.map(d => d && d.name).filter(Boolean));

    // Inject new containers dynamically instead of reloading
    freshCharts.forEach((d, i) => {
      if (!d || !d.name) return;
      if (!existingNames.has(d.name)) {
        if (CHART_DATA.length >= 15) return;  // max tabs
        existingNames.add(d.name);
        CHART_DATA.push(d);
        const newIdx = CHART_DATA.length - 1;
        injectVtab(newIdx, d);
        injectVpanel(newIdx, d);
        built[newIdx] = false;
      }
    });

    // Update existing badges and charts — match by name not index
    freshCharts.forEach((d, i) => {
      // Find the index in CHART_DATA by name (handles dynamically injected containers)
      const cdIdx = CHART_DATA.findIndex(c => c && c.name === d.name);
      if (cdIdx < 0) return;
      CHART_DATA[cdIdx] = d;
      // Badge may be at cdIdx or at a different position if injected dynamically
      const badge = document.getElementById('vtab-badge-' + cdIdx);
      if(badge) {
        if(d.active) {
          badge.className = 'vtab-badge live';
          // Preserve data-started so tickElapsedTimers keeps updating it
          if (d.started_iso) badge.dataset.started = d.started_iso;
          // Only set text if no elapsed time yet (first poll)
          if (!badge.dataset.started) badge.textContent = '● LIVE';
        } else {
          badge.className = 'vtab-badge done';
          delete badge.dataset.started;
          badge.textContent = d.duration && d.duration !== '—' ? d.duration : 'done';
        }
      }
      // Update charts
      if(built[i]) updateCharts(i, d);
    });

    // Tick elapsed timers on all LIVE badges
    tickElapsedTimers();
  } catch(e){}
}, 30000);

// ── AJAX STATS POLLING ──────────────────────────────────────────────────────
let lastFetchTs = 0;
const POLL_INTERVAL = 15000; // 15 seconds — no more full page reloads

function setBar(id, pct, warn, crit) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.width = pct + '%';
  el.className = 'b' + (pct >= crit ? ' hot' : pct >= warn ? ' warm' : ' grn');
}
function setColor(id, val, warn, crit) {
  const el = document.getElementById(id);
  if (!el) return;
  const n = parseFloat(val);
  el.className = 'sv' + (n >= crit ? ' hot' : n >= warn ? ' warm' : ' cool');
}

function renderStats(d) {
  // Header
  document.getElementById('ts-now').textContent = d.now_nzdt || '';
  const dot = document.getElementById('livdot');
  dot.className = 'livdot' + (d.pressure === 'crit' ? ' crit' : d.pressure === 'warn' ? ' warn' : '');

  // Staleness indicators
  const upd = d.updated || {};
  for (const [k, age] of Object.entries(upd)) {
    const el = document.getElementById('stale-' + k);
    if (el) el.textContent = age > 60 ? `⚠ ${age}s ago` : `${age}s ago`;
  }
  document.getElementById('fetch-age').textContent =
    `last poll: ${new Date().toLocaleTimeString('en-NZ', {hour12:false})}`;

  // ── Health dots ──
  function setDot(id, status) {
    const el = document.getElementById(id);
    if (el) el.className = 'hdot ' + status;
  }
  const hgpu = d.gpu || {};
  const hxmr = d.xmrig || {};
  const hkvm = d.kvm || {};
  const hvast = d.vastai || {};
  const hdock = d.docker || {};
  const hgame = d.gaming || {};
  const hqrl = d.qrl || {};
  const hcfx = d.cfx || {};

  setDot('h-gpu', hgpu.temp ? 'ok' : 'err');
  setDot('h-xmrig', hxmr.running ? 'ok' : hxmr.paused ? 'warn' : 'err');
  setDot('h-kvm', hkvm.state === 'running' ? 'ok' : hkvm.state === 'paused' ? 'warn' : 'err');
  setDot('h-vast', hvast['#gpus'] !== undefined || hvast.reliability ? 'ok' : 'err');
  setDot('h-docker', Array.isArray(d.docker) ? (d.docker.length > 0 ? 'ok' : 'warn') : (hdock.containers ? 'ok' : 'err'));
  setDot('h-gaming', hgame.online ? 'ok' : 'err');
  setDot('h-lolminer', hgpu.lol_algo ? 'ok' : 'warn');
  setDot('h-qrl', hqrl.balance !== undefined ? 'ok' : 'err');
  setDot('h-cfx', hcfx.balance !== undefined ? 'ok' : 'err');
  setDot('h-coingecko', (hqrl.price_usd || hcfx.price_usd) ? 'ok' : 'warn');

  // Alerts bar
  const bar = document.getElementById('alert-bar');
  if (d.alerts && d.alerts.length) {
    bar.style.display = 'flex';
    bar.innerHTML = d.alerts.map(a =>
      `<span class="alert-chip ${a.level}">${a.msg}</span>`
    ).join('');
  } else {
    bar.style.display = 'none';
  }

  // Kill btn state
  const killBtn = document.getElementById('kill-btn');
  if (d.kill_active) {
    killBtn.textContent = '✅ RESUME';
    killBtn.classList.add('active');
  } else {
    killBtn.textContent = '⛔ KILL';
    killBtn.classList.remove('active');
  }

  // GPU
  const g = d.gpu || {};
  if (Object.keys(g).length) {
    const temp = parseFloat(g.temp || 0);
    const memPct = g.mem_pct || 0;
    const utilPct = parseFloat(g.util || 0);
    const powerW = parseFloat(g.power || 0);
    const powerPct = Math.min(100, Math.round(powerW/575*100));

    document.getElementById('gpu-temp').textContent = temp + ' °C';
    setColor('gpu-temp', temp, 70, 85);
    setBar('gpu-temp-bar', Math.min(100, Math.round(temp/100*100)), 70, 85);

    document.getElementById('gpu-power').textContent = powerW + ' W';
    setBar('gpu-power-bar', powerPct, 75, 90);

    document.getElementById('gpu-util').textContent = utilPct + ' %';
    setBar('gpu-util-bar', utilPct, 80, 95);

    document.getElementById('gpu-mem').textContent = `${g.mem_used}/${g.mem_total} GB`;
    setBar('gpu-mem-bar', memPct, 80, 95);

    document.getElementById('gpu-fan').textContent = g.fan + ' %';
    document.getElementById('gpu-throttle').innerHTML =
      g.throttling
        ? '<span class="tbadge y">THROTTLED</span>'
        : '<span class="tbadge n">OK</span>';
  }

  // System
  // Gaming wallets
  const gw = d.gaming_wallets || {};
  const gwFields = {"gaming-erg-balance": gw.erg_balance ? gw.erg_balance + " ERG" : "—", "gaming-erg-nzd": gw.erg_nzd || "—", "gaming-erg-usd": gw.erg_usd || "—", "gaming-erg-price": gw.erg_price_usd ? gw.erg_price_usd + " / ERG" : "—", "gaming-flux-balance": gw.flux_balance || "—", "gaming-flux-nzd": gw.flux_nzd || "—", "gaming-flux-price": gw.flux_price_usd ? gw.flux_price_usd + " / FLUX" : "—"};
  // CFX wallet — shared with ghost-vm f2pool account
  const gcfx = d.cfx || {};
  const _gcb = document.getElementById('gaming-cfx-balance'); if(_gcb) _gcb.textContent = gcfx.balance ? gcfx.balance + ' CFX' : '—';
  const _gcn = document.getElementById('gaming-cfx-nzd'); if(_gcn) _gcn.textContent = gcfx.nzd || '—';
  const _gcu = document.getElementById('gaming-cfx-usd'); if(_gcu) _gcu.textContent = gcfx.usd || '—';
  const _gcp = document.getElementById('gaming-cfx-price'); if(_gcp) _gcp.textContent = gcfx.nzd_price ? gcfx.nzd_price + ' / CFX' : '—';
  // Iron (Ghost-VM) wallet
  const iw = d.gaming_wallets || {};
  const ironEl = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val || "—"; };
  ironEl("iron-balance",   iw.iron_balance);
  ironEl("iron-nzd",       iw.iron_nzd);
  ironEl("iron-usd",       iw.iron_usd);
  ironEl("iron-paid",      iw.iron_paid);
  ironEl("iron-hashrate",  iw.iron_hashrate);
  ironEl("iron-price-usd", iw.iron_price_usd);
  Object.entries(gwFields).forEach(([id, val]) => { const el = document.getElementById(id); if (el) el.textContent = val; });
  const s = d.sys || {};
  if (Object.keys(s).length) {
    const cpu = parseFloat(s.cpu_pct || 0);
    const ramPct = s.mem_pct || 0;
    document.getElementById('sys-cpu').textContent = cpu.toFixed(1) + ' %';
    setColor('sys-cpu', cpu, 65, 82);
    setBar('sys-cpu-bar', cpu, 65, 82);
    document.getElementById('sys-ram').textContent = `${s.mem_used}/${s.mem_total} GB`;
    setBar('sys-ram-bar', ramPct, 75, 88);
    if (s.disk_used !== undefined) {
      document.getElementById('sys-disk').textContent = `${s.disk_used}/${s.disk_total} GB`;
      setBar('sys-disk-bar', s.disk_pct, 75, 90);
    }
    if (s.net_rx_mb !== undefined) {
      document.getElementById('sys-net').textContent = `↓${s.net_rx_mb} ↑${s.net_tx_mb} MB`;
    }
    document.getElementById('sys-load').textContent = s.load || '—';
    const swapEl = document.getElementById('sys-swap');
    if (swapEl) swapEl.textContent = `${s.swap_used || 0}/${s.swap_total || 0} GB`;
    const uptimeEl = document.getElementById('sys-uptime');
    if (uptimeEl) uptimeEl.textContent = s.uptime || '—';
  }

  // XMRig
  const x = d.xmrig || {};
  window._lastXmrig = x;
  const xFields = ['xmrig-hr','xmrig-hr60','xmrig-peak','xmrig-algo','xmrig-pool',
                   'xmrig-ping','xmrig-acc','xmrig-rej','xmrig-uptime'];
  if (x.paused) {
    // xmrig-status removed
    const _xAlgo = document.getElementById('xmrig-algo');
    if (_xAlgo) _xAlgo.textContent = x.reason || '—';
    xFields.filter(f => f !== 'xmrig-algo').forEach(f => {
      const el = document.getElementById(f); if (el) el.textContent = '—';
    });
  } else if (x.running) {
    // xmrig-status removed
    const _xEl = (id) => document.getElementById(id);
    if (_xEl('xmrig-hr'))     _xEl('xmrig-hr').textContent     = x.hashrate_10s ? x.hashrate_10s + ' H/s' : '—';
    if (_xEl('xmrig-hr60'))   _xEl('xmrig-hr60').textContent   = x.hashrate_60s ? x.hashrate_60s + ' H/s' : '—';
    if (_xEl('xmrig-peak'))   _xEl('xmrig-peak').textContent   = x.hashrate_peak ? x.hashrate_peak + ' H/s' : '—';
    if (_xEl('xmrig-algo'))   _xEl('xmrig-algo').textContent   = x.algo || '—';
    if (_xEl('xmrig-pool'))   _xEl('xmrig-pool').textContent   = x.pool || '—';
    if (_xEl('xmrig-ping'))   _xEl('xmrig-ping').textContent   = x.ping ? x.ping + ' ms' : '—';
    if (_xEl('xmrig-acc'))    _xEl('xmrig-acc').textContent    = x.accepted ?? '—';
    if (_xEl('xmrig-rej'))    _xEl('xmrig-rej').textContent    = x.rejected ?? '—';
    if (_xEl('xmrig-uptime')) _xEl('xmrig-uptime').textContent = x.uptime || '—';
    if (_xEl('xmrig-cores'))  _xEl('xmrig-cores').textContent  = x.cores ? `${x.cores} cores` : '—';
  } else {
    // xmrig-status removed
    xFields.forEach(f => { const el = document.getElementById(f); if (el) el.textContent = '—'; });
  }

  // QRL + CFX wallet — always update regardless of XMRig state
  const q = d.qrl || {};
  const _qb = document.getElementById("qrl-balance"); if (_qb) _qb.textContent = q.balance ? q.balance + " QRL" : "—";
  const _qn = document.getElementById("qrl-nzd"); if(_qn) _qn.textContent = q.nzd || "—";
  const _qu = document.getElementById("qrl-usd"); if(_qu) _qu.textContent = q.usd || "—";
  const _qp = document.getElementById("qrl-price-nzd"); if(_qp) _qp.textContent = q.nzd_price ? q.nzd_price + " / QRL" : "—";
  const cfx = d.cfx || {};
  const _cb = document.getElementById("cfx-balance"); if(_cb) _cb.textContent = cfx.balance ? cfx.balance + " CFX" : "—";
  const _cn = document.getElementById("cfx-nzd"); if(_cn) _cn.textContent = cfx.nzd || "—";
  const _cu = document.getElementById("cfx-usd"); if(_cu) _cu.textContent = cfx.usd || "—";
  const _cp = document.getElementById("cfx-price-nzd"); if(_cp) _cp.textContent = cfx.nzd_price ? cfx.nzd_price + " / CFX" : "—";

  // KLS wallet
  const kls = d.kls || {};
  const _klb = document.getElementById("kls-balance"); if(_klb) _klb.textContent = kls.balance ? kls.balance + " KLS" : "—";
  const _kli = document.getElementById("kls-immature"); if(_kli) _kli.textContent = kls.immature ? kls.immature + " KLS" : "—";
  const _kln = document.getElementById("kls-nzd"); if(_kln) _kln.textContent = kls.nzd || "—";
  const _klu = document.getElementById("kls-usd"); if(_klu) _klu.textContent = kls.usd || "—";
  const _klp = document.getElementById("kls-price"); if(_klp) _klp.textContent = kls.usd_price ? kls.usd_price + " / KLS" : "—";
  const _klh = document.getElementById("kls-hashrate"); if(_klh) _klh.textContent = kls.hashrate || "—";

  // Auto-highlight mining tab based on current algo
  const kvmAlgo = (d.kvm || {}).lol_algo || "";
  // Gaming PC mining tab auto-highlight
  const gmAlgo = (d.gaming || {}).algo || "";
  function setGamingMiningTab(coin) {
    ['erg','cfx','flux'].forEach(c => {
      const t = document.getElementById('gaming-wtab-'+c);
      if (t) t.classList.remove('mining');
    });
    const at = document.getElementById('gaming-wtab-'+coin);
    if (at) at.classList.add('mining');
  }
  if (gmAlgo.includes('OCTOPUS')) setGamingMiningTab('cfx');
  else if (gmAlgo.includes('AUTOLYKOS')) setGamingMiningTab('erg');
  else if (gmAlgo.includes('FLUX')) setGamingMiningTab('flux');

  if (kvmAlgo.includes("OCTOPUS")) setKvmMiningTab("cfx");
  else if (kvmAlgo.includes("KARLSEN")) setKvmMiningTab("kls");
  else if (kvmAlgo.includes("AUTOLYKOS")) setKvmMiningTab("erg");
  else if (kvmAlgo.includes("FISH")) setKvmMiningTab("iron");
  // Blacklist indicators on wallet tabs
  const bl = (d.kvm && d.kvm.coin_blacklist) || [];
  ['cfx','kls','erg','iron'].forEach(c => {
    const el = document.getElementById('kvm-wtab-' + c);
    if (el) el.textContent = (bl.includes(c.toUpperCase()) ? '⬛ ' : '') + c.toUpperCase();
  });
  // Render TOP 10 table
  const t10 = d.top10 || {};
  const t10coins = t10.coins || [];
  const t10bl = (d.kvm && d.kvm.coin_blacklist) || [];
  const top10El = document.getElementById('top10-table');
  if (top10El && t10coins.length > 0) {
    const rows = t10coins.map((c, i) => {
      const isBlacklisted = t10bl.includes(c.tag);
      const isSup = c.supported;
      const tagDisplay = (isBlacklisted ? '⬛ ' : '') + c.tag;
      const color = isBlacklisted ? 'var(--red)' : c.lolminer ? 'var(--accent)' : 'var(--text)';
      const badge = c.lolminer ? '<span style="font-size:.55rem;color:var(--accent);margin-left:.2rem;">⛏</span>' : '';
      return `<div style="display:flex;justify-content:space-between;padding:.25rem .4rem;` +
        `border-bottom:1px solid rgba(255,255,255,.04);align-items:center;">` +
        `<span style="color:var(--dim);width:1.2rem;font-size:.6rem;">${i+1}</span>` +
        `<span style="color:${color};width:4rem;font-weight:600;">${tagDisplay}${badge}</span>` +
        `<span style="color:var(--dim);flex:1;font-size:.65rem;">${c.algo}</span>` +
        `<span style="color:var(--green);text-align:right;">$${c.usd_day.toFixed(3)}</span>` +
        `</div>`;
    }).join('');
    top10El.innerHTML = `<div style="display:flex;justify-content:space-between;padding:.2rem .4rem;` +
      `border-bottom:1px solid rgba(255,255,255,.08);font-size:.6rem;color:var(--dim);">` +
      `<span style="width:1.2rem">#</span><span style="width:3.5rem">COIN</span>` +
      `<span style="flex:1">ALGO</span><span>$/DAY</span></div>` + rows;
  }

  // Gaming PRL wallet (same address as Ghost-VM)
  const gprlSet = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
  const _kvm = d.kvm || {};
  gprlSet('gaming-prl-balance',    _kvm.prl_balance !== undefined ? _kvm.prl_balance + ' PRL' : '—');
  gprlSet('gaming-prl-paid',       _kvm.prl_paid !== undefined ? _kvm.prl_paid + ' PRL' : '—');
  gprlSet('gaming-prl-hashrate',   _kvm.prl_hashrate || '—');
  gprlSet('gaming-prl-hashrate24', _kvm.prl_hashrate24 || '—');
  gprlSet('gaming-prl-shares',     _kvm.prl_shares24 !== undefined ? String(_kvm.prl_shares24) : '—');

  // Gaming PC wallet tabs
  ['erg','cfx','flux'].forEach(c => {
    const el = document.getElementById('gaming-wtab-' + c);
    if (el) el.textContent = (bl.includes(c.toUpperCase()) ? '⬛ ' : '') + c.toUpperCase();
  });

  // Gaming PC
  const gm = d.gaming || {};
  const gmOnline = document.getElementById("gaming-online");
  if (gmOnline) gmOnline.textContent = gm.online ? "● ONLINE" : "○ OFFLINE";
  if (gmOnline) gmOnline.style.color = gm.online ? "var(--green)" : "var(--dim)";
  const gmOs = document.getElementById('gaming-os');
  if (gmOs) gmOs.textContent = gm.os || '—';
  // Gaming PC 2x4 grid
  const gmTemp = parseFloat(gm.temp) || 0;
  const gmPower = parseFloat(gm.power) || 0;
  const gmUtil = parseFloat(gm.util) || 0;
  const gmMemUsed = parseFloat(gm.mem_used) || 0;
  const gmMemTotal = parseFloat(gm.mem_total) || 1;
  const gmMemPct = Math.round(gmMemUsed/gmMemTotal*100);
  document.getElementById('gaming-temp').textContent = gmTemp ? gmTemp + ' °C' : '—';
  setBar('gaming-temp-bar', Math.min(100, Math.round(gmTemp)), 70, 85);
  document.getElementById('gaming-power').textContent = gmPower ? gmPower + ' W' : '—';
  setBar('gaming-power-bar', Math.min(100, Math.round(gmPower/180*100)), 75, 90);
  document.getElementById('gaming-util').textContent = gmUtil ? gmUtil + ' %' : '—';
  setBar('gaming-util-bar', gmUtil, 80, 95);
  document.getElementById('gaming-mem').textContent = gmMemUsed && gmMemTotal ? (gmMemUsed/1024).toFixed(1)+'/'+(gmMemTotal/1024).toFixed(1)+' GB' : '—';
  setBar('gaming-mem-bar', gmMemPct, 75, 90);
  if (gm.cpu_pct !== undefined) {
    document.getElementById('gaming-cpu').textContent = gm.cpu_pct + ' %';
    setBar('gaming-cpu-bar', parseFloat(gm.cpu_pct), 65, 82);
  }
  if (gm.ram_used !== undefined) {
    document.getElementById('gaming-ram').textContent = gm.ram_used + '/' + gm.ram_total + ' GB';
    setBar('gaming-ram-bar', Math.round(gm.ram_used/gm.ram_total*100), 75, 88);
  }
  if (gm.disk_used !== undefined) {
    document.getElementById('gaming-disk').textContent = gm.disk_used + '/' + gm.disk_total + ' GB';
    setBar('gaming-disk-bar', Math.round(gm.disk_used/gm.disk_total*100), 75, 90);
  }
  document.getElementById('gaming-hashrate').textContent = gm.hashrate || '—';
  const gmFields = {"gaming-gpu": gm.gpu || "—", "gaming-coin": gm.coin || "—", "gaming-algo": gm.algo || "—"};
  Object.entries(gmFields).forEach(([id, val]) => { const el = document.getElementById(id); if (el) el.textContent = val; });


  const containers = d.docker || [];
  const customerContainer = containers.find(c => c.name && c.name.startsWith('C.'));

  // Vast.ai
  const v = d.vastai || {};
  if (Object.keys(v).length) {
    const occup = v.occup || '';
    const vastLabel = occup === 'D_' ? 'rented (demand)' : occup === 'I_' ? 'rented (interruptible)' : occup === 'x_' ? 'available' : 'unlisted';
    const vastClass = (occup === 'x_' || occup === 'D_' || occup === 'I_') ? 'on' : 'off';
    const _vs = document.getElementById('vast-status'); if(_vs) _vs.innerHTML = `<span class="pill ${vastClass}">${vastLabel}</span>`;
    (document.getElementById('vast-listed') || {}).textContent = v.occup || '—';
    (document.getElementById('vast-reliability') || {}).textContent = v.reliab ? (parseFloat(v.reliab) * 100).toFixed(2) + '%' : '—';
    (document.getElementById('vast-score') || {}).textContent = v['gpuD_$/h'] ? '$' + v['gpuD_$/h'] + '/h' : '—';

    // Show container details when rented
    const rented = occup === 'D_' || occup === 'I_';
    const hasContainer = customerContainer != null && rented;  // only active if Vast says rented
    const _vcr2 = document.getElementById('vast-container-row'); if(_vcr2) _vcr2.style.display = hasContainer ? '' : 'none';
    const _vast_image_row = document.getElementById('vast-image-row'); if(_vast_image_row) _vast_image_row.style.display     = hasContainer ? '' : 'none';
    const _vast_process_row = document.getElementById('vast-process-row'); if(_vast_process_row) _vast_process_row.style.display   = hasContainer ? '' : 'none';
    const _vast_running_row = document.getElementById('vast-running-row'); if(_vast_running_row) _vast_running_row.style.display   = hasContainer ? '' : 'none';
    if (customerContainer) {
      (document.getElementById('vast-container') || {}).textContent = customerContainer.name || '—';
      (document.getElementById('vast-image') || {}).textContent     = customerContainer.image || '—';
      (document.getElementById('vast-running') || {}).textContent   = customerContainer.running_for || '—';
      const proc = customerContainer.top_process;
      const cpu  = customerContainer.top_cpu;
      (document.getElementById('vast-process') || {}).textContent  = proc ? `${proc} (${cpu}%)` : '—';
    }
  }



  // Live process list — timeline sparklines
  if (customerContainer && customerContainer.top_procs) {
    const ts = new Date().toLocaleTimeString('en-NZ', {hour12:false});
    const newProcs = customerContainer.top_procs.trim();
    for (let i = 0; i < CHART_DATA.length; i++) {
      if (CHART_DATA[i] && CHART_DATA[i].name === customerContainer.name) {
        const sessionKey = CHART_DATA[i].name + ':' + (CHART_DATA[i].started || 'unknown');
        if (!window._procHistory) window._procHistory = {};
        if (!window._procHistory[sessionKey]) window._procHistory[sessionKey] = {};
        if (!window._procAllKeys) window._procAllKeys = {};
        if (!window._procAllKeys[sessionKey]) window._procAllKeys[sessionKey] = [];
        if (!window._procTick) window._procTick = {};
        if (window._procTick[sessionKey] === undefined) window._procTick[sessionKey] = 0;
        if (!window._lastProcs) window._lastProcs = {};

        window._lastProcs[sessionKey] = newProcs;

        const MAX_TICKS = 80;
        const currentTick = window._procTick[sessionKey];
        window._procTick[sessionKey]++;

        // Parse current snapshot
        const currentKeys = new Set();
        const currentPcts = {};
        newProcs.split(String.fromCharCode(10)).forEach(line => {
          const m = line.match(/^\s*([\d.]+)%\s+(.+)$/);
          if (m) {
            const pct = parseFloat(m[1]);
            const cmd = m[2].trim();
            currentKeys.add(cmd);
            currentPcts[cmd] = pct;
            if (!window._procAllKeys[sessionKey].includes(cmd)) {
              window._procAllKeys[sessionKey].push(cmd);
              // New process — record start tick
              window._procHistory[sessionKey][cmd] = {cmd, active:true, lastPct:pct, startTick:currentTick, hist:[]};
            }
          }
        });

        // Update all known processes
        window._procAllKeys[sessionKey].forEach(key => {
          const ph = window._procHistory[sessionKey][key];
          if (currentKeys.has(key)) {
            ph.hist.push(currentPcts[key]);
            ph.active = true;
            ph.lastPct = currentPcts[key];
          } else if (ph.active) {
            // Just stopped — mark inactive, no more bars
            ph.active = false;
            ph.lastPct = 0;
          }
          // Trim to MAX_TICKS
          if (ph.hist.length > MAX_TICKS) ph.hist.shift();
        });

        // Render
        const procsEl = document.getElementById('chart-procs-' + i);
        if (procsEl) {
          procsEl.innerHTML = '';

	  // Timestamp
          const tsHeader = document.createElement('div');
          tsHeader.style.cssText = 'font-size:.58rem;color:var(--dim);margin-bottom:6px;';
          tsHeader.textContent = 'Updated: ' + ts;
          procsEl.appendChild(tsHeader);

          const BAR_W = 12, BAR_GAP = 4, BAR_H = 20;
          const SPARK_W = MAX_TICKS * (BAR_W + BAR_GAP);

          // Sort: active by pct desc, then stopped
          const allKeys = [...window._procAllKeys[sessionKey]];
          allKeys.sort((a, b) => {
            const ha = window._procHistory[sessionKey][a];
            const hb = window._procHistory[sessionKey][b];
            if (ha.active && !hb.active) return -1;
            if (!ha.active && hb.active) return 1;
            return hb.lastPct - ha.lastPct;
          });

          // Find global max for consistent bar heights
          let globalMax = 1;
          allKeys.forEach(key => {
            const ph = window._procHistory[sessionKey][key];
            ph.hist.forEach(v => { if(v > globalMax) globalMax = v; });
          });

          allKeys.forEach(key => {
            const ph = window._procHistory[sessionKey][key];
            const pDiv = document.createElement('div');

	    pDiv.style.cssText = `display:grid;grid-template-columns:55px minmax(150px,1fr) 320px;gap:8px;align-items:center;margin-bottom:4px;`;

            // CPU%
            const pctDiv = document.createElement('div');
            const pctColor = !ph.active ? 'var(--dim)' : ph.lastPct > 50 ? '#ff4444' : ph.lastPct > 10 ? '#fbbf24' : '#00ff88';
            pctDiv.style.cssText = `font-size:.6rem;color:${pctColor};text-align:right;white-space:nowrap;`;
            pctDiv.textContent = ph.active ? ph.lastPct.toFixed(1) + '%' : 'stopped';

            // Command
            const cmdDiv = document.createElement('div');
            cmdDiv.style.cssText = `font-size:.6rem;color:${ph.active ? 'var(--text)' : 'var(--dim)'};word-break:break-all;line-height:1.4;`;
            cmdDiv.textContent = ph.cmd;

            // Sparkline — fixed width, timeline positioned
            const sparkDiv = document.createElement('div');
            sparkDiv.style.cssText = `display:flex;align-items:flex-end;justify-content:flex-start;justify-self:start;gap:${BAR_GAP}px;height:${BAR_H}px;width:320px;overflow:hidden;`;

            // Calculate offset: how many empty slots before first bar
            const ticksAgo = currentTick - ph.startTick; // how long ago it started
            const emptySlots = Math.max(0, MAX_TICKS - ticksAgo - ph.hist.length);
            const paddingSlots = Math.max(0, MAX_TICKS - ph.hist.length - emptySlots);


            // Padding before process started (gray dots)
            for (let s = 0; s < paddingSlots; s++) {
              const gap = document.createElement('div');
              const slotIdx = s;
              const isDivider = (slotIdx % 20 === 19);
              gap.style.cssText = `width:${BAR_W}px;height:${BAR_H}px;display:flex;align-items:center;justify-content:center;border-right:${isDivider ? '1px solid rgba(100,140,180,0.6)' : 'none'};`;
              const dot = document.createElement('div');
              dot.style.cssText = `width:3px;height:3px;background:#3a5070;border-radius:50%;`;
              gap.appendChild(dot);
              sparkDiv.appendChild(gap);
            }
            // Actual bars
            ph.hist.forEach((v, idx) => {
              const bar = document.createElement('div');
              const h = Math.max(2, Math.round((v / globalMax) * BAR_H));
              const color = !ph.active ? '#4a6080' : v > 50 ? '#ff4444' : v > 10 ? '#fbbf24' : '#00ff88';
              const barPos = paddingSlots + idx;
              const isDivider = (barPos % 20 === 19);
              bar.style.cssText = `width:${BAR_W}px;height:${h}px;background:${color};opacity:0.8;border-radius:1px;border-right:${isDivider ? '1px solid rgba(100,140,180,0.6)' : 'none'};`;
              sparkDiv.appendChild(bar);
            });

            pDiv.appendChild(pctDiv);
            pDiv.appendChild(cmdDiv);
            pDiv.appendChild(sparkDiv);
            procsEl.appendChild(pDiv);
          });

          fetch('/api/procs/history/' + sessionKey.replace(':','_'), {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({history: window._procHistory[sessionKey]})
          }).catch(() => {});
        }
        break;
      }
    }
  }


  // KVM
  const k = d.kvm || {};
  // PRL wallet
  const prlSet = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
  prlSet('prl-balance',    k.prl_balance !== undefined ? k.prl_balance + ' PRL' : '—');
  prlSet('prl-paid',       k.prl_paid !== undefined ? k.prl_paid + ' PRL' : '—');
  prlSet('prl-hashrate',   k.prl_hashrate || '—');
  prlSet('prl-hashrate24', k.prl_hashrate24 || '—');
  prlSet('prl-shares',     k.prl_shares24 !== undefined ? String(k.prl_shares24) : '—');
  if (Object.keys(k).length) {
    // kvm-state removed
    const detail = document.getElementById('kvm-detail');
    detail.style.display = k.running ? '' : 'none';
    // KVM suspend/resume buttons (inline update)
    const _suspendBtn = document.getElementById('kvm-suspend-btn');
    const _resumeBtn  = document.getElementById('kvm-resume-btn');
    const _isSusp = k.state === 'paused';
    if (_suspendBtn) _suspendBtn.className = 'btn-ctrl' + (_isSusp ? ' active' : '');
    if (_resumeBtn)  _resumeBtn.className  = 'btn-ctrl' + (!_isSusp && k.running ? ' active' : '');


    // Mining status pill
    const mineStatus = document.getElementById('kvm-mine-status');
    if (mineStatus) {
      if (k.fetch_paused) {
        mineStatus.textContent = '⚠ ' + (k.fetch_paused_reason || 'Stats paused');
        mineStatus.style.background = 'rgba(245,158,11,.15)';
        mineStatus.style.color = '#f59e0b';
      } else {
        const coins = [];
        if (k.lol_running) {
          const algo = k.lol_algo || '';
          const coin = algo.toUpperCase().includes('AUTOLYKOS') ? 'ERG' :
                       algo.toUpperCase().includes('NEXA') ? 'NEXA' :
                       algo.toUpperCase().includes('FISHHASH') ? 'IRON' :
                       algo.toUpperCase().includes('KARLSEN') ? 'KLS' : algo;
          if (coin) coins.push(coin);
        }
        if (k.pearl_running) coins.push('PRL');
        if (coins.length > 0) {
          mineStatus.textContent = '⛏ ' + coins.join(' + ');
          mineStatus.style.background = 'rgba(0,212,255,.15)';
          mineStatus.style.color = 'var(--accent)';
        } else {
          mineStatus.textContent = '⏸ IDLE';
          mineStatus.style.background = 'rgba(255,255,255,.05)';
          mineStatus.style.color = 'var(--dim)';
        }
      }
    }

    // lolminer hashrate + shares (5min window)
    if (k.lol_hr_mhs !== undefined) {
      document.getElementById('kvm-lol-hr').textContent =
        k.lol_hr_mhs ? k.lol_hr_mhs.toFixed(1) + ' MH/s' : '0 MH/s';
    }
    if (k.lol_accepted !== undefined) {
      const _la = document.getElementById('kvm-lol-accepted');
      if (_la) _la.textContent = k.lol_accepted + ' / ' + (k.lol_total || 0);
    }
    if (k.lol_stale !== undefined) {
      const _ls = document.getElementById('kvm-lol-stale');
      if (_ls) _ls.textContent = k.lol_stale || 0;
    }
    if (k.lol_accept_rate !== undefined) {
      const rateEl = document.getElementById('kvm-lol-rate');
      if (rateEl) {
        rateEl.textContent = k.lol_accept_rate + '%';
        rateEl.style.color = k.lol_accept_rate >= 80 ? '#00ff88'
                           : k.lol_accept_rate >= 50 ? '#fbbf24' : '#ff4444';
      }
    }

    if (k.running) {
      // 2x4 resource grid updates
      if (k.gpu_temp) {
        document.getElementById('kvm-gpu-temp').textContent = k.gpu_temp + ' °C';
        setColor('kvm-gpu-temp', k.gpu_temp, 70, 85);
        setBar('kvm-gpu-temp-bar', Math.min(100, Math.round(k.gpu_temp)), 70, 85);
      }
      if (k.gpu_power) {
        document.getElementById('kvm-gpu-power').textContent = k.gpu_power + ' W';
        setBar('kvm-gpu-power-bar', Math.min(100, Math.round(k.gpu_power/180*100)), 75, 90);
      }
      if (k.gpu_util != null) {
        document.getElementById('kvm-gpu-util').textContent = k.gpu_util + ' %';
        setBar('kvm-gpu-util-bar', k.gpu_util, 80, 95);
      }
      if (k.gpu_mem_used != null) {
        document.getElementById('kvm-gpu-vram').textContent = `${k.gpu_mem_used}/${k.gpu_mem_total} GB`;
        setBar('kvm-gpu-vram-bar', k.gpu_mem_pct, 75, 90);
      }
      if (k.vcpus != null) {
        document.getElementById('kvm-cpu').textContent = k.vcpus + ' vCPUs';
        setBar('kvm-cpu-bar', Math.min(100, k.vcpus/8*100), 75, 90);
      }
      if (k.mem_pct != null) {
        document.getElementById('kvm-ram').textContent = `${k.mem_used_gb}/${k.mem_total_gb} GB`;
        setBar('kvm-ram-bar', k.mem_pct, 75, 90);
      }
      if (k.disk_pct != null) {
        document.getElementById('kvm-disk').textContent = `${k.disk_used_gb}/${k.disk_total_gb} GB`;
        setBar('kvm-disk-bar', k.disk_pct, 70, 85);
      }
      if (k.lol_hr != null) {
        const hrVal = k.lol_hr >= 1000000 ? (k.lol_hr/1000000).toFixed(2)+' MH/s' : k.lol_hr >= 1000 ? (k.lol_hr/1000).toFixed(2)+' KH/s' : k.lol_hr.toFixed(1)+' H/s';
        document.getElementById('kvm-lol-hr').textContent = hrVal;
      }
      const gpuBlock = document.getElementById('kvm-gpu-block');
      if (gpuBlock) gpuBlock.style.display = 'none';
      const lolBlock = document.getElementById('kvm-lol-block');
      if (lolBlock) {
        if (k.lol_running) {
          lolBlock.style.display = '';
          document.getElementById('kvm-lol-algo').textContent = k.lol_algo || '—';
          document.getElementById('kvm-lol-pool').textContent = k.lol_pool || '—';
          document.getElementById('kvm-lol-worker').textContent = k.lol_worker || '—';
        } else {
          lolBlock.style.display = 'none';
        }
      }
    }
  }

  // Docker
  const dockerList = document.getElementById('docker-list');
  if (dockerList && containers.length) {
    dockerList.innerHTML = containers.map(c => `
      <div class="cbox">
        <div style="flex:1">
          <div class="cn">${c.name}</div>
          <div class="ci">${c.image.split('/').pop().slice(0,40)}</div>
          <div class="ci">${c.running_for}</div>
        </div>
        <span class="pill on">up</span>
      </div>`).join('');
  } else if (dockerList) {
    dockerList.innerHTML = '<div style="color:var(--dim);font-size:.75rem;padding:.3rem 0">No containers running</div>';
  }

  // Chat enable/disable
  const chatInput = document.getElementById('chat-input');
  const sendBtn   = document.getElementById('send-btn');
  const chatBadge = document.getElementById('chat-status-badge');
  if (!d.chat_enabled || d.kill_active) {
    if (chatInput) chatInput.disabled = true;
    if (sendBtn) sendBtn.disabled = true;
    if (chatBadge) { chatBadge.style.color = 'var(--red)'; chatBadge.textContent = '● disabled'; }
  } else {
    if (chatInput) chatInput.disabled = false;
    if (sendBtn) sendBtn.disabled = false;
    if (chatBadge) { chatBadge.style.color = 'var(--green)'; chatBadge.textContent = '● ready'; }
  }

  syncControlButtons(d);
  updateMiningPanel(d);
  lastFetchTs = Date.now();
}

async function pollStats() {
  try {
    const r = await fetch('/api/stats');
    if (r.ok) renderStats(await r.json());
  } catch(e) {
    console.warn('Stats poll failed:', e);
  }
}
pollStats();
setInterval(pollStats, POLL_INTERVAL);

// ── CHAT ───────────────────────────────────────────────────────────────────
let contextCount = 0;

function appendMsg(role, text) {
  const msgs = document.getElementById('chat-msgs');
  const div  = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function updateRateBar(remaining, max) {
  const pct  = (remaining / max) * 100;
  const fill = document.getElementById('rate-fill');
  fill.style.width  = pct + '%';
  fill.className    = 'rate-fill' + (pct <= 20 ? ' full' : pct <= 60 ? ' half' : '');
  document.getElementById('chat-rate-label').textContent = `${remaining}/${max} left`;
}

async function sendChat() {
  const input   = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');
  const msg     = input.value.trim();
  if (!msg) return;

  input.value   = '';
  input.disabled = true;
  sendBtn.disabled = true;
  document.getElementById('typing-indicator').classList.add('show');

  appendMsg('user', msg);

  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg})
    });
    const data = await r.json();
    document.getElementById('typing-indicator').classList.remove('show');

    if (r.ok) {
      appendMsg('assistant', data.reply);
      contextCount = data.context_msgs || contextCount + 2;
      document.getElementById('chat-meta-left').textContent = `Context: ${contextCount} msgs`;
      updateRateBar(data.rate_remaining, {{ chat_rate_msgs }});
    } else {
      appendMsg('system', '⚠ ' + (data.error || 'Request failed'));
      if (r.status === 429) {
        const resetIn = data.error?.match(/([0-9]+)s/)?.[1];
        if (resetIn) setTimeout(() => {
          appendMsg('system', `Rate limit lifted — you can chat again`);
        }, parseInt(resetIn) * 1000);
      }
    }
  } catch(e) {
    document.getElementById('typing-indicator').classList.remove('show');
    appendMsg('system', '⚠ Network error: ' + e.message);
  } finally {
    input.disabled  = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
}

async function clearChat() {
  await fetch('/api/chat/clear', {method:'POST'});
  document.getElementById('chat-msgs').innerHTML =
    '<div class="msg system">Context cleared</div>';
  contextCount = 0;
  document.getElementById('chat-meta-left').textContent = 'Context: 0 msgs';
}

let killActive = false;

async function toggleKill() {
  const check = await fetch('/api/stats');
  const state = await check.json();
  killActive = state.kill_active;
  if (!killActive) {
    const r = await fetch('/api/kill', {method:'POST'});
    if (r.ok) killActive = true;
  } else {
    const r = await fetch('/api/kill', {method:'DELETE'});
    if (r.ok) killActive = false;
  }
  pollStats();
}


// ── Mining Panel ──────────────────────────────────────────────────────────────
const COINS = ['VAST','QRL','PRL','ERG','KLS','IRON','CFX','NEXA','FLUX'];
const MACHINE_LABELS = {
  'BLACKWELL': {label:'🖥 BLACKWELL', color:'var(--accent)'},
  'GHOST':     {label:'👻 GHOST',     color:'#a78bfa'},
  'GAMING':    {label:'🎮 GAMING',    color:'#34d399'},
};

let _miningCharts = {};

function miningTab(coin) {
  COINS.forEach(c => {
    document.getElementById('mtab-' + c)?.classList.remove('active');
    document.getElementById('mpanel-' + c)?.classList.remove('active');
  });
  document.getElementById('mtab-' + coin)?.classList.add('active');
  document.getElementById('mpanel-' + coin)?.classList.add('active');
  // Re-render VAST charts on tab switch — Chart.js needs visible canvas
  if (coin === 'VAST') setTimeout(renderVastCharts, 50);
}

function renderMachines(coin, machines) {
  const el = document.getElementById('m-machines-' + coin);
  if (!el) return;
  el.innerHTML = (machines || []).map(m => {
    const info = MACHINE_LABELS[m] || {label: m, color: 'var(--dim)'};
    return `<span style="padding:.1rem .35rem;border-radius:3px;background:rgba(255,255,255,.06);color:${info.color};font-weight:600;">${info.label}</span>`;
  }).join('');
}

function renderMiningStats(coin, d, fullStats) {
  const el = document.getElementById('m-stats-' + coin);
  if (!el || !d) return;
  const rows = [];
  const fs = fullStats || {};
  const fmt = (k, v) => v && v !== '—' ? `<div class="vr"><span class="vk">${k}</span><span class="vv">${v}</span></div>` : '';

  if (coin === 'QRL') {
    const xd = window._lastXmrig || {};
    const qrlState = (fs.qrl) || {};
    rows.push(fmt('Balance', (d.balance && d.balance !== '—') ? d.balance + ' QRL' : (qrlState.balance && qrlState.balance !== '—' ? qrlState.balance + ' QRL' : '—')));
    rows.push(fmt('Value (NZD)', d.nzd || qrlState.nzd));
    rows.push(fmt('Value (USD)', d.usd || qrlState.usd));
    rows.push(fmt('Price / QRL', d.price_nzd || qrlState.nzd_price));
    rows.push(fmt('Hashrate (10s)', xd.hashrate_10s ? xd.hashrate_10s + ' H/s' : d.hashrate));
    rows.push(fmt('Hashrate (60s)', xd.hashrate_60s ? xd.hashrate_60s + ' H/s' : d.hashrate_60s));
    rows.push(fmt('Peak', xd.hashrate_peak ? xd.hashrate_peak + ' H/s' : '—'));
    rows.push(fmt('Algo', xd.algo || 'rx/0'));
    rows.push(fmt('Pool', xd.pool || d.pool));
    rows.push(fmt('Ping', xd.ping ? xd.ping + ' ms' : '—'));
    rows.push(fmt('Accepted', xd.accepted !== undefined ? xd.accepted : d.accepted));
    rows.push(fmt('Rejected', xd.rejected !== undefined ? xd.rejected : d.rejected));
    rows.push(fmt('Threads', fs.xmrig_threads ? fs.xmrig_threads + ' threads' : '—'));
    rows.push(fmt('CPU Temp', fs.cpu_temp ? fs.cpu_temp + '°C (Tctl)' : '—'));
    // uptime is already formatted string from server
    rows.push(fmt('Uptime', xd.uptime || '—'));
  } else if (coin === 'PRL') {
    const fmtAlways = (k, v) => `<div class="vr"><span class="vk">${k}</span><span class="vv">${v || '—'}</span></div>`;
    const prlPrice = 0.50;
    const usdNzd = (fs.fx && fs.fx.usd_nzd) ? fs.fx.usd_nzd : 1.705;
    const prlBal = parseFloat(d.balance_raw) || 0;
    const prlPaid = parseFloat((d.paid || '0').replace(' PRL','')) || 0;
    const prlTotal = prlPaid + prlBal;
    rows.push(fmtAlways('Pool Balance', d.balance));
    rows.push(fmtAlways('In Wallet (paid)', d.paid));
    rows.push(fmtAlways('Total Owned', prlTotal ? prlTotal.toFixed(4) + ' PRL' : '—'));
    rows.push(fmtAlways('Value (USD)', prlTotal ? '$' + (prlTotal * prlPrice).toFixed(2) : '—'));
    rows.push(fmtAlways('Value (NZD)', prlTotal ? '$' + (prlTotal * prlPrice * usdNzd).toFixed(2) : '—'));
    rows.push(fmtAlways('Price / PRL', '$0.50 USD (manual)'));
    rows.push(fmt('HR (1h)', d.hashrate_1h));
    rows.push(fmt('HR (24h)', d.hashrate_24h));
    rows.push(fmt('Shares (24h)', d.shares_24h));
    rows.push(fmt('Workers', (d.workers || []).join(', ')));
    rows.push(fmt('Pool', d.pool));
  } else if (coin === 'ERG') {
    rows.push(fmt('Balance', d.balance));
    rows.push(fmt('Value (NZD)', d.nzd));
    rows.push(fmt('Value (USD)', d.usd));
    rows.push(fmt('Price / ERG', d.price_usd));
    rows.push(fmt('Pool', d.pool));
  } else if (coin === 'KLS') {
    rows.push(fmt('Balance', d.balance));
    rows.push(fmt('Immature', d.immature));
    rows.push(fmt('Value (NZD)', d.nzd));
    rows.push(fmt('Value (USD)', d.usd));
    rows.push(fmt('Pool HR', d.hashrate));
    rows.push(fmt('Pool', d.pool));
  } else if (coin === 'IRON') {
    rows.push(fmt('Balance', d.balance));
    rows.push(fmt('Total Paid', d.paid));
    rows.push(fmt('Value (NZD)', d.nzd));
    rows.push(fmt('Value (USD)', d.usd));
    rows.push(fmt('Price / IRON', d.price_usd));
    rows.push(fmt('Pool HR (1h)', d.hashrate_1h));
    rows.push(fmt('Pool', d.pool));
  } else if (coin === 'CFX') {
    rows.push(fmt('Balance', d.balance));
    rows.push(fmt('Value (NZD)', d.nzd));
    rows.push(fmt('Value (USD)', d.usd));
    rows.push(fmt('Pool', d.pool));
  } else if (coin === 'NEXA') {
    rows.push(fmt('Balance', d.balance));
    rows.push(fmt('Value (NZD)', d.nzd));
    rows.push(fmt('Value (USD)', d.usd));
    rows.push(fmt('Price / NEXA', d.price_usd));
    rows.push(fmt('Hashrate', d.hashrate));
    rows.push(fmt('Pool', d.pool));
    rows.push(fmt('Machines', (d.machines || []).join(', ')));
  } else if (coin === 'FLUX') {
    rows.push(fmt('Balance', d.balance));
    rows.push(fmt('Value (NZD)', d.nzd));
    rows.push(fmt('Value (USD)', d.usd));
    rows.push(fmt('Price / FLUX', d.price_usd));
    rows.push(fmt('Pool', d.pool));
  }
  el.innerHTML = rows.filter(Boolean).join('');
}

function renderMiningChart(coin, data, chartId, label, color) {
  const canvas = document.getElementById(chartId);
  if (!canvas || !data || data.length < 2) return;
  if (_miningCharts[chartId]) { _miningCharts[chartId].destroy(); }
  const labels = data.map(p => {
    const d = new Date(p[0] * 1000);
    return d.getMonth() + '/' + d.getDate();
  });
  const values = data.map(p => p[1]);
  _miningCharts[chartId] = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: color,
        backgroundColor: color + '22',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        tension: 0.3,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { label: ctx => label + ': ' + ctx.parsed.y.toFixed(4) }
      }},
      scales: {
        x: { display: false },
        y: { display: true, ticks: { color: '#666', font: { size: 9 }, maxTicksLimit: 3 },
             grid: { color: 'rgba(255,255,255,.05)' } }
      }
    }
  });
}

function updateMiningPanel(d) {
  const mining = d.mining || {};
  const bl = (d.kvm && d.kvm.coin_blacklist) || [];
  const xmrigMode = d.xmrig_mode || 'auto';
  const xmrigRunning = d.xmrig && d.xmrig.running;

  // Update VAST tab
  const hvast = d.vastai || {};
  window._lastVastOccup = hvast.occup || '';
  const mvastStatus = document.getElementById('m-vast-status');
  if (mvastStatus) {
    const occup = hvast.occup || '';
    const hasCustomer = occup.includes('D') || occup.includes('R') || occup.includes('I');
    mvastStatus.innerHTML = hasCustomer
      ? `<span class="pill on">rented (${occup === 'I_' ? 'interruptible' : 'demand'})</span>`
      : '<span class="pill off">available</span>';
  }
  const mvsEl = (id, val) => { const e = document.getElementById(id); if(e && val !== undefined && val !== null) e.textContent = val; };
  const reliab = hvast.reliab ? (parseFloat(hvast.reliab)*100).toFixed(1) + '%' : '—';
  mvsEl('m-vast-reliability',   reliab);
  mvsEl('m-vast-score',         hvast['gpuD_$/h'] ? '$' + hvast['gpuD_$/h'] + '/h' : '—');
  mvsEl('m-vast-interruptible', hvast['gpuI$/h']  ? '$' + hvast['gpuI$/h']  + '/h' : '—');
  mvsEl('m-vast-listed',        hvast.geoloc ? hvast.geoloc.replace(/_/g,' ') : '—');
  mvsEl('m-vast-id',            hvast.ID);
  mvsEl('m-vast-gpu',           hvast.gpu_name ? hvast.gpu_name.replace(/_/g,' ') : '—');
  mvsEl('m-vast-gpus',          hvast['#gpus'] ? hvast['#gpus'] + ' GPU(s)' : '—');
  mvsEl('m-vast-driver',        hvast.driver ? 'v' + hvast.driver : '—');
  mvsEl('m-vast-ip',            hvast.ip);
  mvsEl('m-vast-disk',          hvast.disk ? hvast.disk + ' GB' : '—');
  mvsEl('m-vast-veri',          hvast.veri || '—');
  mvsEl('m-vast-netd',          hvast['netd_$/TB'] ? '$' + hvast['netd_$/TB'] + '/TB' : '—');
  mvsEl('m-vast-netu',          hvast['netu_$/TB'] ? '$' + hvast['netu_$/TB'] + '/TB' : '—');
  mvsEl('m-vast-reports',       hvast.reports || '—');
  // Container info
  const _vastOccup = (d.vastai || {}).occup || '';
  const _vastRented = _vastOccup === 'D_' || _vastOccup === 'I_';  // both demand and interruptible
  const activeDocker = _vastRented
    ? (d.docker || []).find(c => c.name && c.name.startsWith('C.') && c.status && c.status.startsWith('Up'))
    : null;  // don't show container if Vast says machine is available
  const mvcRow = document.getElementById('m-vast-container-row');
  const mvcNone = document.getElementById('m-vast-no-customer');
  if (mvcRow) mvcRow.style.display = activeDocker ? '' : 'none';
  if (mvcNone) mvcNone.style.display = activeDocker ? 'none' : '';
  const _mvcDW = document.getElementById('m-vast-ai-desc-wrap');
  if (_mvcDW) _mvcDW.style.display = activeDocker ? '' : 'none';
  if (activeDocker) {
    mvsEl('m-vast-container', activeDocker.name);
    // Show repeat booking count
    if (_vastHistory && _vastHistory.top_repeats) {
      const prefix = activeDocker.name.split('_')[0]; // C.XXXXXXXX
      const rep = _vastHistory.top_repeats.find(r => r.prefix === prefix);
      mvsEl('m-vast-repeat-count', rep ? rep.count + 'x bookings' : '1x (new)');
    }
    mvsEl('m-vast-image',     activeDocker.image);
    mvsEl('m-vast-process',   activeDocker.top_process || activeDocker.process);
    mvsEl('m-vast-running',   activeDocker.status || activeDocker.running_for);
    if (_vastHistory && _vastHistory.all_counts) {
      const prefix = activeDocker.name.split('_')[0];
      const count = _vastHistory.all_counts[prefix] || 1;
      mvsEl('m-vast-times-booked', count + (count > 1 ? 'x (repeat)' : 'x (new)'));
    }
    // Show cached AI description (or dash if none yet)
    const _aiDescEl = document.getElementById('m-vast-ai-desc');
    if (_aiDescEl) _aiDescEl.textContent = _vastContainerDescs[activeDocker.name] || '\u2014';

  }
  const mdl = document.getElementById('m-docker-list');
  const odl = document.getElementById('docker-list');
  if (mdl && odl) mdl.innerHTML = odl.innerHTML;

  // Gaming PC mine status badge
  // Show/hide reboot buttons based on OS
  const g = d.gaming || {};
  const btnWin = document.getElementById('btn-gaming-win');
  const btnUbuntu = document.getElementById('btn-gaming-ubuntu');
  const btnReboot = document.getElementById('btn-gaming-reboot');
  const gOS = (g.os || '').toUpperCase();
  const isWin = gOS.startsWith('WINDOWS');
  if (btnWin) btnWin.style.display = isWin ? 'none' : '';
  if (btnReboot) btnReboot.style.display = isWin ? 'none' : '';
  if (btnUbuntu) btnUbuntu.style.display = isWin ? '' : 'none';

  const gamingMineStatus = document.getElementById('gaming-mine-status');
  if (gamingMineStatus) {
    const gCoins = [];
    if (g.coin) gCoins.push(g.coin.toUpperCase());
    else if (g.algo) {
      const a = g.algo.toUpperCase();
      const c = a.includes('OCTOPUS') ? 'CFX' : a.includes('AUTOLYKOS') ? 'ERG' :
                a.includes('NEXA') ? 'NEXA' : a.includes('FISHHASH') ? 'IRON' :
                a.includes('FLUX') ? 'FLUX' : g.algo;
      if (c) gCoins.push(c);
    }
    if (g.pearl_running) gCoins.push('PRL');
    if (gCoins.length > 0) {
      gamingMineStatus.textContent = '⛏ ' + gCoins.join(' + ');
      gamingMineStatus.style.background = 'rgba(0,212,255,.15)';
      gamingMineStatus.style.color = 'var(--accent)';
    } else {
      gamingMineStatus.textContent = '⏸ IDLE';
      gamingMineStatus.style.background = 'rgba(255,255,255,.05)';
      gamingMineStatus.style.color = 'var(--dim)';
    }
  }

  // VAST tab dot — green when customer active
  const vastDot = document.getElementById('mtab-dot-VAST');
  if (vastDot) {
    const _vOccup = (d.vastai || {}).occup || '';
    const _vRented = _vOccup === 'D_' || _vOccup === 'I_';
    const hasActiveCustomer = _vRented && (d.docker || []).some(c => c.name && c.name.startsWith('C.') && c.status && c.status.startsWith('Up'));
    vastDot.textContent = hasActiveCustomer ? '●' : '○';
    vastDot.style.color = hasActiveCustomer ? 'var(--green)' : 'var(--dim)';
  }

  COINS.forEach(coin => {
    const cd = mining[coin] || {};
    const isRunning = cd.running || false;
    const isBlacklisted = bl.includes(coin);

    // Update tab dot (skip VAST — handled separately above)
    const dot = document.getElementById('mtab-dot-' + coin);
    if (dot && coin !== 'VAST') {
      if (isBlacklisted) { dot.textContent = '⬛'; dot.style.color = 'var(--red)'; }
      else if (isRunning) { dot.textContent = '●'; dot.style.color = 'var(--green)'; }
      else { dot.textContent = '○'; dot.style.color = 'var(--dim)'; }
    }
    // Show unblacklist button only when coin is blacklisted
    const unblBtn = document.getElementById('m-unbl-' + coin);
    if (unblBtn) unblBtn.style.display = isBlacklisted ? 'inline-block' : 'none';

    // PRL: per-worker dot colours based on online/stale/inactive status
    if (coin === 'PRL' && dot) {
      const ws = cd.worker_status || {};
      const anyActive = Object.values(ws).some(s => s === 'active');
      const anyStale  = Object.values(ws).some(s => s === 'stale');
      if (anyActive) { dot.textContent = '●'; dot.style.color = 'var(--green)'; }
      else if (anyStale) { dot.textContent = '●'; dot.style.color = '#f59e0b'; }
      else { dot.textContent = '○'; dot.style.color = 'var(--dim)'; }
    }

    // Update machine badges
    renderMachines(coin, cd.machines || []);

    // Update stats
    renderMiningStats(coin, cd, d);

    // Update price chart (use CoinGecko 30d data from existing cfx/kls/erg state)
    const priceKey = {QRL:'qrl', ERG:'erg', KLS:'kls', IRON:'iron', CFX:'cfx', NEXA:'nexa', FLUX:'flux'}[coin];
    if (coin === 'QRL') {
      if (window._xmrigHistory && window._xmrigHistory.length > 1) {
        renderMiningChart(coin, window._xmrigHistory, 'm-price-chart-' + coin, 'H/s', '#fbbf24');
      }
    } else if (coin === 'PRL' && cd.hashrate_series && cd.hashrate_series.length > 1) {
      const scaledSeries = cd.hashrate_series.map(p => [p[0], p[1] / 1e12]);
      renderMiningChart(coin, scaledSeries, 'm-price-chart-' + coin, 'TH/s', '#a78bfa');
    } else if (priceKey && d[priceKey + '_price_history']) {
      renderMiningChart(coin, d[priceKey + '_price_history'], 'm-price-chart-' + coin, 'USD', '#00d4ff');
    }

    // Update 3rd chart — 30d price for QRL, hashrate 24h for others
    if (coin === 'QRL') {
      // CoinGecko 30d price for QRL
      const qph = (d.qrl_price_history || []);
      if (qph.length > 1) window._qrlPriceHistory = qph;
      // Value growth chart
      if (cd.value_history && cd.value_history.length > 1) {
        renderMiningChart(coin, cd.value_history, 'm-hr-chart-' + coin, 'NZD $', '#00d4ff');
      } else if (window._qrlPriceHistory && window._qrlPriceHistory.length > 1) {
        renderMiningChart(coin, window._qrlPriceHistory, 'm-hr-chart-' + coin, 'USD', '#00d4ff');
      }
    } else if (coin === 'IRON') {
      // HeroMiners has hashrate series in charts
      const ironCharts = (window._ironChartsData || []);
      if (ironCharts.length > 1) renderMiningChart(coin, ironCharts, 'm-hr-chart-' + coin, 'MH/s', '#f97316');
    } else if (coin === 'PRL') {
      // Already shown in slot 1, show balance in slot 3
      if (cd.balance_history && cd.balance_history.length > 1) {
        if (cd.value_history && cd.value_history.length > 1) {
          renderMiningChart(coin, cd.value_history, 'm-hr-chart-' + coin, 'NZD $', '#a78bfa');
        } else if (cd.balance_history && cd.balance_history.length > 1) {
          renderMiningChart(coin, cd.balance_history, 'm-hr-chart-' + coin, 'PRL', '#a78bfa');
        }
      }
    }

    // Update 4th chart — earnings/day from balance history derivative
    if (cd.balance_history && cd.balance_history.length > 3) {
      // Calculate daily earnings from balance deltas
      const bh = cd.balance_history;
      const earnings = [];
      for (let i = 1; i < bh.length; i++) {
        const dt = (bh[i][0] - bh[i-1][0]) / 86400; // days
        if (dt > 0 && dt < 2) {
          const delta = bh[i][1] - bh[i-1][1];
          if (delta >= 0) earnings.push([bh[i][0], parseFloat((delta/dt).toFixed(4))]);
        }
      }
      if (earnings.length > 1) {
        const eColor = {QRL:'#fbbf24',PRL:'#a78bfa',ERG:'#34d399',KLS:'#60a5fa',
                        IRON:'#f97316',CFX:'#22d3ee',NEXA:'#f59e0b',FLUX:'#a3e635'}[coin] || '#00d4ff';
        renderMiningChart(coin, earnings, 'm-earnings-chart-' + coin, '/day', eColor);
      }
    }

    // Update balance growth chart
    if (cd.balance_history && cd.balance_history.length > 1) {
      const color = {QRL:'#fbbf24', PRL:'#a78bfa', ERG:'#34d399', KLS:'#60a5fa',
                     IRON:'#f97316', CFX:'#22d3ee', NEXA:'#f59e0b', FLUX:'#a3e635'}[coin] || '#00d4ff';
      renderMiningChart(coin, cd.balance_history, 'm-balance-chart-' + coin, coin, color);
    }
  });

  // QRL control buttons in mining panel
  const mqrlStop  = document.getElementById('m-qrl-stop-btn');
  const mqrlStart = document.getElementById('m-qrl-start-btn');
  const mqrlAuto  = document.getElementById('m-qrl-auto-btn');
  const xmrigOn  = d.xmrig_force_on  || false;
  const xmrigOff = d.xmrig_force_off || false;
  if (mqrlStop)  { mqrlStop.textContent  = !xmrigRunning ? 'Stopped' : 'Stop';    mqrlStop.className  = 'btn-ctrl btn-sm' + (!xmrigRunning ? ' active' : ''); }
  if (mqrlStart) { mqrlStart.textContent = xmrigRunning  ? 'Running' : 'Start';   mqrlStart.className = 'btn-ctrl btn-sm' + (xmrigRunning  ? ' active' : ''); }
  if (mqrlAuto)  { mqrlAuto.textContent  = 'Auto';   mqrlAuto.className = 'btn-ctrl btn-sm' + (xmrigMode === 'auto' ? ' active' : ''); }
  const mqrlForceOn  = document.getElementById('m-qrl-forceon-btn');
  const mqrlForceOff = document.getElementById('m-qrl-forceoff-btn');
  if (mqrlForceOn)  { mqrlForceOn.textContent  = (xmrigMode === 'forced' && xmrigOn)  ? 'Forced ON'  : 'Force ON';  mqrlForceOn.className  = 'btn-ctrl btn-sm' + (xmrigMode === 'forced' && xmrigOn  ? ' active' : ''); }
  if (mqrlForceOff) { mqrlForceOff.textContent = (xmrigMode === 'forced' && xmrigOff) ? 'Forced OFF' : 'Force OFF'; mqrlForceOff.className = 'btn-ctrl btn-sm' + (xmrigMode === 'forced' && xmrigOff ? ' active' : ''); }

  // PRL buttons
  const prlRunning = (d.kvm || {}).pearl_running;
  // Update PRL machines badge
  const prlMachinesEl = document.getElementById('m-machines-PRL');
  if (prlMachinesEl) {
    const prlMachines = [];
    if ((d.kvm||{}).pearl_running) prlMachines.push('GHOST');
    if ((d.gaming||{}).pearl_running) prlMachines.push('GAMING');
    prlMachinesEl.innerHTML = prlMachines.map(m => {
      const info = MACHINE_LABELS[m] || {label:m, color:'var(--dim)'};
      return '<span style="padding:.1rem .35rem;border-radius:3px;background:rgba(255,255,255,.06);color:' + info.color + ';font-weight:600;">' + info.label + '</span>';
    }).join('');
  }
  const gamingPrlRunning = (d.gaming || {}).pearl_running;
  const mpgs = document.getElementById('m-prl-ghost-stop');
  const mpgst = document.getElementById('m-prl-ghost-start');
  if (mpgs)  { mpgs.textContent  = !prlRunning ? 'Stopped' : 'Stop';    mpgs.className  = 'btn-ctrl btn-sm' + (!prlRunning ? ' active' : ''); }
  if (mpgst) { mpgst.textContent = prlRunning  ? 'Running' : 'Start';   mpgst.className = 'btn-ctrl btn-sm' + (prlRunning  ? ' active' : ''); }
  const mpgms = document.getElementById('m-prl-gaming-stop');
  const mpgmst = document.getElementById('m-prl-gaming-start');
  if (mpgms)  { mpgms.textContent  = !gamingPrlRunning ? 'Stopped' : 'Stop';    mpgms.className  = 'btn-ctrl btn-sm' + (!gamingPrlRunning ? ' active' : ''); }
  if (mpgmst) { mpgmst.textContent = gamingPrlRunning  ? 'Running' : 'Start';   mpgmst.className = 'btn-ctrl btn-sm' + (gamingPrlRunning  ? ' active' : ''); }
}

// Fetch VAST history data
let _vastHistory = null;
let _vastContainerDescs = {};  // containerName -> persisted AI description

async function loadVastContainerDescs() {
  try {
    const r = await fetch('/api/analyse/load');
    const data = await r.json();
    for (const [k, v] of Object.entries(data)) {
      if (k.startsWith('__desc__') && Array.isArray(v) && v.length > 0)
        _vastContainerDescs[k.slice(8)] = v[0];
    }
  } catch(e) {}
}

async function updateVastContainerDesc() {
  const btn = document.getElementById('m-vast-desc-btn');
  const descEl = document.getElementById('m-vast-ai-desc');
  if (!btn || !descEl) return;
  const cname = (document.getElementById('m-vast-container') || {}).textContent || '';
  const image = (document.getElementById('m-vast-image') || {}).textContent || '';
  if (!cname || cname === '\u2014') return;
  const origLabel = btn.textContent;
  btn.textContent = '\u23f3 wait';
  btn.disabled = true;
  descEl.style.opacity = '.35';
  try {
    const r = await fetch('/api/container_desc/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: cname, image: image})
    });
    const data = await r.json();
    if (data.description) {
      _vastContainerDescs[cname] = data.description;
      descEl.textContent = data.description;
    } else {
      descEl.textContent = data.error || 'Error generating description.';
    }
  } catch(e) {
    descEl.textContent = 'Request failed.';
  }
  descEl.style.opacity = '.85';
  btn.textContent = origLabel;
  btn.disabled = false;
}

async function fetchVastHistory() {
  try {
    const r = await fetch('/api/vast_history');
    _vastHistory = await r.json();
    renderVastCharts();
  } catch(e) {}
}

function renderVastCharts() {
  if (!_vastHistory) return;
  const vh = _vastHistory;

  // Hourly occupancy chart
  if (vh.hourly_minutes) {
    const hoc = document.getElementById('m-vast-hourly-chart');
    if (hoc) {
      if (window._vastHourlyChart) window._vastHourlyChart.destroy();
      window._vastHourlyChart = new Chart(hoc, {
        type: 'line',
        data: {
          labels: vh.hourly_minutes.map((p, i) => {
            if (i % 24 === 0) {
              const d = new Date(p[0]*1000);
              return (d.getMonth()+1)+'/'+d.getDate();
            }
            return '';
          }),
          datasets: [{ data: vh.hourly_minutes.map(p => p[1]),
            borderColor: '#00d4ff', backgroundColor: 'rgba(0,212,255,.1)',
            borderWidth: 1, pointRadius: 0, fill: true, tension: 0.2 }]
        },
        options: { responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#666', font: { size: 8 }, maxTicksLimit: 7 }, grid: { display: false } },
            y: { min: 0, max: 60, ticks: { color: '#666', font: { size: 8 }, maxTicksLimit: 4 },
                 grid: { color: 'rgba(255,255,255,.05)' } }
          }
        }
      });
    }
    const sc2 = document.getElementById('m-vast-sessions-chart2');
    if (sc2 && vh.sessions_7d) {
      if (window._vastSessionChart2) window._vastSessionChart2.destroy();
      window._vastSessionChart2 = new Chart(sc2, {
        type: 'bar',
        data: { labels: vh.sessions_7d.map(p => p[0].slice(5)),
          datasets: [{ data: vh.sessions_7d.map(p => p[1]),
            backgroundColor: 'rgba(0,212,255,.4)', borderColor: 'var(--accent)', borderWidth: 1 }] },
        options: { responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: { ticks: { color: '#666', font: { size: 8 } }, grid: { display: false } },
                    y: { ticks: { color: '#666', font: { size: 8 }, maxTicksLimit: 3 },
                         grid: { color: 'rgba(255,255,255,.05)' } } } }
      });
    }
  }

  // Sessions chart
  if (vh.sessions_7d) {
    const sc = document.getElementById('m-vast-sessions-chart');
    if (sc) {
      if (window._vastSessionChart) window._vastSessionChart.destroy();
      window._vastSessionChart = new Chart(sc, {
        type: 'bar',
        data: {
          labels: vh.sessions_7d.map(p => p[0].slice(5)),
          datasets: [{ data: vh.sessions_7d.map(p => p[1]),
            backgroundColor: 'rgba(0,212,255,.4)', borderColor: 'var(--accent)',
            borderWidth: 1 }]
        },
        options: { responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: { ticks: { color: '#666', font: { size: 8 } }, grid: { display: false } },
                    y: { ticks: { color: '#666', font: { size: 8 }, maxTicksLimit: 3 },
                         grid: { color: 'rgba(255,255,255,.05)' } } } }
      });
    }
  }

  // Hours chart
  if (vh.hours_7d) {
    const hc = document.getElementById('m-vast-hours-chart');
    if (hc) {
      if (window._vastHoursChart) window._vastHoursChart.destroy();
      window._vastHoursChart = new Chart(hc, {
        type: 'bar',
        data: {
          labels: vh.hours_7d.map(p => p[0].slice(5)),
          datasets: [{ data: vh.hours_7d.map(p => p[1]),
            backgroundColor: 'rgba(251,191,36,.3)', borderColor: '#fbbf24',
            borderWidth: 1 }]
        },
        options: { responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: { ticks: { color: '#666', font: { size: 8 } }, grid: { display: false } },
                    y: { ticks: { color: '#666', font: { size: 8 }, maxTicksLimit: 3 },
                         grid: { color: 'rgba(255,255,255,.05)' } } } }
      });
    }
  }

  // Top repeats
  const repEl = document.getElementById('m-vast-repeats');
  if (repEl && vh.top_repeats) {
    repEl.innerHTML = vh.top_repeats.map(r =>
      `<div style="display:flex;justify-content:space-between;padding:.1rem 0;border-bottom:1px solid rgba(255,255,255,.04);">` +
      `<span style="color:var(--accent)">${r.prefix}</span>` +
      `<span style="color:#fbbf24;font-weight:600">${r.count}x</span></div>`
    ).join('') + (vh.total_sessions ?
      `<div style="margin-top:.3rem;color:var(--dim);font-size:.6rem;">Total: ${vh.total_sessions} sessions · ${vh.total_hours}h · avg ${vh.avg_session_h}h</div>` : '');
  }
}

fetchVastHistory();
  loadVastContainerDescs();
setInterval(fetchVastHistory, 300000); // refresh every 5 mins

// Load persisted analyses
async function loadPersistedAnalyses() {
  try {
    const r = await fetch('/api/analyse/load');
    window._persistedAnalyses = await r.json();
    populatePersistedAnalyses();
  } catch(e) {}
}

function populatePersistedAnalyses() {
  if (!window._persistedAnalyses || !window.CHART_DATA) return;
  CHART_DATA.forEach((c, idx) => {
    if (!c || !c.name) return;
    const hist = window._persistedAnalyses[c.name];
    if (!hist || !hist.length) return;
    if (!window._analyseHistory) window._analyseHistory = {};
    if (!window._analyseHistory[idx] || !window._analyseHistory[idx].length) {
      window._analyseHistory[idx] = hist;
    }
    // Show compact summary above processes
    const procsEl = document.getElementById('chart-procs-' + idx);
    if (procsEl && !procsEl.querySelector('.analyse-compact')) {
      const latest = hist[0];
      const d = document.createElement('div');
      d.className = 'analyse-compact';
      d.style.cssText = 'font-size:.65rem;color:var(--dim);background:rgba(0,212,255,.05);border-left:2px solid var(--accent);padding:.3rem .5rem;margin-bottom:.4rem;border-radius:0 3px 3px 0;cursor:pointer;';
      d.innerHTML = '<span style="color:var(--accent);font-size:.58rem;">⬡ ' + latest.ts + '</span> ' +
        latest.text.split(String.fromCharCode(10))[0].slice(0,120) + '…';
      d.onclick = () => {
        const ce = document.getElementById('analyse-collapse-' + idx);
        const re = document.getElementById('analyse-result-' + idx);
        if (ce) ce.style.display = 'block';
        if (re) { re.style.display = 'block';
          re.innerHTML = latest.text.split(String.fromCharCode(10)).join('<br>') +
            (latest.cost ? '<div style="font-size:.6rem;color:var(--dim);margin-top:.3rem;">' + latest.ts + ' · $' + latest.cost + '</div>' : '');
        }
      };
      procsEl.insertBefore(d, procsEl.firstChild);
    }
  });
}

loadPersistedAnalyses();
// Initial tab
miningTab('VAST');

async function toggleKvm() {
  // Legacy — kept for compatibility
  await fetch('/api/control/kvm', {method:'POST'});
  setTimeout(pollStats, 500);
}

async function prlControl(target, action) {
  await fetch(`/api/control/prl/${target}/${action}`, {method: 'POST'});
}
async function unblacklist(coin) {
  const btn = document.getElementById('m-unbl-' + coin);
  if (btn) { btn.textContent = '...'; btn.disabled = true; }
  await fetch(`/api/control/unblacklist/${coin}`, {method: 'POST'});
  if (btn) { btn.textContent = '✓ Done'; setTimeout(() => { btn.style.display='none'; }, 2000); }
  setTimeout(pollStats, 2000);
}

async function kvmSuspend() {
  await fetch('/api/control/kvm/suspend', {method:'POST'});
  setTimeout(pollStats, 500);
}

async function kvmResume() {
  await fetch('/api/control/kvm/resume', {method:'POST'});
  setTimeout(pollStats, 500);
}

async function kvmSetMode(mode) {
  await fetch('/api/control/kvm/mode', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode})
  });
  setTimeout(pollStats, 500);
}

async function toggleXmrig() {
  // Legacy — kept for compatibility
  await fetch('/api/control/xmrig', {method:'POST'});
  setTimeout(pollStats, 500);
}

async function xmrigSetMode(mode) {
  await fetch('/api/control/xmrig/mode', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode})
  });
  setTimeout(pollStats, 500);
}

async function toggleLolminer() {
  await fetch('/api/control/lolminer', {method:'POST'});
  setTimeout(pollStats, 5000);
}

function syncControlButtons(d) {
  const x = d.xmrig || {};
  const k = d.kvm || {};
  // PRL button states
  const prlRunning = k.pearl_running !== undefined ? k.pearl_running : true;
  const kvmPrlStop  = document.getElementById('kvm-prl-stop-btn');
  const kvmPrlStart = document.getElementById('kvm-prl-start-btn');
  if (kvmPrlStop)  { kvmPrlStop.textContent  = !prlRunning ? 'Stopped' : 'Stop';  kvmPrlStop.className  = 'btn-ctrl btn-sm' + (!prlRunning ? ' active' : ''); }
  if (kvmPrlStart) { kvmPrlStart.textContent = prlRunning  ? 'Running' : 'Start'; kvmPrlStart.className = 'btn-ctrl btn-sm' + (prlRunning  ? ' active' : ''); }
  // KVM suspend/resume buttons
  const kvmState = d.kvm?.state || '';
  const isSuspended = kvmState === 'paused' || d.kvm_suspended;
  const suspendBtn = document.getElementById('kvm-suspend-btn');
  const resumeBtn  = document.getElementById('kvm-resume-btn');
  if (suspendBtn) {
    suspendBtn.textContent = isSuspended ? 'Suspended' : 'Suspend';
    suspendBtn.className   = 'btn-ctrl' + (isSuspended ? ' active' : '');
  }
  if (resumeBtn) {
    resumeBtn.textContent = (kvmState === "running") ? "Running" : "Resume";
    resumeBtn.className   = 'btn-ctrl' + (!isSuspended && kvmState === 'running' ? ' active' : '');
  }

  // KVM mode buttons
  const kvmMode    = d.kvm_mode || 'auto';
  const forceOn    = d.kvm_force_on  || false;
  const forceOff   = d.kvm_force_off || false;
  const modeAutoBtn  = document.getElementById('kvm-mode-auto-btn');
  const modeOnBtn    = document.getElementById('kvm-mode-on-btn');
  const modeOffBtn   = document.getElementById('kvm-mode-off-btn');
  if (modeAutoBtn) {
    modeAutoBtn.textContent = 'Auto';
    modeAutoBtn.className   = 'btn-ctrl btn-sm' + (kvmMode === 'auto' ? ' active' : '');
  }
  if (modeOnBtn) {
    modeOnBtn.textContent = (kvmMode === 'forced' && forceOn)  ? 'Forced ON'  : 'Force ON';
    modeOnBtn.className   = 'btn-ctrl btn-sm' + (kvmMode === 'forced' && forceOn  ? ' active' : '');
  }
  if (modeOffBtn) {
    modeOffBtn.textContent = (kvmMode === 'forced' && forceOff) ? 'Forced OFF' : 'Force OFF';
    modeOffBtn.className   = 'btn-ctrl btn-sm' + (kvmMode === 'forced' && forceOff ? ' active' : '');
  }
  // XMRig stop/start + mode buttons
  const xmrigMode   = d.xmrig_mode   || 'auto';
  const xmrigOn     = d.xmrig_force_on  || false;
  const xmrigOff    = d.xmrig_force_off || false;
  const xmrigActive = !xmrigOff;
  const xmrigStopBtn     = document.getElementById('xmrig-stop-btn'); // may be null if panel removed
  const xmrigStartBtn    = document.getElementById('xmrig-start-btn');
  const xmrigAutoBtn     = document.getElementById('xmrig-mode-auto-btn');
  const xmrigModeOnBtn   = document.getElementById('xmrig-mode-on-btn');
  const xmrigModeOffBtn  = document.getElementById('xmrig-mode-off-btn');
  if (xmrigStopBtn) {
    xmrigStopBtn.textContent = (x && !x.running) ? "Stopped" : "Stop";
    xmrigStopBtn.className   = 'btn-ctrl btn-sm' + (x && !x.running ? ' active' : '');
  }
  if (xmrigStartBtn) {
    xmrigStartBtn.textContent = (x && x.running) ? 'Started' : 'Start';
    xmrigStartBtn.className   = 'btn-ctrl btn-sm' + ((x && x.running) ? ' active' : '');
  }
  if (xmrigAutoBtn) {
    xmrigAutoBtn.textContent = 'Auto';
    xmrigAutoBtn.className   = 'btn-ctrl btn-sm' + (xmrigMode === 'auto' ? ' active' : '');
  }
  if (xmrigModeOnBtn) {
    xmrigModeOnBtn.textContent = (xmrigMode === 'forced' && xmrigOn)  ? 'Forced ON'  : 'Force ON';
    xmrigModeOnBtn.className   = 'btn-ctrl btn-sm' + (xmrigMode === 'forced' && xmrigOn  ? ' active' : '');
  }
  if (xmrigModeOffBtn) {
    xmrigModeOffBtn.textContent = (xmrigMode === 'forced' && xmrigOff) ? 'Forced OFF' : 'Force OFF';
    xmrigModeOffBtn.className   = 'btn-ctrl btn-sm' + (xmrigMode === 'forced' && xmrigOff ? ' active' : '');
  }
  // lolMiner button
  const lolBtn = document.getElementById('lolminer-btn');
  if (lolBtn) {
    if (d.lolminer_locked) {
      lolBtn.textContent = '⛏ lolMiner: Start'; lolBtn.className = 'btn-ctrl stopped';
    } else {
      lolBtn.textContent = '⛏ lolMiner: Stop'; lolBtn.className = 'btn-ctrl';
    }
  }
  // Kill button
  const killBtn = document.getElementById('kill-btn');
  if (d.kill_active) {
    killBtn.textContent = '✅ RESUME'; killBtn.classList.add('active');
  } else {
    killBtn.textContent = '⛔ KILL'; killBtn.classList.remove('active');
  }
}

</script>
</body>
</html>"""



if __name__ == "__main__":
    start_background_pollers()
    log.info("All background pollers started. Flask listening on :8080")
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
