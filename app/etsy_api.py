"""Etsy Open API v3: klíč, OAuth přihlášení shop a volání API."""

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from zaklad import API, AppError, AUTH_URL, http_json, load_tokens, LOCK, pending_auth, save_tokens, SCOPES, TOKEN_URL


def api_key_header(cfg):
    return {"x-api-key": f"{cfg['keystring']}:{cfg['shared_secret']}"}


def token_request(cfg, form):
    tok = http_json("POST", TOKEN_URL, form=form)
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)) - 60,
    }


def access_token(cfg, tokens, shop_id):
    shop = tokens[shop_id]
    if time.time() >= shop["expires_at"]:
        fresh = token_request(cfg, {
            "grant_type": "refresh_token",
            "client_id": cfg["keystring"],
            "refresh_token": shop["refresh_token"],
        })
        shop.update(fresh)
        save_tokens(tokens)
    return shop["access_token"]


def api_get(cfg, tokens, shop_id, path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = api_key_header(cfg)
    headers["Authorization"] = "Bearer " + access_token(cfg, tokens, shop_id)
    return http_json("GET", url, headers=headers)


def api_send(cfg, tokens, shop_id, method, path, data=None, files=None):
    """Zápis do Etsy: data jako JSON, nebo se soubory (files = {pole: (název, bajty)}) jako multipart."""
    headers = api_key_header(cfg)
    headers["Authorization"] = "Bearer " + access_token(cfg, tokens, shop_id)
    if files is not None:
        boundary = "----etsydashboard" + secrets.token_hex(12)
        parts = []
        for k, v in (data or {}).items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for k, (fname, content) in files.items():
            safe = fname.replace('"', "'").replace("\r", "").replace("\n", "")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{safe}"\r\n'
                         f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return http_json(method, API + path, headers, body=b"".join(parts),
                         ctype="multipart/form-data; boundary=" + boundary)
    return http_json(method, API + path, headers, body=json.dumps(data or {}).encode(), ctype="application/json")


def api_get_all(cfg, tokens, shop_id, path, params):
    """Stáhne všechny stránky výsledků (limit 100 na stránku)."""
    out, offset = [], 0
    while True:
        page = api_get(cfg, tokens, shop_id, path, {**params, "limit": 100, "offset": offset})
        results = page.get("results", [])
        out.extend(results)
        offset += len(results)
        if len(results) < 100 or offset >= page.get("count", 0):
            return out


# ------------------------------------------------------------- přihlášení

def auth_start(cfg):
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    pending_auth(update={state: verifier})
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": cfg["keystring"],
        "redirect_uri": cfg["redirect_uri"],
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }, quote_via=urllib.parse.quote)


def auth_finish(cfg, pasted):
    url = urllib.parse.urlparse(pasted.strip())
    query = urllib.parse.parse_qs(url.query)
    if "code_challenge" in query or url.path.startswith("/oauth/connect"):  # vložený přihlašovací odkaz
        raise AppError("auth_connect_url", "Tohle je přihlašovací odkaz na Etsy, ne adresa po přihlášení. "
                       "Otevři ho, na Etsy klikni na Grant access a vlož sem adresu, na které pak skončíš "
                       "(začíná tvou Callback URL a obsahuje ?code=).")
    if "error" in query:
        detail = query.get("error_description", query["error"])[0]
        raise AppError("auth_denied", "Etsy přístup nepovolilo: " + detail, detail=detail)
    if "code" not in query:
        raise AppError("auth_code", "V adrese chybí 'code'. Vlož celou adresu, na které skončíš po kliknutí "
                       "na Grant access na Etsy.")
    state = query.get("state", [""])[0]
    verifier = pending_auth(pop=state)
    if not verifier:
        raise AppError("auth_state", "Adresa nepatří k tomuto přihlášení. Klikni znovu na „Přihlásit shopu“.")
    tok = token_request(cfg, {
        "grant_type": "authorization_code",
        "client_id": cfg["keystring"],
        "redirect_uri": cfg["redirect_uri"],
        "code": query["code"][0],
        "code_verifier": verifier,
    })
    with LOCK:
        tokens = load_tokens()
        tokens["_novy"] = tok
        try:
            me = api_get(cfg, tokens, "_novy", "/users/me")
            shop_id = str(me.get("shop_id") or "")
            if not shop_id:
                raise AppError("no_shop", "Tento Etsy účet nemá shopu.")
            shop = api_get(cfg, tokens, "_novy", f"/shops/{shop_id}")
        finally:
            tokens.pop("_novy", None)
        tok.update({"shop_name": shop.get("shop_name", shop_id), "scope": SCOPES,
                    "user_id": tok["access_token"].split(".")[0]})
        tokens[shop_id] = tok
        save_tokens(tokens)
    return tok["shop_name"]


def money(m):
    if not m:
        return 0.0, ""
    return m["amount"] / (m.get("divisor") or 100), m.get("currency_code", "")


# ------------------------------------------------------------- oprávnění

def can_write(tok):
    return "listings_w" in (tok.get("scope") or "").split()


def can_delete(tok):
    return "listings_d" in (tok.get("scope") or "").split()


def can_ship(tok):
    return "transactions_w" in (tok.get("scope") or "").split()
