#!/usr/bin/env python3
"""Etsy Dashboard: webová aplikace s dashboardem, která sleduje nové objednávky
a pohyby na platebním účtu (měsíční výpis) pro jednu nebo více Etsy shop
přes Etsy Open API v3. Běží lokálně u tebe v počítači.

Jen standardní knihovna Pythonu (3.9+), nic se nemusí instalovat.

Příkazy:
    python etsy_dashboard.py           # spustí aplikaci a otevře ji v prohlížeči
    python etsy_dashboard.py web --sluzba  # běh na pozadí bez otevírání prohlížeče (Mac: LaunchAgent)
    python etsy_dashboard.py demo      # ukázková data bez Etsy (na portu 8766)
    python etsy_dashboard.py jednou    # jedna kontrola bez prohlížeče (Plánovač úloh / cron)
"""

import base64
import csv
import hashlib
import html
import io
import json
import os
import random
import secrets
import shutil
import subprocess
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = os.environ.get("ETSY_DASHBOARD_API") or "https://openapi.etsy.com/v3/application"
AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
SCOPES = "transactions_r shops_r profile_r listings_r listings_w listings_d"
PORT = 8765
VERSION = "1.19"
UPDATE_BASE = os.environ.get("ETSY_DASHBOARD_UPDATE_URL") or "https://raw.githubusercontent.com/fanattik/etsy-dashboard/main/app/"
UPDATE_EVERY = 24 * 3600
RATES_URL = os.environ.get("ETSY_DASHBOARD_RATES_URL") or "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
RATES_EVERY = 12 * 3600

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKENS_PATH = os.path.join(DATA_DIR, "tokens.json")
DB_PATH = os.path.join(DATA_DIR, "etsy.db")
RATES_PATH = os.path.join(DATA_DIR, "kurzy.json")
TAXONOMY_PATH = os.path.join(DATA_DIR, "kategorie.json")
DASHBOARD_PATH = os.path.join(BASE_DIR, "dashboard.html")

LEDGER_CHUNK = 30 * 24 * 3600  # výpis stahujeme po 30denních oknech
OVERLAP = 2 * 24 * 3600  # při každé kontrole se díváme 2 dny zpět (nic neuteče)

DEFAULT_CONFIG = {
    "keystring": "",
    "shared_secret": "",
    "redirect_uri": "https://localhost:3003/etsy",
    "interval_minut": 15,
    "prvni_stazeni_dni": 365,
    "ntfy_topic": "",
    "jazyk": "cs",
}

LOCK = threading.Lock()  # jedna kontrola / zápis tokenů naráz
DISCOUNT_LOCK = threading.Lock()  # slevy se spouští / ukončují jen jednou naráz
STATUS = {"posledni_kontrola": None, "chyby": {}, "bezi": False}

# Texty, které posílá server (upozornění na telefon). Dashboard má vlastní překlady.
TEXTS = {
    "cs": {"order": "🛒 {shop}: nová objednávka {total} od {buyer} ({items})",
           "status": "🔄 {shop}: objednávka {id} je teď {status}",
           "more": "… a dalších {n}", "title": "Etsy Dashboard: novinky"},
    "en": {"order": "🛒 {shop}: new order {total} from {buyer} ({items})",
           "status": "🔄 {shop}: order {id} is now {status}",
           "more": "… and {n} more", "title": "Etsy Dashboard: news"},
    "de": {"order": "🛒 {shop}: neue Bestellung {total} von {buyer} ({items})",
           "status": "🔄 {shop}: Bestellung {id} ist jetzt {status}",
           "more": "… und {n} weitere", "title": "Etsy Dashboard: Neuigkeiten"},
}


def tr(cfg, key, **kw):
    return TEXTS.get(cfg.get("jazyk"), TEXTS["cs"])[key].format(**kw)


class AppError(RuntimeError):
    """Chyba pro uživatele: český text + kód, podle kterého ji dashboard přeloží."""

    def __init__(self, kod, text, **param):
        super().__init__(text)
        self.kod = kod
        self.param = param

# macOS: Python z python.org nemá vlastní kořenové certifikáty, systémové jsou v /etc/ssl/cert.pem
SSL_CTX = ssl.create_default_context()
if sys.platform == "darwin" and os.path.exists("/etc/ssl/cert.pem"):
    SSL_CTX.load_verify_locations("/etc/ssl/cert.pem")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------- konfigurace

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def config_ready(cfg):
    return bool(cfg.get("keystring") and cfg.get("shared_secret"))


def load_tokens():
    if not os.path.exists(TOKENS_PATH):
        return {}
    with open(TOKENS_PATH, encoding="utf-8") as f:
        return json.load(f)


PENDING_PATH = os.path.join(DATA_DIR, "prihlaseni.json")


def pending_auth(update=None, pop=None):
    """Rozpracovaná přihlášení (state → code_verifier). Ukládají se na disk, aby přežila
    restart služby (např. automatickou aktualizaci) mezi otevřením Etsy a vložením adresy."""
    try:
        with open(PENDING_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    now = int(time.time())
    data = {k: v for k, v in data.items() if now - v[1] < 3600}
    found = data.pop(pop, None) if pop is not None else None
    if update:
        data.update({k: [v, now] for k, v in update.items()})
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return found[0] if found else None


def save_tokens(tokens):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, TOKENS_PATH)


# ---------------------------------------------------------------------- HTTP

def http_json(method, url, headers=None, form=None, body=None, ctype=None):
    """form = urlencoded formulář, body = hotová data (JSON, multipart) s typem ctype."""
    data = urllib.parse.urlencode(form).encode() if form is not None else body
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header("User-Agent", f"etsy-dashboard/{VERSION} (+https://github.com/fanattik/etsy-dashboard)")
    if data is not None:
        req.add_header("Content-Type", ctype or "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}  # DELETE vrací 204 bez obsahu
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Etsy vrátilo chybu {e.code}: {body[:300]}") from None


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


# --------------------------------------------------------------- databáze

def db(path=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(path or DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS objednavky (
        shop TEXT, receipt_id INTEGER PRIMARY KEY, vytvoreno_ts INTEGER, zakaznik TEXT,
        polozky TEXT, celkem REAL, mena TEXT, zaplaceno INTEGER, odeslano INTEGER,
        stav TEXT, zmeneno_ts INTEGER, pridano_ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS vypis (
        shop TEXT, entry_id INTEGER PRIMARY KEY, datum_ts INTEGER, typ TEXT, popis TEXT,
        castka REAL, mena TEXT, zustatek REAL, reference TEXT, pridano_ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS listingy (
        shop TEXT, listing_id INTEGER PRIMARY KEY, nazev TEXT, stav TEXT, cena REAL, mena TEXT,
        mnozstvi INTEGER, zobrazeni INTEGER, oblibene INTEGER, stitky TEXT, obrazek TEXT, url TEXT,
        sku TEXT, vytvoreno_ts INTEGER, zmeneno_ts INTEGER, pridano_ts INTEGER, popis TEXT)""")
    if "popis" not in {r[1] for r in con.execute("PRAGMA table_info(listingy)")}:  # tabulka z verze 1.10
        con.execute("ALTER TABLE listingy ADD COLUMN popis TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS doprava (
        receipt_id INTEGER PRIMARY KEY, dopravce TEXT, cislo TEXT, cena REAL, mena TEXT,
        zmeneno_ts INTEGER)""")  # ruční údaje, import ani synchronizace je nepřepíšou
    con.execute("""CREATE TABLE IF NOT EXISTS slevy (
        id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT, listing_id INTEGER, procento REAL, od_ts INTEGER,
        do_ts INTEGER, stav TEXT, puvodni TEXT, nove TEXT, chyba TEXT, vytvoreno_ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS csv_polozky (
        receipt_id INTEGER PRIMARY KEY, polozky TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS stav (
        shop_id TEXT, co TEXT, posledni_ts INTEGER, PRIMARY KEY (shop_id, co))""")
    return con


def last_ts(con, shop_id, what):
    row = con.execute("SELECT posledni_ts FROM stav WHERE shop_id=? AND co=?", (shop_id, what)).fetchone()
    return row[0] if row else None


def set_last_ts(con, shop_id, what, ts):
    con.execute("INSERT OR REPLACE INTO stav VALUES (?,?,?)", (shop_id, what, ts))


def money(m):
    if not m:
        return 0.0, ""
    return m["amount"] / (m.get("divisor") or 100), m.get("currency_code", "")


# ------------------------------------------------------------------ kontrola

def check_shop(cfg, tokens, con, shop_id):
    name = tokens[shop_id].get("shop_name", shop_id)
    now = int(time.time())
    first_start = now - int(cfg["prvni_stazeni_dni"]) * 24 * 3600
    news = []

    # --- objednávky: nové + změny stavu
    since = last_ts(con, shop_id, "objednavky")
    first = since is None
    params = {"sort_on": "updated", "sort_order": "desc"}
    if first:
        params["min_created"] = first_start
    else:
        params["min_last_modified"] = since - OVERLAP
    for r in api_get_all(cfg, tokens, shop_id, f"/shops/{shop_id}/receipts", params):
        total, cur = money(r.get("grandtotal"))
        items = "; ".join(f"{t.get('quantity', 1)}x {t.get('title', '')}" for t in r.get("transactions", []))
        ship = next((x for x in r.get("shipments") or [] if x.get("carrier_name") or x.get("tracking_code")), None)
        if ship:  # dopravce a číslo zásilky z Etsy, jen když je uživatel nevyplnil sám
            con.execute("INSERT OR IGNORE INTO doprava VALUES (?,?,?,?,?,?)",
                        (r["receipt_id"], "", "", None, "", 0))
            con.execute("UPDATE doprava SET dopravce=? WHERE receipt_id=? AND dopravce=''",
                        (ship.get("carrier_name") or "", r["receipt_id"]))
            con.execute("UPDATE doprava SET cislo=? WHERE receipt_id=? AND cislo=''",
                        (ship.get("tracking_code") or "", r["receipt_id"]))
        old = con.execute("SELECT stav, pridano_ts FROM objednavky WHERE receipt_id=?", (r["receipt_id"],)).fetchone()
        added = old[1] if old else (0 if first else now)
        con.execute("INSERT OR REPLACE INTO objednavky VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            name, r["receipt_id"], r.get("created_timestamp") or r.get("create_timestamp") or 0,
            r.get("name", ""), items, total, cur, int(bool(r.get("is_paid"))),
            int(bool(r.get("is_shipped"))), r.get("status", ""),
            r.get("updated_timestamp") or r.get("update_timestamp") or 0, added))
        if old is None and not first:
            news.append(tr(cfg, "order", shop=name, total=f"{total:.2f} {cur}", buyer=r.get("name", ""), items=items))
        elif old is not None and old[0] != r.get("status", ""):
            news.append(tr(cfg, "status", shop=name, id=r["receipt_id"], status=r.get("status", "")))
    set_last_ts(con, shop_id, "objednavky", now)

    # --- platební účet (měsíční výpis): prodeje, poplatky, refundy, výplaty
    since = last_ts(con, shop_id, "vypis")
    first_l = since is None
    if first_l:  # data z ručně nahraných CSV nahradí přesná data z API
        con.execute("DELETE FROM vypis WHERE shop=? AND entry_id<0", (name,))
    start = first_start if first_l else since - OVERLAP
    while start < now:
        end = min(start + LEDGER_CHUNK, now)
        entries = api_get_all(cfg, tokens, shop_id, f"/shops/{shop_id}/payment-account/ledger-entries",
                              {"min_created": start, "max_created": end})
        for e in entries:
            amount = (e.get("amount") or 0) / 100
            is_new = con.execute("SELECT 1 FROM vypis WHERE entry_id=?", (e["entry_id"],)).fetchone() is None
            if not is_new:
                continue
            con.execute("INSERT INTO vypis VALUES (?,?,?,?,?,?,?,?,?,?)", (
                name, e["entry_id"], e.get("created_timestamp") or e.get("create_date") or 0,
                e.get("ledger_type", ""), e.get("description", ""), amount, e.get("currency", ""),
                (e.get("balance") or 0) / 100,
                f"{e.get('reference_type', '')} {e.get('reference_id', '')}".strip(),
                0 if first_l else now))
            if not first_l:
                news.append(f"💰 {name}: {e.get('description') or e.get('ledger_type', '')} "
                            f"{amount:+.2f} {e.get('currency', '')}")
        start = end
    set_last_ts(con, shop_id, "vypis", now)
    try:  # chyba u listingů nesmí zahodit objednávky a výpis
        sync_listings(cfg, tokens, con, shop_id, name, now)
    except Exception as e:
        print(f"⚠️  {name}: listingy: {e}")
    con.commit()
    return news


LISTING_STATES = ("active", "inactive", "draft", "sold_out", "expired")


def sync_listings(cfg, tokens, con, shop_id, name, now):
    """Listingy (položky v obchodě). Všechny stavy vyžadují oprávnění listings_r; shopy přihlášené
    ve starší verzi ho nemají, pro ně se stáhnou aspoň aktivní listingy (stačí API klíč)."""
    listings, complete = [], True
    try:
        for state in LISTING_STATES:
            listings += api_get_all(cfg, tokens, shop_id, f"/shops/{shop_id}/listings",
                                    {"state": state, "includes": "Images"})
    except Exception:
        complete, listings = False, api_get_all(cfg, tokens, shop_id, f"/shops/{shop_id}/listings/active",
                                                {"includes": "Images"})
    seen = set()
    for l in listings:
        price, cur = money(l.get("price"))
        images = l.get("images") or []
        old = con.execute("SELECT pridano_ts FROM listingy WHERE listing_id=?", (l["listing_id"],)).fetchone()
        con.execute("INSERT OR REPLACE INTO listingy VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            name, l["listing_id"], html.unescape(l.get("title") or ""), l.get("state", ""), price, cur,
            l.get("quantity"), l.get("views"), l.get("num_favorers"), ", ".join(l.get("tags") or []),
            (images[0].get("url_570xN") or images[0].get("url_170x135", "")) if images else "", l.get("url", ""),
            ", ".join(l.get("skus") or []), l.get("created_timestamp") or l.get("creation_timestamp") or 0,
            l.get("last_modified_timestamp") or l.get("updated_timestamp") or 0, old[0] if old else 0,
            html.unescape(l.get("description") or "")))
        seen.add(l["listing_id"])
    con.execute("DELETE FROM listingy WHERE shop=? AND listing_id<0", (name,))  # API nahradí data z CSV
    if complete:  # smazané listingy
        for (lid,) in con.execute("SELECT listing_id FROM listingy WHERE shop=?", (name,)).fetchall():
            if lid not in seen:
                con.execute("DELETE FROM listingy WHERE listing_id=?", (lid,))


# ------------------------------------------------------- nové listingy přes API

def can_write(tok):
    return "listings_w" in (tok.get("scope") or "").split()


def can_delete(tok):
    return "listings_d" in (tok.get("scope") or "").split()


def taxonomy(cfg, tokens, shop_id):
    """Kategorie Etsy (jen koncové, s celou cestou). Mění se zřídka, drží se 30 dní v data/kategorie.json."""
    try:
        with open(TAXONOMY_PATH, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - cached["stazeno"] < 30 * 24 * 3600:
            return cached["kategorie"]
    except (OSError, ValueError, KeyError):
        pass
    out = []

    def walk(nodes, path):
        for n in nodes:
            p = path + [n.get("name", "")]
            if n.get("children"):
                walk(n["children"], p)
            else:
                out.append([n["id"], " › ".join(p)])
    walk(api_get(cfg, tokens, shop_id, "/seller-taxonomy/nodes").get("results", []), [])
    out.sort(key=lambda x: x[1].lower())
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TAXONOMY_PATH, "w", encoding="utf-8") as f:
        json.dump({"stazeno": int(time.time()), "kategorie": out}, f, ensure_ascii=False)
    return out


DEMO_TAXONOMY = [[1, "Paper & Party Supplies › Paper › Calendars & Planners"], [2, "Paper & Party Supplies › Paper › Stationery › Worksheets"],
                 [3, "Books, Movies & Music › Books › Coloring Books"], [4, "Home & Living › Kitchen & Dining › Drink & Barware › Drinkware › Mugs"],
                 [5, "Home & Living › Home Decor › Vases"]]


def listing_options(cfg, shop_id, demo=False):
    """Co formulář pro nový listing potřebuje vědět o shopě: měnu, kategorie, profily dopravy a zpracování."""
    if demo:
        return {"zapis": True, "mena": "USD", "kategorie": DEMO_TAXONOMY,
                "doprava": [{"id": 11, "nazev": "Standard (CZ → svět)"}], "zpracovani": [{"id": 21, "nazev": "Made to order, 3–5 days"}]}
    tokens = load_tokens()
    if shop_id not in tokens:
        raise AppError("listing_no_shop", "Tahle shopa není přihlášená přes Etsy API.")
    out = {"zapis": can_write(tokens[shop_id]), "mena": "", "kategorie": [], "doprava": [], "zpracovani": [], "chyby": []}
    out["mena"] = api_get(cfg, tokens, shop_id, f"/shops/{shop_id}").get("currency_code", "")
    out["kategorie"] = taxonomy(cfg, tokens, shop_id)
    try:
        out["doprava"] = [{"id": p["shipping_profile_id"], "nazev": p.get("title") or str(p["shipping_profile_id"])}
                          for p in api_get(cfg, tokens, shop_id, f"/shops/{shop_id}/shipping-profiles").get("results", [])]
    except Exception as e:
        out["chyby"].append(str(e))
    try:
        for r in api_get(cfg, tokens, shop_id, f"/shops/{shop_id}/readiness-state-definitions").get("results", []):
            rid = r.get("readiness_state_id") or r.get("readiness_state_definition_id")
            lo = r.get("min_processing_days", r.get("min_processing_time"))
            hi = r.get("max_processing_days", r.get("max_processing_time"))
            unit = r.get("processing_time_unit") or "days"
            label = r.get("processing_days_display_label") or ("" if lo is None and hi is None else
                                                              f"{lo if lo is not None else hi}–{hi if hi is not None else lo} {unit}")
            if lo is not None and lo == hi and not r.get("processing_days_display_label"):
                label = f"{lo} {unit[:-1] if lo == 1 and unit.endswith('s') else unit}"
            out["zpracovani"].append({"id": rid, "stav": r.get("readiness_state") or "", "nazev": label})
    except Exception as e:
        out["chyby"].append(str(e))
    return out


DEMO_PROPERTIES = [
    {"property_id": 200, "name": "Primary color", "display_name": "Primary color", "is_required": False, "supports_attributes": True,
     "supports_variations": True, "is_multivalued": False, "max_values_allowed": None, "scales": [],
     "possible_values": [{"value_id": 1, "name": "Black"}, {"value_id": 2, "name": "White"}, {"value_id": 3, "name": "Green"}]},
    {"property_id": 100, "name": "Size", "display_name": "Size", "is_required": False, "supports_attributes": False,
     "supports_variations": True, "is_multivalued": False, "max_values_allowed": None,
     "scales": [{"scale_id": 1, "display_name": "Inches"}, {"scale_id": 2, "display_name": "Centimeters"}], "possible_values": []},
    {"property_id": 46803063641, "name": "Holiday", "display_name": "Holiday", "is_required": False, "supports_attributes": True,
     "supports_variations": False, "is_multivalued": True, "max_values_allowed": 5, "scales": [],
     "possible_values": [{"value_id": 35, "name": "Christmas"}, {"value_id": 36, "name": "Halloween"}, {"value_id": 37, "name": "Thanksgiving"}]},
]


def listing_properties(cfg, shop_id, taxonomy_id, demo=False):
    """Vlastnosti kategorie: co jde nastavit jako atribut a co jako variantu (barva, velikost…)."""
    if demo:
        props = DEMO_PROPERTIES
    else:
        tokens = load_tokens()
        if shop_id not in tokens:
            raise AppError("listing_no_shop", "Tahle shopa není přihlášená přes Etsy API.")
        props = api_get(cfg, tokens, shop_id, f"/seller-taxonomy/nodes/{int(taxonomy_id)}/properties").get("results", [])
    out = []
    for p in props:
        if not (p.get("supports_attributes") or p.get("supports_variations")):
            continue
        out.append({"id": p["property_id"], "nazev": p.get("display_name") or p.get("name") or str(p["property_id"]),
                    "povinne": bool(p.get("is_required")), "atribut": bool(p.get("supports_attributes")),
                    "varianta": bool(p.get("supports_variations")), "vice": bool(p.get("is_multivalued")),
                    "max": p.get("max_values_allowed"),
                    "skaly": [{"id": x["scale_id"], "nazev": x.get("display_name") or str(x["scale_id"])} for x in p.get("scales") or []],
                    "hodnoty": [{"id": v.get("value_id"), "nazev": v.get("name", ""), "skala": v.get("scale_id")}
                                for v in p.get("possible_values") or []]})
    return out


CUSTOM_PROPERTIES = (513, 514)  # vlastní varianty s vlastním názvem


def build_inventory(body, price, qty, readiness):
    """Etsy inventory z variant: kombinace hodnot → produkt s cenou, množstvím a SKU."""
    var = body.get("varianty") or {}
    props = var.get("vlastnosti") or []
    if not props:
        return None
    if len(props) > 2:
        raise AppError("listing_variants", "Listing může mít nejvýš 2 varianty.")
    custom = iter(CUSTOM_PROPERTIES)
    ids = []
    for p in props:
        pid = p.get("property_id")
        ids.append(next(custom) if pid in (None, "", "custom") else int(pid))
        if not p.get("hodnoty"):
            raise AppError("listing_variants", "Každá varianta potřebuje aspoň jednu hodnotu.")

    def num(v, default, cast):
        try:
            return cast(str(v).replace(",", ".")) if v not in (None, "") else default
        except ValueError:
            raise AppError("listing_variants", "Cena a množství u variant musí být čísla.")
    products = []
    for combo in var.get("kombinace") or []:
        values = []
        for i, p in enumerate(props):
            h = p["hodnoty"][int(combo["hodnoty"][i])]
            pv = {"property_id": ids[i], "property_name": str(p.get("nazev") or "").strip(),
                  "value_ids": [int(h["id"])] if h.get("id") not in (None, "") else [], "values": [str(h.get("nazev", "")).strip()]}
            if p.get("scale_id"):
                pv["scale_id"] = int(p["scale_id"])
            values.append(pv)
        offering = {"price": round(num(combo.get("cena"), price, float), 2), "quantity": num(combo.get("mnozstvi"), qty, int),
                    "is_enabled": combo.get("aktivni", True) is not False}
        if readiness:
            offering["readiness_state_id"] = readiness
        products.append({"sku": str(combo.get("sku") or "").strip(), "property_values": values, "offerings": [offering]})
    if not products or not any(p["offerings"][0]["is_enabled"] for p in products):
        raise AppError("listing_variants", "U variant musí být zapnutá aspoň jedna kombinace.")
    on = lambda k: [ids[int(i)] for i in var.get(k) or []]
    inv = {"products": products, "price_on_property": on("cena_dle"), "quantity_on_property": on("mnozstvi_dle"),
           "sku_on_property": on("sku_dle")}
    if readiness:
        inv["readiness_state_on_property"] = []
    return inv


MAX_FILE = 20 * 1024 * 1024


def _decode_files(items, what):
    out = []
    for it in items or []:
        try:
            data = base64.b64decode(it.get("data") or "", validate=False)
        except (ValueError, TypeError):
            raise AppError("listing_bad_file", f"Soubor {it.get('nazev')} se nepodařilo přečíst.", soubor=it.get("nazev", ""))
        if what == "file" and len(data) > MAX_FILE:
            raise AppError("listing_file_big", f"Soubor {it.get('nazev')} má víc než 20 MB, Etsy ho nepřijme.", soubor=it.get("nazev", ""))
        out.append((os.path.basename(it.get("nazev") or what), data))
    return out


def _shop_tokens(shop_id, need="w"):
    tokens = load_tokens()
    if shop_id not in tokens:
        raise AppError("listing_no_shop", "Tahle shopa není přihlášená přes Etsy API.")
    if need == "w" and not can_write(tokens[shop_id]):
        raise AppError("listing_relogin", "Shopa je přihlášená bez práva vytvářet listingy. V Nastavení ji přihlas znovu.")
    if need == "d" and not can_delete(tokens[shop_id]):
        raise AppError("listing_relogin_d", "Shopa je přihlášená bez práva mazat listingy. V Nastavení ji přihlas znovu.")
    return tokens


def refresh_listings(cfg, shop_id):
    """Po změně přes API stáhne listingy shopy znovu, aby stránka Listingy hned ukazovala novinky."""
    def run():
        with LOCK:
            try:
                tokens = load_tokens()
                con = db()
                sync_listings(cfg, tokens, con, shop_id, tokens[shop_id].get("shop_name", shop_id), int(time.time()))
                con.commit()
                con.close()
            except Exception as e:
                print(f"⚠️  listingy: {e}")
    threading.Thread(target=run, daemon=True).start()


def _image_items(items):
    """Obrázky v novém pořadí: {"id": …} je už nahraný na Etsy, {"nazev", "data"} je nový."""
    out = []
    for it in items or []:
        if it.get("id"):
            out.append(("id", int(it["id"])))
        else:
            out.append(("new", _decode_files([it], "image")[0]))
    return out


def save_listing(cfg, body):
    """Založí nový listing (bez listing_id), nebo upraví existující. Nahraje obrázky, soubory ke stažení,
    atributy a varianty, případně změní stav. Když selže až některý krok, listing zůstane a vrátí se seznam chyb."""
    shop_id = str(body.get("shop_id") or "")
    tokens = _shop_tokens(shop_id)
    lid = int(body["listing_id"]) if body.get("listing_id") else None
    digital = body.get("typ") != "physical"
    title = " ".join(str(body.get("nazev") or "").split())
    tags = [" ".join(str(t).split()) for t in body.get("stitky") or [] if str(t).strip()]
    try:
        price = round(float(str(body.get("cena")).replace(",", ".")), 2)
        qty = int(body.get("mnozstvi") or (999 if digital else 1))
        tax = int(body.get("kategorie"))
    except (TypeError, ValueError):
        raise AppError("listing_fields", "Vyplň název, cenu, množství a kategorii.")
    if not title or len(title) > 140 or price <= 0 or qty < 1:
        raise AppError("listing_fields", "Vyplň název (max 140 znaků), cenu a množství.")
    if len(tags) > 13 or any(len(t) > 20 for t in tags):
        raise AppError("listing_tags", "Etsy povoluje nejvýš 13 štítků, každý do 20 znaků.")
    images = _image_items(body.get("obrazky"))
    files = _image_items(body.get("soubory")) if digital else []  # stejný tvar: {"id"} nebo nový soubor
    for kind, val in files:
        if kind == "new" and len(val[1]) > MAX_FILE:
            raise AppError("listing_file_big", f"Soubor {val[0]} má víc než 20 MB, Etsy ho nepřijme.", soubor=val[0])
    if len(files) > 5:
        raise AppError("listing_files_many", "Digitální listing může mít nejvýš 5 souborů.")
    data = {"title": title, "description": str(body.get("popis") or "").strip() or title,
            "who_made": body.get("who_made") or "i_did", "when_made": body.get("when_made") or "made_to_order",
            "taxonomy_id": tax, "is_supply": False, "tags": tags, "type": "download" if digital else "physical"}
    readiness = None
    if not digital:
        if not body.get("doprava_id") or not body.get("zpracovani_id"):
            raise AppError("listing_physical", "Fyzický listing potřebuje profil dopravy a zpracování.")
        data["shipping_profile_id"] = int(body["doprava_id"])
        readiness = int(body["zpracovani_id"])
    inventory = build_inventory(body, price, qty, readiness)
    base = f"/shops/{shop_id}/listings"
    errors = []
    if lid is None:
        create = dict(data, price=price, quantity=qty)
        if readiness:
            create["readiness_state_id"] = readiness
        listing = api_send(cfg, tokens, shop_id, "POST", base, create)
        lid = listing["listing_id"]
        old_images, old_files, old_props = [], [], []
        if not inventory:
            inventory = None  # cena a množství jsou už v konceptu
    else:
        listing = api_send(cfg, tokens, shop_id, "PATCH", f"{base}/{lid}", data)
        old_images = [i["listing_image_id"] for i in sorted(api_get(cfg, tokens, shop_id, f"/listings/{lid}/images").get("results", []),
                                                            key=lambda i: i.get("rank", 0))]
        old_files = [f["listing_file_id"] for f in api_get(cfg, tokens, shop_id, f"{base}/{lid}/files").get("results", [])] \
            if digital or listing.get("type") == "download" else []
        old_props = [p["property_id"] for p in api_get(cfg, tokens, shop_id, f"{base}/{lid}/properties").get("results", [])]
        if not inventory:  # bez variant: jeden produkt s cenou a množstvím
            offering = {"price": price, "quantity": qty, "is_enabled": True}
            if readiness:
                offering["readiness_state_id"] = readiness
            inventory = {"products": [{"sku": str(body.get("sku") or "").strip(), "property_values": [], "offerings": [offering]}],
                         "price_on_property": [], "quantity_on_property": [], "sku_on_property": []}
    # obrázky: smazat odebrané, nahrát nové a seřadit
    keep = {v for k, v in images if k == "id"}
    for iid in old_images:
        if iid not in keep:
            try:
                api_send(cfg, tokens, shop_id, "DELETE", f"{base}/{lid}/images/{iid}")
            except Exception as e:
                errors.append(f"Obrázek {iid}: {e}")
    for rank, (kind, val) in enumerate(images, 1):
        try:
            if kind == "id":
                if val not in old_images or old_images.index(val) + 1 != rank:
                    api_send(cfg, tokens, shop_id, "POST", f"{base}/{lid}/images", {"listing_image_id": val, "rank": rank}, {})
            else:
                api_send(cfg, tokens, shop_id, "POST", f"{base}/{lid}/images", {"rank": rank}, {"image": val})
        except Exception as e:
            errors.append(f"{val[0] if kind == 'new' else val}: {e}")
    keep = {v for k, v in files if k == "id"}
    for fid in old_files:
        if fid not in keep:
            try:
                api_send(cfg, tokens, shop_id, "DELETE", f"{base}/{lid}/files/{fid}")
            except Exception as e:
                errors.append(f"Soubor {fid}: {e}")
    for rank, (kind, val) in enumerate(files, 1):
        if kind == "new":
            try:
                api_send(cfg, tokens, shop_id, "POST", f"{base}/{lid}/files", {"name": val[0], "rank": rank}, {"file": val})
            except Exception as e:
                errors.append(f"{val[0]}: {e}")
    # atributy (barva, materiál, svátek…): nastavit vyplněné, smazat vyprázdněné
    new_props = set()
    for a in body.get("atributy") or []:
        vals = {"value_ids": [int(x) for x in a.get("value_ids") or []], "values": [str(x) for x in a.get("values") or []]}
        if not vals["value_ids"] and not vals["values"]:
            continue
        if a.get("scale_id"):
            vals["scale_id"] = int(a["scale_id"])
        new_props.add(int(a["property_id"]))
        try:
            api_send(cfg, tokens, shop_id, "PUT", f"{base}/{lid}/properties/{int(a['property_id'])}", vals)
        except Exception as e:
            errors.append(f"{a.get('nazev') or a.get('property_id')}: {e}")
    var_props = {pv["property_id"] for p in (inventory or {}).get("products", []) for pv in p["property_values"]}
    for pid in old_props:
        if pid not in new_props and pid not in var_props:
            try:
                api_send(cfg, tokens, shop_id, "DELETE", f"{base}/{lid}/properties/{pid}")
            except Exception as e:
                errors.append(f"Atribut {pid}: {e}")
    if inventory:
        try:
            api_send(cfg, tokens, shop_id, "PUT", f"/listings/{lid}/inventory?legacy=false", inventory)
        except Exception as e:
            errors.append(f"Varianty: {e}")
    if isinstance(body.get("personalizace"), list):
        err = save_personalization(cfg, tokens, shop_id, lid, body["personalizace"])
        if err:
            errors.append(err)
    state = listing.get("state", "draft")
    want = body.get("stav") or ("active" if body.get("zverejnit") else None)
    if want and want != state and not errors:
        if want == "active" and (not images or (digital and not files)):
            errors.append("Ke zveřejnění chybí obrázek nebo soubor ke stažení, stav se nezměnil.")
        else:
            try:
                state = api_send(cfg, tokens, shop_id, "PATCH", f"{base}/{lid}", {"state": want}).get("state", want)
            except Exception as e:
                errors.append(str(e))
    refresh_listings(cfg, shop_id)
    return {"listing_id": lid, "stav": state, "chyby": errors,
            "url": listing.get("url") or f"https://www.etsy.com/listing/{lid}",
            "upravit": f"https://www.etsy.com/your/shops/me/listing-editor/edit/{lid}"}


PERSO_TYPES = ("text_input", "dropdown", "unlabeled_upload", "labeled_upload")


def _perso_out(q):
    return {"question_id": q.get("question_id"), "typ": q.get("question_type") or "text_input", "text": q.get("question_text") or "",
            "instr": q.get("instructions") or "", "req": bool(q.get("required")),
            "max": q.get("max_allowed_characters") or q.get("max_allowed_files") or None,
            "opts": [o.get("label", "") for o in q.get("options") or []]}


def get_personalization(cfg, tokens, shop_id, lid, listing):
    """Vlastní volby kupujícího (personalizace). Nové API umí až 5 otázek; když není dostupné, vezmou se starší pole listingu."""
    try:
        r = api_get(cfg, tokens, shop_id, f"/listings/{lid}/personalization")
        return [_perso_out(q) for q in r.get("personalization_questions") or []], True
    except Exception:
        pass
    if listing.get("is_personalizable"):
        return [{"question_id": None, "typ": "text_input", "text": "Personalization", "instr": listing.get("personalization_instructions") or "",
                 "req": bool(listing.get("personalization_is_required")), "max": listing.get("personalization_char_count_max") or 256,
                 "opts": []}], False
    return [], False


def save_personalization(cfg, tokens, shop_id, lid, items):
    qs = []
    for q in items[:5]:
        typ = q.get("typ") if q.get("typ") in PERSO_TYPES else "text_input"
        text = " ".join(str(q.get("text") or "").split())[:45]
        if not text:
            continue
        o = {"question_type": typ, "question_text": text, "required": bool(q.get("req"))}
        if q.get("question_id"):
            o["question_id"] = int(q["question_id"])
        opts = [{"label": str(x).strip()[:20 if typ == "dropdown" else 45]} for x in q.get("opts") or [] if str(x).strip()]
        if typ in ("text_input", "unlabeled_upload") and str(q.get("instr") or "").strip():
            o["instructions"] = str(q["instr"]).strip()[:120]
        if typ == "text_input":
            o["max_allowed_characters"] = max(1, min(1024, int(q.get("max") or 256)))
        if typ == "unlabeled_upload":
            o["max_allowed_files"] = max(1, min(10, int(q.get("max") or 1)))
        if typ == "dropdown":
            o["options"] = opts[:30]
        if typ == "labeled_upload":
            o["options"] = opts[:10]
            o["max_allowed_files"] = len(o["options"])
        if typ in ("dropdown", "labeled_upload") and not o["options"]:
            return f"Vlastní volby: otázka „{text}“ nemá žádné možnosti."
        qs.append(o)
    path = f"/shops/{shop_id}/listings/{lid}/personalization?supports_multiple_personalization_questions=true"
    try:
        if qs:
            api_send(cfg, tokens, shop_id, "POST", path, {"personalization_questions": qs})
        else:
            api_send(cfg, tokens, shop_id, "DELETE", path)
        return None
    except Exception as e:
        new_err = str(e)
    # starší způsob: jen jedno textové pole
    if len(qs) > 1 or (qs and qs[0]["question_type"] != "text_input"):
        return f"Vlastní volby: {new_err}"
    legacy = {"is_personalizable": bool(qs)}
    if qs:
        legacy.update({"personalization_is_required": qs[0]["required"], "personalization_char_count_max": qs[0]["max_allowed_characters"],
                       "personalization_instructions": qs[0].get("instructions") or qs[0]["question_text"]})
    try:
        api_send(cfg, tokens, shop_id, "PATCH", f"/shops/{shop_id}/listings/{lid}", legacy)
        return None
    except Exception as e:
        return f"Vlastní volby: {new_err}; {e}"


def _money(m):
    if isinstance(m, dict):
        return round(m.get("amount", 0) / (m.get("divisor") or 100), 2)
    return float(m or 0)


def listing_detail(cfg, shop_id, lid, demo=False):
    """Všechno, co editor potřebuje k existujícímu listingu: texty, obrázky, soubory, atributy a varianty."""
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    tokens = _shop_tokens(shop_id, need="r")
    lid = int(lid)
    l = api_get(cfg, tokens, shop_id, f"/listings/{lid}", {"includes": "Images"})
    inv = api_get(cfg, tokens, shop_id, f"/listings/{lid}/inventory", {"legacy": "false"})
    files = []
    if l.get("type") == "download":
        files = [{"id": f["listing_file_id"], "nazev": f.get("filename", ""), "velikost": f.get("filesize")}
                 for f in api_get(cfg, tokens, shop_id, f"/shops/{shop_id}/listings/{lid}/files").get("results", [])]
    props = api_get(cfg, tokens, shop_id, f"/shops/{shop_id}/listings/{lid}/properties").get("results", [])
    perso, perso_new = get_personalization(cfg, tokens, shop_id, lid, l)
    products = []
    for p in inv.get("products", []):
        if p.get("is_deleted"):
            continue
        o = (p.get("offerings") or [{}])[0]
        products.append({"sku": p.get("sku") or "", "hodnoty": [{"property_id": v["property_id"], "nazev": v.get("property_name", ""),
                                                                 "scale_id": v.get("scale_id"), "value_id": (v.get("value_ids") or [None])[0],
                                                                 "hodnota": (v.get("values") or [""])[0]} for v in p.get("property_values") or []],
                         "cena": _money(o.get("price")), "mnozstvi": o.get("quantity"), "aktivni": o.get("is_enabled", True),
                         "zpracovani": o.get("readiness_state_id")})
    first = products[0] if products else {}
    return {"listing_id": lid, "shop_id": shop_id, "stav": l.get("state"), "typ": "digital" if l.get("type") == "download" else "physical",
            "nazev": html.unescape(l.get("title") or ""), "popis": html.unescape(l.get("description") or ""),
            "stitky": [html.unescape(t) for t in l.get("tags") or []], "kategorie": l.get("taxonomy_id"),
            "cena": first.get("cena", _money(l.get("price"))), "mnozstvi": l.get("quantity"),
            "doprava": l.get("shipping_profile_id"), "zpracovani": first.get("zpracovani") or l.get("readiness_state_id"),
            "url": l.get("url"), "obrazky": [{"id": i["listing_image_id"], "url": i.get("url_570xN") or i.get("url_fullxfull", "")}
                                             for i in sorted(l.get("images") or [], key=lambda i: i.get("rank", 0))],
            "soubory": files, "atributy": [{"property_id": p["property_id"], "nazev": p.get("property_name", ""),
                                            "value_ids": p.get("value_ids") or [], "values": p.get("values") or [],
                                            "scale_id": p.get("scale_id")} for p in props],
            "produkty": products, "cena_dle": inv.get("price_on_property") or [], "mnozstvi_dle": inv.get("quantity_on_property") or [],
            "sku_dle": inv.get("sku_on_property") or [], "personalizace": perso, "personalizace_nove": perso_new}


def listings_state(cfg, body):
    """Hromadná změna stavu (active / inactive) nebo smazání (stav "smazat")."""
    shop_id, want = str(body.get("shop_id") or ""), body.get("stav")
    if want not in ("active", "inactive", "smazat"):
        raise AppError("listing_fields", "Neznámá akce.")
    tokens = _shop_tokens(shop_id, need="d" if want == "smazat" else "w")
    out = []
    for lid in body.get("ids") or []:
        try:
            if want == "smazat":
                api_send(cfg, tokens, shop_id, "DELETE", f"/listings/{int(lid)}")
                con = db()
                con.execute("DELETE FROM listingy WHERE listing_id=?", (int(lid),))
                con.commit()
                con.close()
            else:
                api_send(cfg, tokens, shop_id, "PATCH", f"/shops/{shop_id}/listings/{int(lid)}", {"state": want})
            out.append({"listing_id": lid, "ok": True})
        except Exception as e:
            out.append({"listing_id": lid, "ok": False, "chyba": str(e)})
    refresh_listings(cfg, shop_id)
    return {"vysledky": out}


# ------------------------------------------------------------------ slevy
# Etsy API neumí Sales & Discounts ani kupóny. Sleva se proto dělá změnou ceny: v den začátku
# dashboard sníží ceny všech variant o zadaná procenta, po konci je vrátí (jen ty, které mezitím nikdo nezměnil).

def _inv_payload(inv, price_fn):
    products = []
    for p in inv.get("products", []):
        if p.get("is_deleted"):
            continue
        offerings = []
        for o in p.get("offerings") or []:
            if o.get("is_deleted"):
                continue
            off = {"price": price_fn(p, _money(o.get("price"))), "quantity": o.get("quantity", 0), "is_enabled": o.get("is_enabled", True)}
            if o.get("readiness_state_id"):
                off["readiness_state_id"] = o["readiness_state_id"]
            offerings.append(off)
        products.append({"sku": p.get("sku") or "", "offerings": offerings,
                         "property_values": [{k: v[k] for k in ("property_id", "property_name", "scale_id", "value_ids", "values") if v.get(k) is not None}
                                             for v in p.get("property_values") or []]})
    out = {"products": products}
    for k in ("price_on_property", "quantity_on_property", "sku_on_property", "readiness_state_on_property"):
        if inv.get(k) is not None:
            out[k] = inv[k]
    return out


def _pkey(p):
    return json.dumps([[v.get("property_id"), v.get("values")] for v in p.get("property_values") or []])


def add_discount(cfg, body, db_path=None):
    shop_id = str(body.get("shop_id") or "")
    _shop_tokens(shop_id)
    try:
        pct = float(str(body.get("procento")).replace(",", "."))
        start = int(datetime.strptime(body["od"], "%Y-%m-%d").timestamp())
        end = int((datetime.strptime(body["do"], "%Y-%m-%d") + timedelta(days=1)).timestamp())  # včetně posledního dne
    except (KeyError, TypeError, ValueError):
        raise AppError("sale_fields", "Vyplň slevu v procentech a data od a do.")
    if not 0 < pct < 100 or end <= start or end <= time.time():
        raise AppError("sale_fields", "Sleva musí být mezi 0 a 100 % a konec nesmí být před začátkem ani v minulosti.")
    ids = [int(x) for x in body.get("ids") or []]
    con = db(db_path)
    try:
        busy = [lid for lid in ids if con.execute("SELECT 1 FROM slevy WHERE listing_id=? AND stav IN ('naplanovano','bezi') "
                                                  "AND od_ts < ? AND do_ts > ?", (lid, end, start)).fetchone()]
        if busy:
            raise AppError("sale_overlap", f"{len(busy)} z vybraných listingů už má v tomhle termínu slevu.", n=len(busy))
        for lid in ids:
            con.execute("INSERT INTO slevy (shop_id, listing_id, procento, od_ts, do_ts, stav, vytvoreno_ts) VALUES (?,?,?,?,?,?,?)",
                        (shop_id, lid, pct, start, end, "naplanovano", int(time.time())))
        con.commit()
    finally:
        con.close()
    threading.Thread(target=process_discounts, args=(cfg,), daemon=True).start()
    return {"ok": True, "pocet": len(ids)}


def cancel_discount(cfg, body, db_path=None):
    con = db(db_path)
    try:
        row = con.execute("SELECT stav FROM slevy WHERE id=?", (int(body.get("id")),)).fetchone()
        if row and row[0] == "naplanovano":
            con.execute("UPDATE slevy SET stav='zruseno' WHERE id=?", (int(body["id"]),))
        elif row and row[0] == "bezi":
            con.execute("UPDATE slevy SET do_ts=? WHERE id=?", (int(time.time()), int(body["id"])))  # ukončí se hned
        con.commit()
    finally:
        con.close()
    process_discounts(cfg)
    return {"ok": True}


def process_discounts(cfg):
    """Spustí naplánované slevy a ukončí ty, kterým vypršel termín. Volá se při každé kontrole."""
    if not config_ready(cfg):
        return
    with DISCOUNT_LOCK:
        con = db()
        tokens = load_tokens()
        now = int(time.time())
        touched = set()
        for sid, shop_id, lid, pct, stav, puvodni, nove in con.execute(
                "SELECT id, shop_id, listing_id, procento, stav, puvodni, nove FROM slevy WHERE "
                "(stav='naplanovano' AND od_ts<=?) OR (stav='bezi' AND do_ts<=?)", (now, now)).fetchall():
            if shop_id not in tokens:
                continue
            try:
                inv = api_get(cfg, tokens, shop_id, f"/listings/{lid}/inventory", {"legacy": "false"})
                if stav == "naplanovano":
                    if con.execute("SELECT do_ts FROM slevy WHERE id=?", (sid,)).fetchone()[0] <= now:
                        con.execute("UPDATE slevy SET stav='hotovo' WHERE id=?", (sid,))  # termín propásnutý (Mac byl vypnutý)
                        continue
                    old, new = {}, {}

                    def cut(p, price):
                        old[_pkey(p)] = price
                        new[_pkey(p)] = max(0.2, round(price * (1 - pct / 100), 2))
                        return new[_pkey(p)]
                    api_send(cfg, tokens, shop_id, "PUT", f"/listings/{lid}/inventory?legacy=false", _inv_payload(inv, cut))
                    con.execute("UPDATE slevy SET stav='bezi', puvodni=?, nove=?, chyba=NULL WHERE id=?",
                                (json.dumps(old), json.dumps(new), sid))
                else:
                    old, new = json.loads(puvodni or "{}"), json.loads(nove or "{}")
                    back = lambda p, price: old[_pkey(p)] if _pkey(p) in old and abs(new.get(_pkey(p), -1) - price) < 0.005 else price
                    api_send(cfg, tokens, shop_id, "PUT", f"/listings/{lid}/inventory?legacy=false", _inv_payload(inv, back))
                    con.execute("UPDATE slevy SET stav='hotovo', chyba=NULL WHERE id=?", (sid,))
                touched.add(shop_id)
            except Exception as e:
                con.execute("UPDATE slevy SET chyba=? WHERE id=?", (str(e)[:300], sid))
            con.commit()
        con.commit()
        con.close()
    for shop_id in touched:
        refresh_listings(cfg, shop_id)


def run_check(cfg):
    """Zkontroluje všechny shopy. Vrací seznam novinek."""
    if not config_ready(cfg):
        return []
    with LOCK:
        STATUS["bezi"] = True
        try:
            tokens = load_tokens()
            con = db()
            all_news = []
            for shop_id in list(tokens):
                name = tokens[shop_id].get("shop_name", shop_id)
                try:
                    all_news.extend(check_shop(cfg, tokens, con, shop_id))
                    STATUS["chyby"].pop(name, None)
                except Exception as e:
                    STATUS["chyby"][name] = str(e)
                    print(f"⚠️  {name}: {e}")
            con.close()
            STATUS["posledni_kontrola"] = int(time.time())
        finally:
            STATUS["bezi"] = False
    try:
        process_discounts(cfg)
    except Exception as e:
        print(f"⚠️  slevy: {e}")
    if all_news:
        notify(cfg, all_news)
    return all_news


def notify(cfg, lines):
    for line in lines:
        print(line)
    topic = cfg.get("ntfy_topic")
    if not topic or not lines:
        return
    body = "\n".join(lines[:20]) + ("\n" + tr(cfg, "more", n=len(lines) - 20) if len(lines) > 20 else "")
    req = urllib.request.Request(f"https://ntfy.sh/{urllib.parse.quote(topic)}", data=body.encode("utf-8"),
                                 method="POST", headers={"Title": tr(cfg, "title"), "Tags": "shopping_cart"})
    try:
        urllib.request.urlopen(req, timeout=30, context=SSL_CTX).close()
    except Exception as e:
        print(f"(Nepodařilo se poslat notifikaci na telefon: {e})")


def version_tuple(v):
    return tuple(int(x) for x in str(v).split(".") if x.isdigit())


def restart():
    """Na Macu (služba) stačí skončit, launchd aplikaci hned spustí znovu s novým kódem."""
    time.sleep(1)
    if "--sluzba" in sys.argv:
        os._exit(0)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def check_update(apply=True):
    """Podívá se na GitHub, jestli není novější verze; pokud ano, stáhne ji a restartuje se."""
    STATUS["posledni_aktualizace"] = int(time.time())
    info = http_json("GET", UPDATE_BASE + "version.json?t=" + str(int(time.time())))
    new = str(info.get("verze", ""))
    if version_tuple(new) <= version_tuple(VERSION):
        return {"nova": False, "verze": VERSION}
    if not apply:
        return {"nova": True, "verze": new}
    files = {}
    for name in info.get("soubory", ["etsy_dashboard.py", "dashboard.html"]):
        if "/" in name or name.startswith("."):
            continue
        with urllib.request.urlopen(UPDATE_BASE + name, timeout=60, context=SSL_CTX) as resp:
            files[name] = resp.read()
    compile(files.get("etsy_dashboard.py", b""), "etsy_dashboard.py", "exec")  # rozbitý soubor nenahrajeme
    for name, data in files.items():
        tmp = os.path.join(BASE_DIR, name + ".novy")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(BASE_DIR, name))
    print(f"Aktualizováno na verzi {new}, restartuji…")
    threading.Thread(target=restart, daemon=True).start()
    return {"nova": True, "verze": new, "nainstalovano": True}


def watcher_loop():
    while True:
        cfg = load_config()
        try:
            run_check(cfg)
        except Exception as e:
            print(f"⚠️  Chyba kontroly: {e}")
        if time.time() - (STATUS.get("posledni_aktualizace") or 0) > UPDATE_EVERY:
            try:
                check_update()
            except Exception as e:
                print(f"(Kontrola aktualizací se nepovedla: {e})")
        time.sleep(max(5, int(cfg.get("interval_minut", 15))) * 60)


# ------------------------------------------------------------- směnné kurzy

def get_rates():
    """Denní kurzy ECB (1 EUR = x měny), uložené v data/kurzy.json. Při chybě vrátí poslední uložené."""
    cached = {}
    if os.path.exists(RATES_PATH):
        try:
            with open(RATES_PATH, encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            cached = {}
    if time.time() - cached.get("stazeno", 0) < RATES_EVERY:
        return cached
    try:
        with urllib.request.urlopen(RATES_URL, timeout=15, context=SSL_CTX) as resp:
            xml = resp.read().decode("utf-8", "replace")
        rates = {c: float(r) for c, r in _re.findall(r"currency=['\"](\w{3})['\"]\s+rate=['\"]([\d.]+)['\"]", xml)}
        if not rates:
            raise ValueError("v odpovědi ECB nejsou kurzy")
        rates["EUR"] = 1.0
        day = _re.search(r"time=['\"]([\d-]+)['\"]", xml)
        cached = {"datum": day.group(1) if day else "", "stazeno": int(time.time()), "kurzy": rates}
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(RATES_PATH, "w", encoding="utf-8") as f:
            json.dump(cached, f)
    except Exception as e:
        print(f"(Kurzy ECB se nepodařilo stáhnout: {e})")
    return cached


# -------------------------------------------------------------- data pro web

def rows_as_dicts(con, sql):
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def dashboard_data(db_path=None):
    con = db(db_path)
    data = {
        "objednavky": rows_as_dicts(con, "SELECT * FROM objednavky ORDER BY vytvoreno_ts DESC"),
        "vypis": rows_as_dicts(con, "SELECT * FROM vypis ORDER BY datum_ts DESC, entry_id DESC"),
        "listingy": rows_as_dicts(con, "SELECT * FROM listingy ORDER BY nazev"),
        "doprava": rows_as_dicts(con, "SELECT * FROM doprava"),
        "slevy": rows_as_dicts(con, "SELECT id, shop_id, listing_id, procento, od_ts, do_ts, stav, chyba FROM slevy "
                                    "WHERE stav IN ('naplanovano','bezi') OR do_ts > strftime('%s','now') - 30*86400 ORDER BY od_ts"),
    }
    con.close()
    return data


CSV_HEADERS = {
    "cs": {"dopravce": "Dopravce", "cislo_zasilky": "Číslo zásilky", "cena_dopravy": "Cena dopravy", "mena_dopravy": "Měna dopravy", "listing_id": "ID listingu", "nazev": "Název", "cena": "Cena", "mnozstvi": "Skladem", "zobrazeni": "Zobrazení", "oblibene": "Oblíbené", "stitky": "Štítky", "sku": "SKU", "url": "Odkaz", "shop": "Shopa", "receipt_id": "Číslo objednávky", "vytvoreno_ts": "Datum", "zakaznik": "Zákazník",
           "polozky": "Položky", "celkem": "Celkem", "mena": "Měna", "zaplaceno": "Zaplaceno",
           "odeslano": "Odesláno", "stav": "Stav", "entry_id": "ID pohybu", "datum_ts": "Datum",
           "typ": "Typ", "popis": "Popis", "castka": "Částka", "zustatek": "Zůstatek",
           "reference": "Reference"},
    "en": {"dopravce": "Carrier", "cislo_zasilky": "Tracking number", "cena_dopravy": "Shipping cost", "mena_dopravy": "Shipping currency", "listing_id": "Listing ID", "nazev": "Title", "cena": "Price", "mnozstvi": "Quantity", "zobrazeni": "Views", "oblibene": "Favorites", "stitky": "Tags", "sku": "SKU", "url": "URL", "shop": "Shop", "receipt_id": "Order ID", "vytvoreno_ts": "Date", "zakaznik": "Buyer",
           "polozky": "Items", "celkem": "Total", "mena": "Currency", "zaplaceno": "Paid",
           "odeslano": "Shipped", "stav": "Status", "entry_id": "Entry ID", "datum_ts": "Date",
           "typ": "Type", "popis": "Description", "castka": "Amount", "zustatek": "Balance",
           "reference": "Reference"},
    "de": {"dopravce": "Versanddienst", "cislo_zasilky": "Sendungsnummer", "cena_dopravy": "Versandkosten", "mena_dopravy": "Versandwährung", "listing_id": "Angebots-ID", "nazev": "Titel", "cena": "Preis", "mnozstvi": "Bestand", "zobrazeni": "Aufrufe", "oblibene": "Favoriten", "stitky": "Tags", "sku": "SKU", "url": "Link", "shop": "Shop", "receipt_id": "Bestellnr.", "vytvoreno_ts": "Datum", "zakaznik": "Kunde",
           "polozky": "Artikel", "celkem": "Gesamt", "mena": "Währung", "zaplaceno": "Bezahlt",
           "odeslano": "Versandt", "stav": "Status", "entry_id": "Buchungsnr.", "datum_ts": "Datum",
           "typ": "Typ", "popis": "Beschreibung", "castka": "Betrag", "zustatek": "Saldo",
           "reference": "Referenz"},
}


def csv_export(kind, db_path=None, lang="cs"):
    """CSV pro Excel (UTF-8 s BOM). Česky a německy středník a desetinná čárka, anglicky čárka a tečka."""
    con = db(db_path)
    if kind == "objednavky":
        cols = ["shop", "receipt_id", "vytvoreno_ts", "zakaznik", "polozky", "celkem", "mena",
                "zaplaceno", "odeslano", "stav", "dopravce", "cislo_zasilky", "cena_dopravy", "mena_dopravy"]
        rows = con.execute("SELECT o.shop, o.receipt_id, o.vytvoreno_ts, o.zakaznik, o.polozky, o.celkem, o.mena, "
                           "o.zaplaceno, o.odeslano, o.stav, d.dopravce, d.cislo, d.cena, d.mena FROM objednavky o "
                           "LEFT JOIN doprava d ON d.receipt_id=o.receipt_id ORDER BY o.shop, o.vytvoreno_ts").fetchall()
    elif kind == "listingy":
        cols = ["shop", "listing_id", "nazev", "stav", "cena", "mena", "mnozstvi", "zobrazeni", "oblibene",
                "stitky", "sku", "url"]
        rows = con.execute(f"SELECT {','.join(cols)} FROM listingy ORDER BY shop, nazev").fetchall()
        rows = [(r[0], r[1] if r[1] > 0 else "") + tuple(r[2:]) for r in rows]
    else:
        cols = ["shop", "entry_id", "datum_ts", "typ", "popis", "castka", "mena", "zustatek", "reference"]
        rows = con.execute(f"SELECT {','.join(cols)} FROM vypis ORDER BY shop, datum_ts").fetchall()
    con.close()
    buf = io.StringIO()
    comma = lang == "en"
    w = csv.writer(buf, delimiter="," if comma else ";")
    names = CSV_HEADERS.get(lang, CSV_HEADERS["cs"])
    w.writerow([names.get(c, c.replace("_ts", "")) for c in cols])
    for row in rows:
        out = []
        for c, v in zip(cols, row):
            if c.endswith("_ts"):
                v = datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M") if v else ""
            elif isinstance(v, float):
                v = f"{v:.2f}" if comma else f"{v:.2f}".replace(".", ",")
            out.append(v)
        w.writerow(out)
    return ("﻿" + buf.getvalue()).encode("utf-8")


# -------------------------------------------------------------- ukázková data

def save_shipping(body, db_path=None):
    try:
        rid = int(body.get("receipt_id"))
    except (TypeError, ValueError):
        raise AppError("ship_bad", "Chybí číslo objednávky.")
    cena = body.get("cena")
    if cena in (None, ""):
        cena = None
    else:
        try:
            cena = round(float(str(cena).replace(",", ".").replace(" ", "")), 2)
        except ValueError:
            raise AppError("ship_price", "Cena dopravy musí být číslo.")
    vals = [str(body.get(k) or "").strip()[:200] for k in ("dopravce", "cislo", "mena")]
    con = db(db_path)
    try:
        if not vals[0] and not vals[1] and cena is None:
            con.execute("DELETE FROM doprava WHERE receipt_id=?", (rid,))
        else:
            con.execute("INSERT OR REPLACE INTO doprava VALUES (?,?,?,?,?,?)",
                        (rid, vals[0], vals[1], cena, vals[2].upper(), int(time.time())))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


def make_demo_db(path):
    if os.path.exists(path):
        os.remove(path)
    con = db(path)
    rnd = random.Random(7)
    now = datetime.now()
    names = ["Emily R.", "Jessica M.", "Sarah K.", "Ashley T.", "Megan B.", "Laura P.", "Chris D.", "Hannah W."]
    shops = {
        "DemoPrintables": ["Weekly Planner", "Budget Tracker", "Habit Tracker",
                           "Kids Coloring Pages", "Meal Planner", "Wedding Checklist"],
        "DemoHandmade": ["Ceramic Mug", "Linen Tote Bag", "Custom Name Necklace",
                         "Wooden Coaster Set", "Scented Candle"],
    }
    rid, eid, lid = 3000000000, 900000, 1500000000
    for shop, products in shops.items():
        for i, p in enumerate(products):
            lid += 1
            digital = shop == "DemoPrintables"
            state = "draft" if p == "Wedding Checklist" else "sold_out" if p == "Scented Candle" else "active"
            con.execute("INSERT INTO listingy VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                shop, lid, p, state, rnd.choice([3.49, 4.99, 6.99, 8.99]) if digital else rnd.choice([12.9, 18.5, 24.0, 35.0]),
                "USD", 999 if digital else (0 if state == "sold_out" else rnd.randint(1, 25)),
                0 if state == "draft" else rnd.randint(150, 4200), 0 if state == "draft" else rnd.randint(5, 380),
                ", ".join(rnd.sample(["printable", "gift", "planner", "minimalist", "handmade", "custom", "home decor", "for her"], 3)),
                "", f"https://www.etsy.com/listing/{lid}", f"{shop[4:7].upper()}-{i + 1:03d}",
                int((now - timedelta(days=rnd.randint(60, 700))).timestamp()), int(time.time()), 0,
                f"{p} from {shop}.\n\nThis is demo listing text. The real description comes from the Etsy API or the listings CSV."))
    for shop, products in shops.items():
        digital = shop == "DemoPrintables"
        balance = 0.0
        for d in range(365, -1, -1):
            day = now - timedelta(days=d)
            n = rnd.choice([0, 0, 1, 1, 2, 3]) if digital else rnd.choice([0, 0, 0, 1, 1, 2])
            n += (1 if day.month in (10, 11, 12) and rnd.random() < .5 else 0)
            for _ in range(n):
                rid += 1
                ts = int((day.replace(hour=rnd.randint(7, 22), minute=rnd.randint(0, 59))).timestamp())
                if ts > time.time():
                    ts = int(time.time()) - rnd.randint(60, 3000)
                p = rnd.choice(products)
                qty = 1 if digital else rnd.choice([1, 1, 2])
                price = rnd.choice([3.49, 4.99, 6.99, 8.99]) if digital else rnd.choice([12.9, 18.5, 24.0, 35.0])
                total = round(price * qty + (0 if digital else 4.5), 2)
                shipped = digital or d > 3 or rnd.random() < .3
                status = "Completed" if shipped else "Paid"
                con.execute("INSERT INTO objednavky VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    shop, rid, ts, rnd.choice(names), f"{qty}x {p}", total, "USD", 1, int(shipped),
                    status, ts, ts if d <= 1 else 0))
                for typ, desc, amt in (("sale", f"Payment for order #{rid}", total),
                                       ("transaction", f"Transaction fee: {p}", -round(total * .065, 2)),
                                       ("processing", "Processing fee", -round(total * .03 + .25, 2)),
                                       ("listing", f"Listing fee: {p}", -0.20)):
                    eid += 1
                    balance += amt
                    con.execute("INSERT INTO vypis VALUES (?,?,?,?,?,?,?,?,?,?)", (
                        shop, eid, ts + 5, typ, desc, amt, "USD", round(balance, 2), f"receipt {rid}",
                        ts if d <= 1 else 0))
            if day.weekday() == 0 and balance > 20:
                eid += 1
                amt = -round(balance, 2)
                balance = 0.0
                ts = int(day.replace(hour=6, minute=0).timestamp())
                con.execute("INSERT INTO vypis VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    shop, eid, ts, "DISBURSE2", "Deposit to your bank account", amt, "USD", 0.0, "", 0))
            if day.day == 1:
                eid += 1
                amt = -rnd.choice([9.99, 14.5, 22.3])
                balance += amt
                ts = int(day.replace(hour=5, minute=0).timestamp())
                con.execute("INSERT INTO vypis VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    shop, eid, ts, "offsite_ads_fee", "Etsy Ads", amt, "USD", round(balance, 2), "", 0))
    for (rid, ts) in con.execute("SELECT receipt_id, vytvoreno_ts FROM objednavky WHERE shop='DemoHandmade' AND odeslano=1").fetchall():
        if rnd.random() < .8:
            carrier = rnd.choice(["Zásilkovna", "Česká pošta", "PPL", "DPD"])
            con.execute("INSERT INTO doprava VALUES (?,?,?,?,?,?)", (rid, carrier, f"Z{rnd.randint(10**9, 10**10 - 1)}",
                        rnd.choice([3.2, 3.9, 4.6, 5.8]), "USD", ts))
    con.commit()
    con.close()


# ------------------------------------------------------------- import CSV z Etsy

import re as _re

MONTH_FMTS = ("%B %d, %Y", "%b %d, %Y", "%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%d.%m.%Y")


def parse_date(s):
    s = (s or "").strip()
    for fmt in MONTH_FMTS:
        try:
            return int(datetime.strptime(s, fmt).replace(hour=12).timestamp())
        except ValueError:
            pass
    return 0


def parse_money(s):
    """'-€0.16' → -0.16, '€1,234.50' → 1234.5, '--' nebo '' → None."""
    s = (s or "").strip()
    if not s or s == "--":
        return None
    neg = s.startswith("-") or s.startswith("(")
    num = _re.sub(r"[^0-9.]", "", s.replace(",", ""))
    if not num:
        return None
    v = float(num)
    return -v if neg else v


def csv_id(*parts):
    """Stabilní záporné ID pro řádky z CSV (opakovaný import nic nezdvojí)."""
    h = hashlib.sha1("|".join(map(str, parts)).encode("utf-8")).digest()
    return -(int.from_bytes(h[:7], "big") + 1)


def statement_type(typ, title):
    t, ti = typ.lower(), title.lower()
    if t == "sale":
        return "sale"
    if t == "deposit":
        return "DISBURSE"
    if t == "refund":
        return "refund"
    if t == "vat":
        return "vat"
    if t == "marketing":
        return "etsy_ads"
    if t == "fee":
        if ti.startswith("transaction fee: shipping"):
            return "shipping_transaction"
        if ti.startswith("transaction fee"):
            return "transaction"
        if ti.startswith("processing fee"):
            return "processing"
        if ti.startswith("listing fee"):
            return "listing"
    return t or "ostatni"


def import_statement(con, shop, rows):
    """Měsíční výpis (Payment Account / etsy_statement_RRRR_M.csv)."""
    seen, added = {}, 0
    orders = {}
    for r in rows:
        date, typ, title, info = r.get("Date", ""), r.get("Type", ""), r.get("Title", ""), r.get("Info", "")
        net = parse_money(r.get("Net"))
        if net is None:
            net = parse_money(r.get("Amount"))
        if typ.lower() == "deposit" and net is None:  # „€62.57 sent to your bank account“
            m = _re.search(r"([-]?[^\d\s-]?[\d,]+\.\d{2})", title)
            net = -abs(parse_money(m.group(1))) if m else 0.0
        if net is None:
            net = 0.0
        key = (date, typ, title, info, r.get("Net", ""))
        seen[key] = seen.get(key, 0) + 1
        eid = csv_id(shop, *key, seen[key])
        ts = parse_date(date)
        cur = r.get("Currency", "")
        order = _re.search(r"Order #(\d+)", title + " " + info)
        cur_ = con.execute("INSERT OR IGNORE INTO vypis VALUES (?,?,?,?,?,?,?,?,?,?)", (
            shop, eid, ts, statement_type(typ, title), title, net, cur, None, info, 0))
        added += cur_.rowcount
        if order:
            o = orders.setdefault(int(order.group(1)), {"ts": ts, "cur": cur, "total": 0.0, "items": {}})
            if typ.lower() == "sale":
                o["total"] += net
                o["ts"] = ts
            elif title.startswith("Transaction fee: ") and not title.startswith("Transaction fee: Shipping"):
                name = fee_item_name(title)
                o["items"][name] = o["items"].get(name, 0) + 1
    # objednávky odvozené z výpisu (bez jména zákazníka); CSV objednávek je pak přepíše
    for rid, o in orders.items():
        if not o["total"]:
            continue
        items = csv_items(con, rid) or "; ".join(f"{q}x {n}" for n, q in o["items"].items())
        con.execute("INSERT OR IGNORE INTO objednavky VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            shop, rid, o["ts"], "", items, round(o["total"], 2), o["cur"], 1, None, "Zaplaceno", o["ts"], 0))
        if items:
            con.execute("UPDATE objednavky SET polozky=? WHERE receipt_id=? AND polozky=''", (items, rid))
    fill_items_from_ledger(con)
    return added


def fee_item_name(title):
    """„Transaction fee: Design 3D Cover | ...“ → „Design 3D Cover“ (Etsy názvy ve výpisu zkracuje)."""
    name = title[len("Transaction fee: "):].strip()
    name = _re.sub(r"\s*\|\s*\.\.\.$", "", name)
    return _re.sub(r"\s*\.\.\.$", "…", name)


def fill_items_from_ledger(con):
    """Objednávky, u kterých známe jen počet kusů („3 ks“), doplní názvy produktů
    z řádků „Transaction fee: …“ v nahraném výpisu (pořadí nahrávání souborů pak nehraje roli)."""
    todo = con.execute("SELECT receipt_id, polozky FROM objednavky WHERE polozky='' OR polozky GLOB '[0-9]* ks'").fetchall()
    for rid, old in todo:
        names = {}
        for (title,) in con.execute("SELECT popis FROM vypis WHERE popis LIKE 'Transaction fee: %' "
                                    "AND popis NOT LIKE 'Transaction fee: Shipping%' AND reference LIKE ?",
                                    (f"%Order #{rid}%",)):
            name = fee_item_name(title)
            names[name] = names.get(name, 0) + 1
        if not names:
            continue
        count = _re.match(r"(\d+) ks$", old or "")
        if count and len(names) == 1:  # jeden produkt: přesný počet kusů je z CSV objednávek
            names = {next(iter(names)): int(count.group(1))}
        items = "; ".join(f"{q}x {n}" for n, q in names.items())
        con.execute("UPDATE objednavky SET polozky=? WHERE receipt_id=?", (items, rid))


def csv_items(con, rid):
    row = con.execute("SELECT polozky FROM csv_polozky WHERE receipt_id=?", (rid,)).fetchone()
    return row[0] if row else ""


def pick(r, *names):
    for n in names:
        if n in r and r[n] not in (None, ""):
            return r[n]
    return ""


def import_orders(con, shop, rows):
    """Download Data → Orders (EtsySoldOrders*.csv)."""
    added, now = 0, int(time.time())
    for r in rows:
        rid = pick(r, "Order ID", "Order Id")
        if not rid.strip().isdigit():
            continue
        rid = int(rid)
        old = con.execute("SELECT polozky, pridano_ts FROM objednavky WHERE receipt_id=?", (rid,)).fetchone()
        total = parse_money(pick(r, "Order Total", "Adjusted Order Total", "Order Value")) or 0.0
        shipped = bool(pick(r, "Date Shipped").strip())
        name = pick(r, "Full Name", "Buyer", "Ship Name") or \
            f"{pick(r, 'First Name')} {pick(r, 'Last Name')}".strip()
        n_items = pick(r, "Number of Items")
        items = csv_items(con, rid) or (old[0] if old and old[0] else (f"{n_items} ks" if n_items else ""))
        con.execute("INSERT OR REPLACE INTO objednavky VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            shop, rid, parse_date(pick(r, "Sale Date", "Order Date")), name, items, total,
            pick(r, "Currency"), 1, int(shipped), pick(r, "Status") or ("Odesláno" if shipped else "Zaplaceno"),
            now, old[1] if old else 0))
        added += 1
    fill_items_from_ledger(con)
    return added


def import_order_items(con, shop, rows):
    """Download Data → Order Items (EtsySoldOrderItems*.csv): doplní názvy položek."""
    per_order = {}
    for r in rows:
        rid = pick(r, "Order ID", "Order Id")
        if not rid.strip().isdigit():
            continue
        per_order.setdefault(int(rid), []).append(f"{pick(r, 'Quantity') or 1}x {pick(r, 'Item Name')}")
    for rid, items in per_order.items():  # uloží se i pro objednávky nahrané později
        con.execute("INSERT OR REPLACE INTO csv_polozky VALUES (?,?)", (rid, "; ".join(items)))
        con.execute("UPDATE objednavky SET polozky=? WHERE receipt_id=?", ("; ".join(items), rid))
    return len(per_order)


def import_payments(con, shop, rows):
    """Download Data → Payments (EtsyDirectCheckoutPayments*.csv): platby po objednávkách."""
    n = 0
    for r in rows:
        rid = pick(r, "Order ID")
        if not rid.strip().isdigit():
            continue
        rid, name = int(rid), pick(r, "Buyer Name", "Buyer").strip()
        total = parse_money(pick(r, "Adjusted Gross", "Gross Amount")) or 0.0
        refund = parse_money(pick(r, "Refund Amount")) or 0.0
        cur = con.execute("INSERT OR IGNORE INTO objednavky VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            shop, rid, parse_date(pick(r, "Order Date")), name, csv_items(con, rid), round(total - refund, 2),
            pick(r, "Currency"), 1, None, "Vráceno" if refund else "Zaplaceno", 0, 0))
        if not cur.rowcount:  # objednávka už je: doplní jen chybějící jméno zákazníka
            con.execute("UPDATE objednavky SET zakaznik=? WHERE receipt_id=? AND zakaznik=''", (name, rid))
        n += 1
    return n


def import_listings(con, shop, rows):
    """Download Data → Currently for Sale Listings (EtsyListingsDownload.csv). Soubor je úplný
    seznam aktivních listingů, proto nahradí dříve nahrané listingy z CSV. Listingy z API zůstanou."""
    con.execute("DELETE FROM listingy WHERE shop=? AND listing_id<0", (shop,))
    api_titles = {r[0] for r in con.execute("SELECT nazev FROM listingy WHERE shop=?", (shop,))}
    now, n = int(time.time()), 0
    for r in rows:
        title = html.unescape(pick(r, "TITLE", "Title").strip())
        if not title or title in api_titles:
            continue
        qty = pick(r, "QUANTITY", "Quantity").strip()
        con.execute("INSERT OR REPLACE INTO listingy VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            shop, csv_id(shop, "listing", title), title, "active", parse_money(pick(r, "PRICE", "Price")),
            pick(r, "CURRENCY_CODE", "Currency"), int(qty) if qty.isdigit() else None, None, None,
            pick(r, "TAGS", "Tags").replace(",", ", "), pick(r, "IMAGE1"), "", pick(r, "SKU"), 0, now, 0,
            html.unescape(pick(r, "DESCRIPTION", "Description"))))
        n += 1
    return n


def import_csv(shop, filename, text, db_path=None):
    shop = (shop or "").strip()
    if not shop:
        raise AppError("import_shop", "Vyber nebo napiš, ke které shopě soubor patří.")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    header = set(rows[0].keys()) if rows else set()
    con = db(db_path)
    try:
        if {"Date", "Type", "Title", "Net"} <= header:
            kind, n = "statement", import_statement(con, shop, rows)
        elif {"Payment ID", "Order ID", "Gross Amount"} <= header:
            kind, n = "payments", import_payments(con, shop, rows)
        elif "Item Name" in header and ("Order ID" in header or "Order Id" in header):
            kind, n = "order_items", import_order_items(con, shop, rows)
        elif "Sale Date" in header and ("Order ID" in header or "Order Id" in header):
            kind, n = "orders", import_orders(con, shop, rows)
        elif {"TITLE", "PRICE", "QUANTITY"} <= header:
            kind, n = "listings", import_listings(con, shop, rows)
        else:
            raise AppError("import_unknown", f"Soubor {filename} nevypadá jako export z Etsy "
                           "(výpis, Orders, Order Items, Payments nebo Listings).", soubor=filename)
        con.commit()
    finally:
        con.close()
    return {"soubor": filename, "druh": kind, "novych": n, "radku": len(rows)}


# ------------------------------------------------------------------ web server

class Handler(BaseHTTPRequestHandler):
    demo = False
    db_path = None

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def state(self):
        cfg = load_config()
        tokens = load_tokens()
        if self.demo:
            shops = [{"id": "1", "name": "DemoPrintables", "zapis": True, "mazani": True}, {"id": "2", "name": "DemoHandmade", "zapis": True, "mazani": True}]
        else:
            shops = [{"id": k, "name": v.get("shop_name", k), "zapis": can_write(v), "mazani": can_delete(v)} for k, v in tokens.items()]
            con = db(self.db_path)
            names = {r[0] for r in con.execute("SELECT shop FROM objednavky UNION SELECT shop FROM vypis UNION SELECT shop FROM listingy")}
            con.close()
            shops += [{"id": None, "name": n} for n in sorted(names - {s["name"] for s in shops})]
        settings = {k: cfg.get(k) for k in ("interval_minut", "ntfy_topic", "redirect_uri", "keystring", "jazyk")}
        settings["ma_secret"] = bool(cfg.get("shared_secret"))
        return {
            "demo": self.demo,
            "nastaveno": self.demo or config_ready(cfg),
            "shopy": shops,
            "posledni_kontrola": int(time.time()) if self.demo else STATUS["posledni_kontrola"],
            "bezi": STATUS["bezi"],
            "chyby": STATUS["chyby"],
            "nastaveni": settings,
            "verze": VERSION,
        }

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(DASHBOARD_PATH, encoding="utf-8") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/stav":
            return self.send(200, self.state())
        if path == "/api/data":
            return self.send(200, dashboard_data(self.db_path))
        if path == "/api/kurzy":
            return self.send(200, get_rates())
        if path == "/api/listing/detail":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self.send(200, listing_detail(load_config(), q.get("shop", [""])[0], q.get("id", ["0"])[0], self.demo))
            except Exception as e:
                return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        if path == "/api/listing/vlastnosti":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self.send(200, listing_properties(load_config(), q.get("shop", [""])[0], q.get("kategorie", ["0"])[0], self.demo))
            except Exception as e:
                return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        if path == "/api/listing/moznosti":
            shop = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("shop", [""])[0]
            try:
                return self.send(200, listing_options(load_config(), shop, self.demo))
            except Exception as e:
                return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        if path in ("/export/objednavky.csv", "/export/vypis.csv", "/export/listingy.csv"):
            kind = path.split("/")[-1].split(".")[0]
            lang = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("lang", ["cs"])[0]
            fname = {"en": {"objednavky": "orders", "vypis": "statement", "listingy": "listings"},
                     "de": {"objednavky": "bestellungen", "vypis": "kontoauszug", "listingy": "angebote"}}.get(lang, {}).get(kind, kind)
            return self.send(200, csv_export(kind, self.db_path, lang), "text/csv; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="{fname}.csv"'})
        self.send(404, {"chyba": "nenalezeno"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"chyba": "zakázáno"})
        try:
            body = self.read_json()
            if self.demo and path not in ("/api/zkontrolovat", "/api/doprava"):
                return self.send(400, {"chyba": "V ukázkovém režimu nejde nic měnit.", "kod": "demo"})
            if path == "/api/nastaveni":
                cfg = load_config()
                for k in ("keystring", "redirect_uri", "ntfy_topic", "jazyk"):
                    if k in body:
                        cfg[k] = str(body[k]).strip()
                if body.get("shared_secret"):
                    cfg["shared_secret"] = str(body["shared_secret"]).strip()
                if body.get("interval_minut"):
                    cfg["interval_minut"] = max(5, int(body["interval_minut"]))
                save_config(cfg)
                return self.send(200, {"ok": True})
            if path == "/api/prihlasit/start":
                cfg = load_config()
                if not config_ready(cfg):
                    return self.send(400, {"chyba": "Nejdřív vyplň Keystring a Shared secret v Nastavení.",
                                           "kod": "need_keys"})
                return self.send(200, {"url": auth_start(cfg)})
            if path == "/api/prihlasit/dokoncit":
                name = auth_finish(load_config(), body.get("url", ""))
                threading.Thread(target=run_check, args=(load_config(),), daemon=True).start()
                return self.send(200, {"ok": True, "shop": name})
            if path == "/api/odebrat":
                with LOCK:
                    tokens = load_tokens()
                    tokens.pop(str(body.get("id")), None)
                    save_tokens(tokens)
                return self.send(200, {"ok": True})
            if path == "/api/aktualizace":
                return self.send(200, check_update())
            if path == "/api/listing/vytvorit":
                return self.send(200, save_listing(load_config(), body))
            if path == "/api/listing/stav":
                return self.send(200, listings_state(load_config(), body))
            if path == "/api/sleva":
                return self.send(200, add_discount(load_config(), body, self.db_path))
            if path == "/api/sleva/zrusit":
                return self.send(200, cancel_discount(load_config(), body, self.db_path))
            if path == "/api/doprava":
                return self.send(200, save_shipping(body, self.db_path))
            if path == "/api/import":
                return self.send(200, import_csv(body.get("shop"), body.get("soubor", ""),
                                                 body.get("obsah", ""), self.db_path))
            if path == "/api/odinstalovat":
                uninstall(bool(body.get("smazat_data")))
                return self.send(200, {"ok": True})
            if path == "/api/zkontrolovat":
                news = [] if self.demo else run_check(load_config())
                return self.send(200, {"ok": True, "novinky": news, "chyby": STATUS["chyby"]})
        except Exception as e:
            return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        self.send(404, {"chyba": "nenalezeno"})


LAUNCH_LABEL = "io.github.fanattik.etsy-dashboard"


def uninstall(delete_data):
    """Vypne běh na pozadí (Mac), smaže zástupce a volitelně data, pak aplikaci ukončí."""
    home = os.path.expanduser("~")
    plist = os.path.join(home, "Library", "LaunchAgents", LAUNCH_LABEL + ".plist")
    for f in (plist, os.path.join(home, "Desktop", "Etsy Dashboard.webloc")):
        if os.path.exists(f):
            os.remove(f)
    if delete_data:
        installed = os.path.basename(BASE_DIR) == "EtsyDashboard" and "Application Support" in BASE_DIR
        if installed:
            shutil.rmtree(BASE_DIR, ignore_errors=True)
        else:
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            if os.path.exists(CONFIG_PATH):
                os.remove(CONFIG_PATH)

    def stop():
        if sys.platform == "darwin":
            # samostatný proces, aby ho launchd neukončil spolu s aplikací
            subprocess.Popen(["/bin/sh", "-c", f"sleep 1; launchctl bootout gui/{os.getuid()}/{LAUNCH_LABEL} "
                                               f"|| launchctl remove {LAUNCH_LABEL}"],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        os._exit(0)

    threading.Thread(target=stop, daemon=True).start()


def serve(demo=False, open_browser=True):
    global PORT
    Handler.demo = demo
    if demo:
        PORT = 8766
        Handler.db_path = os.path.join(DATA_DIR, "demo.db")
        make_demo_db(Handler.db_path)
    url = f"http://127.0.0.1:{PORT}/"
    with socket.socket() as probe:
        busy = probe.connect_ex(("127.0.0.1", PORT)) == 0
    if busy:  # už běží (např. na pozadí), stačí otevřít prohlížeč
        print(f"Etsy Dashboard už běží na {url}")
        if open_browser:
            webbrowser.open(url)
        return
    if not demo:
        con = db()  # doplní názvy produktů i do dříve nahraných objednávek
        fill_items_from_ledger(con)
        con.commit()
        con.close()
        threading.Thread(target=watcher_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Etsy Dashboard běží na {url}  (ukončíš Ctrl+C nebo zavřením okna)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "web"
    if cmd == "web":
        serve(open_browser="--sluzba" not in sys.argv)
    elif cmd == "demo":
        serve(demo=True, open_browser="--bez-prohlizece" not in sys.argv)
    elif cmd == "jednou":
        cfg = load_config()
        if not config_ready(cfg):
            sys.exit("Nejdřív spusť aplikaci a vyplň Nastavení.")
        news = run_check(cfg)
        print("\n".join(news) if news else "Nic nového.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
