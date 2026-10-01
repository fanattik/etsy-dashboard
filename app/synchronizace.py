"""Stahování dat z Etsy do lokální databáze (objednávky, výpis, listingy, statistiky) a kurzy ECB."""

import html
import json
import os
import re as _re
import time
import urllib.error
import urllib.parse
import urllib.request

from zaklad import DATA_DIR, LEDGER_CHUNK, OVERLAP, RATES_EVERY, RATES_PATH, RATES_URL, SSL_CTX, tr
from databaze import last_ts, set_last_ts, today
from etsy_api import api_get, api_get_all, money


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
        save_receipt_info(con, r)
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
    if not first and last_ts(con, shop_id, "obj_info") is None:  # objednávky stažené před verzí 1.21: doplnit město a kupujícího
        try:
            for r in api_get_all(cfg, tokens, shop_id, f"/shops/{shop_id}/receipts", {"min_created": first_start}):
                save_receipt_info(con, r)
            set_last_ts(con, shop_id, "obj_info", now)
        except Exception as e:
            print(f"⚠️  {name}: města objednávek: {e}")
    elif first:
        set_last_ts(con, shop_id, "obj_info", now)

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
    try:
        sync_shop_stats(cfg, tokens, con, shop_id, name, now)
    except Exception as e:
        print(f"⚠️  {name}: statistiky: {e}")
    con.commit()
    return news


def save_receipt_info(con, r):
    con.execute("INSERT OR REPLACE INTO obj_info VALUES (?,?,?,?)", (
        r["receipt_id"], str(r.get("buyer_user_id") or r.get("buyer_email") or r.get("name") or ""),
        (r.get("city") or "").strip(), r.get("country_iso") or ""))


def sync_shop_stats(cfg, tokens, con, shop_id, name, now):
    """Sledující shopy (denní stav) a recenze."""
    shop = api_get(cfg, tokens, shop_id, f"/shops/{shop_id}")
    if shop.get("num_favorers") is not None:
        con.execute("INSERT OR REPLACE INTO stat_shop VALUES (?,?,?)", (today(), name, shop["num_favorers"]))
    since = last_ts(con, shop_id, "recenze")
    params = {"min_created": since - OVERLAP} if since else {}
    for r in api_get_all(cfg, tokens, shop_id, f"/shops/{shop_id}/reviews", params):
        ts = r.get("created_timestamp") or r.get("create_timestamp") or 0
        con.execute("INSERT OR REPLACE INTO recenze VALUES (?,?,?,?,?,?)", (
            str(r.get("transaction_id") or f"{r.get('listing_id')}-{ts}"), name, r.get("listing_id"),
            r.get("rating"), r.get("review") or "", ts))
    set_last_ts(con, shop_id, "recenze", now)


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
        if l.get("views") is not None or l.get("num_favorers") is not None:
            con.execute("INSERT OR REPLACE INTO stat_listingy VALUES (?,?,?,?,?)",
                        (today(), l["listing_id"], name, l.get("views"), l.get("num_favorers")))
    con.execute("DELETE FROM listingy WHERE shop=? AND listing_id<0", (name,))  # API nahradí data z CSV
    if complete:  # smazané listingy
        for (lid,) in con.execute("SELECT listing_id FROM listingy WHERE shop=?", (name,)).fetchall():
            if lid not in seen:
                con.execute("DELETE FROM listingy WHERE listing_id=?", (lid,))


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
