"""Základ: cesty, konstanty, konfigurace, tokeny, texty pro notifikace a HTTP."""

import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


API = os.environ.get("ETSY_DASHBOARD_API") or "https://openapi.etsy.com/v3/application"
AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
SCOPES = "transactions_r transactions_w shops_r profile_r listings_r listings_w listings_d"
UPDATE_BASE = os.environ.get("ETSY_DASHBOARD_UPDATE_URL") or "https://raw.githubusercontent.com/fanattik/etsy-dashboard/main/app/"
UPDATE_EVERY = 24 * 3600
RATES_URL = os.environ.get("ETSY_DASHBOARD_RATES_URL") or "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
RATES_EVERY = 12 * 3600
VERSION = ""  # nastaví etsy_dashboard.py (jediné místo s číslem verze, čte ho i build a launcher)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKENS_PATH = os.path.join(DATA_DIR, "tokens.json")
DB_PATH = os.path.join(DATA_DIR, "etsy.db")
RATES_PATH = os.path.join(DATA_DIR, "kurzy.json")
TAXONOMY_PATH = os.path.join(DATA_DIR, "kategorie.json")
MEDIA_DIR = os.path.join(DATA_DIR, "media")  # fotky a soubory produktů z katalogu
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
    "zakladni_mena": "",  # měna základních cen v katalogu
    "jazyk_katalogu": "en",  # výchozí jazyk textů produktů
}

LOCK = threading.Lock()  # jedna kontrola / zápis tokenů naráz
DISCOUNT_LOCK = threading.Lock()  # slevy se spouští / ukončují jen jednou naráz
STATUS = {"posledni_kontrola": None, "chyby": {}, "bezi": False}

# Texty, které posílá server (upozornění na telefon). Dashboard má vlastní překlady.
TEXTS = {
    "cs": {"order": "🛒 {shop}: nová objednávka {total} od {buyer} ({items})",
           "status": "🔄 {shop}: objednávka {id} je teď {status}",
           "state": "📦 {shop}: objednávka {id} → {state}",
           "more": "… a dalších {n}", "title": "Etsy Dashboard: novinky"},
    "en": {"order": "🛒 {shop}: new order {total} from {buyer} ({items})",
           "status": "🔄 {shop}: order {id} is now {status}",
           "state": "📦 {shop}: order {id} → {state}",
           "more": "… and {n} more", "title": "Etsy Dashboard: news"},
    "de": {"order": "🛒 {shop}: neue Bestellung {total} von {buyer} ({items})",
           "status": "🔄 {shop}: Bestellung {id} ist jetzt {status}",
           "state": "📦 {shop}: Bestellung {id} → {state}",
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
