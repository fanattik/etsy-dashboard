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

API = "https://openapi.etsy.com/v3/application"
AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
SCOPES = "transactions_r shops_r profile_r"
PORT = 8765
VERSION = "1.8"
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
STATUS = {"posledni_kontrola": None, "chyby": {}, "bezi": False}
PENDING_AUTH = {}  # state -> code_verifier

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


def save_tokens(tokens):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp, TOKENS_PATH)


# ---------------------------------------------------------------------- HTTP

def http_json(method, url, headers=None, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode("utf-8"))
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
    PENDING_AUTH[state] = verifier
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
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query)
    if "error" in query:
        detail = query.get("error_description", query["error"])[0]
        raise AppError("auth_denied", "Etsy přístup nepovolilo: " + detail, detail=detail)
    state = query.get("state", [""])[0]
    verifier = PENDING_AUTH.pop(state, None)
    if not verifier:
        raise AppError("auth_state", "Adresa nepatří k tomuto přihlášení. Klikni znovu na „Přihlásit shopu“.")
    if "code" not in query:
        raise AppError("auth_code", "V adrese chybí 'code'.")
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
        tok.update({"shop_name": shop.get("shop_name", shop_id),
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
    con.commit()
    return news


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
    }
    con.close()
    return data


CSV_HEADERS = {
    "cs": {"shop": "Shopa", "receipt_id": "Číslo objednávky", "vytvoreno_ts": "Datum", "zakaznik": "Zákazník",
           "polozky": "Položky", "celkem": "Celkem", "mena": "Měna", "zaplaceno": "Zaplaceno",
           "odeslano": "Odesláno", "stav": "Stav", "entry_id": "ID pohybu", "datum_ts": "Datum",
           "typ": "Typ", "popis": "Popis", "castka": "Částka", "zustatek": "Zůstatek",
           "reference": "Reference"},
    "en": {"shop": "Shop", "receipt_id": "Order ID", "vytvoreno_ts": "Date", "zakaznik": "Buyer",
           "polozky": "Items", "celkem": "Total", "mena": "Currency", "zaplaceno": "Paid",
           "odeslano": "Shipped", "stav": "Status", "entry_id": "Entry ID", "datum_ts": "Date",
           "typ": "Type", "popis": "Description", "castka": "Amount", "zustatek": "Balance",
           "reference": "Reference"},
    "de": {"shop": "Shop", "receipt_id": "Bestellnr.", "vytvoreno_ts": "Datum", "zakaznik": "Kunde",
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
                "zaplaceno", "odeslano", "stav"]
        rows = con.execute(f"SELECT {','.join(cols)} FROM objednavky ORDER BY shop, vytvoreno_ts").fetchall()
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
    rid, eid = 3000000000, 900000
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
        else:
            raise AppError("import_unknown", f"Soubor {filename} nevypadá jako export z Etsy "
                           "(výpis, Orders, Order Items nebo Payments).", soubor=filename)
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
            shops = [{"id": "1", "name": "DemoPrintables"}, {"id": "2", "name": "DemoHandmade"}]
        else:
            shops = [{"id": k, "name": v.get("shop_name", k)} for k, v in tokens.items()]
            con = db(self.db_path)
            names = {r[0] for r in con.execute("SELECT shop FROM objednavky UNION SELECT shop FROM vypis")}
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
        if path in ("/export/objednavky.csv", "/export/vypis.csv"):
            kind = path.split("/")[-1].split(".")[0]
            lang = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("lang", ["cs"])[0]
            fname = {"en": {"objednavky": "orders", "vypis": "statement"},
                     "de": {"objednavky": "bestellungen", "vypis": "kontoauszug"}}.get(lang, {}).get(kind, kind)
            return self.send(200, csv_export(kind, self.db_path, lang), "text/csv; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="{fname}.csv"'})
        self.send(404, {"chyba": "nenalezeno"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"chyba": "zakázáno"})
        try:
            body = self.read_json()
            if self.demo and path != "/api/zkontrolovat":
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
