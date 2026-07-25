#!/usr/bin/env python3
"""
authgate.py — Passkey (WebAuthn) reverse-proxy gateway for the blackwell dashboard.

Sits IN FRONT of dashboard.py. Everything it proxies is gated behind a passkey
(Touch ID / Face ID on the Mac, platform biometric on the phone). Deliberately
does NOT touch dashboard.py — the dashboard keeps its fragile TEMPLATE string and
stays bound to localhost; only this gateway is exposed (via `tailscale serve`).

Auth model (defence in depth):
  1. You must be an enrolled device on the tailnet to reach this at all
     (Tailscale serve terminates TLS and only tailnet traffic arrives).
  2. You must present a registered passkey with user verification (biometric).

Routes owned by the gateway (everything else is proxied to the dashboard):
  GET  /__auth/login                     login page (passkey prompt)
  POST /__auth/authenticate/options      begin authentication ceremony
  POST /__auth/authenticate/verify       finish authentication -> sets session cookie
  GET  /__auth/register?token=...        one-time enrolment page (see --enroll)
  POST /__auth/register/options          begin registration ceremony
  POST /__auth/register/verify           finish registration -> stores credential
  POST /__auth/logout                    clear session

Run:
  python3 authgate.py                    # serve gateway on 127.0.0.1:8081
  python3 authgate.py --enroll           # print a one-time enrolment URL (10 min TTL)

Config via env (see CONFIG block). RP_ID / ORIGIN must match the tailnet hostname
that the browser sees, e.g. blackwell-node-01.<tailnet>.ts.net
"""

import os
import sys
import json
import time
import hmac
import base64
import hashlib
import secrets
import pathlib
import argparse

import requests
from flask import Flask, request, redirect, make_response, Response, abort

from webauthn import (
    generate_registration_options,
    verify_registration_response,
    generate_authentication_options,
    verify_authentication_response,
    options_to_json,
    base64url_to_bytes,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
    PublicKeyCredentialDescriptor,
)

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
RP_ID       = os.environ.get("AUTHGATE_RP_ID",   "blackwell-node-01.example.ts.net")
ORIGIN      = os.environ.get("AUTHGATE_ORIGIN",   f"https://{RP_ID}")
RP_NAME     = os.environ.get("AUTHGATE_RP_NAME",  "Blackwell Dashboard")
USER_NAME   = os.environ.get("AUTHGATE_USER",     "rich-rob")
UPSTREAM    = os.environ.get("AUTHGATE_UPSTREAM", "http://127.0.0.1:8080")  # dashboard.py
LISTEN_HOST = os.environ.get("AUTHGATE_HOST",     "127.0.0.1")
LISTEN_PORT = int(os.environ.get("AUTHGATE_PORT", "8081"))
STATE_DIR   = pathlib.Path(os.environ.get("AUTHGATE_STATE", str(pathlib.Path.home() / ".authgate")))
SESSION_TTL = int(os.environ.get("AUTHGATE_SESSION_TTL", str(8 * 3600)))   # 8h
COOKIE_NAME = "authgate_session"

# A persistent secret for signing session cookies + enrolment tokens.
STATE_DIR.mkdir(parents=True, exist_ok=True)
_secret_path = STATE_DIR / "secret.key"
if not _secret_path.exists():
    _secret_path.write_bytes(secrets.token_bytes(32))
    _secret_path.chmod(0o600)
SECRET = _secret_path.read_bytes()

CREDS_PATH   = STATE_DIR / "credentials.json"   # registered passkeys
ENROLL_PATH  = STATE_DIR / "enroll.json"        # active one-time enrolment token

app = Flask(__name__)

# ----------------------------------------------------------------------------
# tiny helpers
# ----------------------------------------------------------------------------
def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def load_creds() -> list:
    if CREDS_PATH.exists():
        return json.loads(CREDS_PATH.read_text())
    return []

def save_creds(creds: list):
    CREDS_PATH.write_text(json.dumps(creds, indent=2))
    CREDS_PATH.chmod(0o600)

def sign_token(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = b64u(raw)
    sig = b64u(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"

def verify_token(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expect = b64u(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        pad = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + pad))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None

# in-memory challenge store (single user, single process — fine here)
_pending: dict[str, bytes] = {}

def stash_challenge(kind: str, challenge: bytes):
    _pending[kind] = challenge

def take_challenge(kind: str) -> bytes | None:
    return _pending.pop(kind, None)

# ----------------------------------------------------------------------------
# session
# ----------------------------------------------------------------------------
def is_authenticated() -> bool:
    tok = request.cookies.get(COOKIE_NAME)
    if not tok:
        return False
    return verify_token(tok) is not None

def issue_session(resp):
    tok = sign_token({"u": USER_NAME, "exp": int(time.time()) + SESSION_TTL})
    resp.set_cookie(COOKIE_NAME, tok, max_age=SESSION_TTL,
                    secure=True, httponly=True, samesite="Strict", path="/")
    return resp

# ----------------------------------------------------------------------------
# WebAuthn: registration (enrolment)
# ----------------------------------------------------------------------------
@app.get("/__auth/register")
def register_page():
    token = request.args.get("token", "")
    saved = json.loads(ENROLL_PATH.read_text()) if ENROLL_PATH.exists() else {}
    if not token or token != saved.get("token") or saved.get("exp", 0) < time.time():
        return "Invalid or expired enrolment token. Generate a fresh one with "\
               "`python3 authgate.py --enroll`.", 403
    return REGISTER_HTML

@app.post("/__auth/register/options")
def register_options():
    token = request.json.get("token", "")
    saved = json.loads(ENROLL_PATH.read_text()) if ENROLL_PATH.exists() else {}
    if not token or token != saved.get("token") or saved.get("exp", 0) < time.time():
        abort(403)
    existing = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"]))
                for c in load_creds()]
    opts = generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_name=USER_NAME,
        user_display_name=USER_NAME,
        exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    stash_challenge("reg", opts.challenge)
    return Response(options_to_json(opts), mimetype="application/json")

@app.post("/__auth/register/verify")
def register_verify():
    token = request.json.get("token", "")
    saved = json.loads(ENROLL_PATH.read_text()) if ENROLL_PATH.exists() else {}
    if not token or token != saved.get("token") or saved.get("exp", 0) < time.time():
        abort(403)
    challenge = take_challenge("reg")
    if challenge is None:
        abort(400)
    v = verify_registration_response(
        credential=json.dumps(request.json["credential"]),
        expected_challenge=challenge,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
        require_user_verification=True,
    )
    creds = load_creds()
    creds.append({
        "id": b64u(v.credential_id),
        "public_key": b64u(v.credential_public_key),
        "sign_count": v.sign_count,
        "added": int(time.time()),
    })
    save_creds(creds)
    ENROLL_PATH.unlink(missing_ok=True)   # one-time: burn the token
    return {"ok": True}

# ----------------------------------------------------------------------------
# WebAuthn: authentication (login)
# ----------------------------------------------------------------------------
@app.get("/__auth/login")
def login_page():
    return LOGIN_HTML

@app.post("/__auth/authenticate/options")
def auth_options():
    creds = load_creds()
    if not creds:
        abort(403, "No passkeys registered. Enrol one first.")
    allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in creds]
    opts = generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    stash_challenge("auth", opts.challenge)
    return Response(options_to_json(opts), mimetype="application/json")

@app.post("/__auth/authenticate/verify")
def auth_verify():
    challenge = take_challenge("auth")
    if challenge is None:
        abort(400)
    cred = request.json["credential"]
    raw_id = cred["id"]
    stored = next((c for c in load_creds() if c["id"] == raw_id), None)
    # browsers may hand back the id with different padding; match by decoded bytes
    if stored is None:
        want = base64url_to_bytes(raw_id)
        stored = next((c for c in load_creds()
                       if base64url_to_bytes(c["id"]) == want), None)
    if stored is None:
        abort(403, "Unknown credential")
    v = verify_authentication_response(
        credential=json.dumps(cred),
        expected_challenge=challenge,
        expected_rp_id=RP_ID,
        expected_origin=ORIGIN,
        credential_public_key=base64url_to_bytes(stored["public_key"]),
        credential_current_sign_count=stored["sign_count"],
        require_user_verification=True,
    )
    # persist the new signature counter (clone / replay detection)
    creds = load_creds()
    for c in creds:
        if c["id"] == stored["id"]:
            c["sign_count"] = v.new_sign_count
    save_creds(creds)
    return issue_session(make_response({"ok": True}))

@app.post("/__auth/logout")
def logout():
    resp = make_response({"ok": True})
    resp.set_cookie(COOKIE_NAME, "", max_age=0, path="/")
    return resp

# ----------------------------------------------------------------------------
# Reverse proxy — everything else, only when authenticated
# ----------------------------------------------------------------------------
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
              "content-length"}

@app.route("/", defaults={"path": ""},
           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@app.route("/<path:path>",
           methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def proxy(path):
    if not is_authenticated():
        # HTML navigations get the login page; API calls get 401
        if "text/html" in request.headers.get("Accept", ""):
            return redirect("/__auth/login")
        abort(401)
    url = f"{UPSTREAM}/{path}"
    upstream = requests.request(
        method=request.method,
        url=url,
        params=request.args,
        headers={k: v for k, v in request.headers if k.lower() != "host"},
        data=request.get_data(),
        cookies=request.cookies,
        stream=True,
        allow_redirects=False,
        timeout=300,
    )
    headers = [(k, v) for k, v in upstream.raw.headers.items()
               if k.lower() not in HOP_BY_HOP]
    return Response(upstream.iter_content(chunk_size=65536),
                    status=upstream.status_code, headers=headers)

# ----------------------------------------------------------------------------
# minimal front-end (self-contained, no CDN)
# ----------------------------------------------------------------------------
_JS_HELPERS = """
function b64uToBuf(s){s=s.replace(/-/g,'+').replace(/_/g,'/');const p='='.repeat((4-s.length%4)%4);
  const bin=atob(s+p);const b=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)b[i]=bin.charCodeAt(i);return b.buffer;}
function bufToB64u(buf){const b=new Uint8Array(buf);let s='';for(let i=0;i<b.length;i++)s+=String.fromCharCode(b[i]);
  return btoa(s).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');}
function fixReg(o){o.challenge=b64uToBuf(o.challenge);o.user.id=b64uToBuf(o.user.id);
  if(o.excludeCredentials)o.excludeCredentials.forEach(c=>c.id=b64uToBuf(c.id));return o;}
function fixAuth(o){o.challenge=b64uToBuf(o.challenge);
  if(o.allowCredentials)o.allowCredentials.forEach(c=>c.id=b64uToBuf(c.id));return o;}
function packReg(c){return {id:c.id,rawId:bufToB64u(c.rawId),type:c.type,
  response:{clientDataJSON:bufToB64u(c.response.clientDataJSON),
            attestationObject:bufToB64u(c.response.attestationObject)}};}
function packAuth(c){return {id:c.id,rawId:bufToB64u(c.rawId),type:c.type,
  response:{clientDataJSON:bufToB64u(c.response.clientDataJSON),
            authenticatorData:bufToB64u(c.response.authenticatorData),
            signature:bufToB64u(c.response.signature),
            userHandle:c.response.userHandle?bufToB64u(c.response.userHandle):null}};}
"""

LOGIN_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Blackwell — unlock</title>
<style>body{font:16px system-ui;background:#0b0d10;color:#e6e8eb;display:grid;place-items:center;height:100vh;margin:0}
.card{text-align:center;max-width:320px}button{font:inherit;padding:.8em 1.4em;border-radius:10px;border:0;
background:#2f6feb;color:#fff;cursor:pointer}#msg{color:#9aa0a6;margin-top:1em;min-height:1.4em}</style>
<div class=card><h2>Blackwell Dashboard</h2><p>Unlock with your passkey.</p>
<button id=go>Unlock</button><div id=msg></div></div>
<script>%JS%
document.getElementById('go').onclick=async()=>{
  const m=document.getElementById('msg');m.textContent='Requesting passkey…';
  try{
    const opts=await (await fetch('/__auth/authenticate/options',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'})).json();
    const cred=await navigator.credentials.get({publicKey:fixAuth(opts)});
    const r=await fetch('/__auth/authenticate/verify',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({credential:packAuth(cred)})});
    if(r.ok){m.textContent='Unlocked.';location.href='/';}else{m.textContent='Rejected.';}
  }catch(e){m.textContent=e.message;}
};</script>""".replace("%JS%", _JS_HELPERS)

REGISTER_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Blackwell — enrol passkey</title>
<style>body{font:16px system-ui;background:#0b0d10;color:#e6e8eb;display:grid;place-items:center;height:100vh;margin:0}
.card{text-align:center;max-width:320px}button{font:inherit;padding:.8em 1.4em;border-radius:10px;border:0;
background:#2f6feb;color:#fff;cursor:pointer}#msg{color:#9aa0a6;margin-top:1em;min-height:1.4em}</style>
<div class=card><h2>Enrol a passkey</h2><p>One-time. This link burns after success.</p>
<button id=go>Create passkey</button><div id=msg></div></div>
<script>%JS%
const token=new URLSearchParams(location.search).get('token');
document.getElementById('go').onclick=async()=>{
  const m=document.getElementById('msg');m.textContent='Creating…';
  try{
    const opts=await (await fetch('/__auth/register/options',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({token})})).json();
    const cred=await navigator.credentials.create({publicKey:fixReg(opts)});
    const r=await fetch('/__auth/register/verify',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({token,credential:packReg(cred)})});
    if(r.ok){m.textContent='Enrolled. You can now unlock at /__auth/login';}else{m.textContent='Failed: '+await r.text();}
  }catch(e){m.textContent=e.message;}
};</script>""".replace("%JS%", _JS_HELPERS)

# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def make_enrol_token(ttl=600):
    token = secrets.token_urlsafe(24)
    ENROLL_PATH.write_text(json.dumps({"token": token, "exp": int(time.time()) + ttl}))
    ENROLL_PATH.chmod(0o600)
    print("One-time enrolment URL (valid 10 min):\n")
    print(f"  {ORIGIN}/__auth/register?token={token}\n")
    print("Open it on the device whose biometric you want to enrol (Mac Touch ID / phone).")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--enroll", action="store_true", help="mint a one-time enrolment URL and exit")
    args = ap.parse_args()
    if args.enroll:
        make_enrol_token()
        sys.exit(0)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
