"""Kanál Vlastní API (fáze 5): e-shop, který implementuje jednoduché API z docs/custom-api.md.
Dashboard ho volá sám (účet, vystavení a úprava produktu, sklad, objednávky, tracking), e-shop
nic nevolá zpátky, takže to funguje i s dashboardem na notebooku."""

import base64
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from zaklad import AppError, LOCK, MEDIA_DIR, OVERLAP, SSL_CTX, tr
import zaklad
from databaze import db, last_ts, set_last_ts

KANAL = "api"
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        ".pdf": "application/pdf", ".zip": "application/zip"}


def is_api(ucet_id):
    return str(ucet_id or "").startswith(KANAL + ":")


def _jl(v, default):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


# ------------------------------------------------------------------ účty

def _check_url(url):
    url = str(url or "").strip().rstrip("/")
    u = urllib.parse.urlparse(url)
    local = u.hostname in ("localhost", "127.0.0.1")
    if u.scheme not in ("https", "http") or not u.hostname or (u.scheme == "http" and not local):
        raise AppError("api_url", "Adresa API musí začínat https:// (http:// jen pro localhost).")
    return url


def account(con, ucet_id):
    row = con.execute("SELECT id, nazev, mena, jazyk, pristup, pravidla FROM kanal_ucty WHERE id=? AND kanal=?",
                      (ucet_id, KANAL)).fetchone()
    if not row:
        raise AppError("api_account", "Tenhle e-shop v aplikaci není.")
    p = _jl(row[4], {})
    return {"id": row[0], "nazev": row[1], "mena": row[2] or "", "jazyk": row[3] or "", "url": p.get("url", ""),
            "klic": p.get("klic", ""), "pravidla": _jl(row[5], {})}


def accounts(con):
    return [account(con, r[0]) for r in con.execute("SELECT id FROM kanal_ucty WHERE kanal=? AND aktivni=1 ORDER BY nazev", (KANAL,))]


def call(acct, method, path, body=None, params=None, ok404=False):
    """Jeden požadavek na e-shop. Chyba e-shopu ({"error": …}) se ukáže uživateli s názvem e-shopu."""
    url = acct["url"] + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + acct["klic"])
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", f"etsy-dashboard/{zaklad.VERSION} (+https://github.com/fanattik/etsy-dashboard)")
    if data is not None:
        req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=120, context=SSL_CTX) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        text = e.read().decode("utf-8", "replace")
        msg = (_jl(text, {}) or {}).get("error") if text.strip().startswith("{") else None
        raise AppError("api_failed", f"{acct['nazev']}: {msg or text[:200] or e.reason} ({e.code})",
                       shop=acct["nazev"], e=f"{msg or text[:200] or e.reason} ({e.code})", status=e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AppError("api_failed", f"{acct['nazev']}: {e}", shop=acct["nazev"], e=str(e), status=0) from None


def list_accounts(db_path=None):
    """Pro Nastavení: e-shopy bez klíče (jen jestli je uložený)."""
    con = db(db_path)
    try:
        out = []
        for a in accounts(con):
            last = last_ts(con, a["id"], "objednavky")
            out.append({"id": a["id"], "nazev": a["nazev"], "url": a["url"], "mena": a["mena"], "jazyk": a["jazyk"],
                        "ma_klic": bool(a["klic"]), "posledni": last})
        return {"ucty": out}
    finally:
        con.close()


def _slug(name):
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:30]
    return s or "eshop"


def test_account(body, db_path=None):
    """Zkusí /info s adresou a klíčem z formuláře (prázdný klíč = uložený)."""
    url = _check_url(body.get("url"))
    klic = str(body.get("klic") or "").strip()
    if not klic and body.get("id"):
        con = db(db_path)
        try:
            klic = account(con, body["id"])["klic"]
        finally:
            con.close()
    if not klic:
        raise AppError("api_key", "Vyplň API klíč.")
    info = call({"nazev": body.get("nazev") or url, "url": url, "klic": klic}, "GET", "/info")
    return {"ok": True, "info": {k: info.get(k) for k in ("name", "currency", "language", "api_version")}}


def save_account(body, db_path=None, demo=False):
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    nazev = str(body.get("nazev") or "").strip()[:60]
    if not nazev:
        raise AppError("api_name", "E-shop potřebuje název.")
    url = _check_url(body.get("url"))
    mena = str(body.get("mena") or "").strip().upper()[:3]
    if not re.fullmatch(r"[A-Z]{3}", mena):
        raise AppError("api_currency", "Vyber měnu, ve které e-shop prodává.")
    jazyk = str(body.get("jazyk") or "").strip().lower()[:5]
    klic = str(body.get("klic") or "").strip()
    with LOCK:
        con = db(db_path)
        try:
            uid = body.get("id")
            if uid:
                old = account(con, uid)
                klic = klic or old["klic"]
            else:
                uid, n = f"{KANAL}:{_slug(nazev)}", 1
                while con.execute("SELECT 1 FROM kanal_ucty WHERE id=?", (uid,)).fetchone():
                    n += 1
                    uid = f"{KANAL}:{_slug(nazev)}-{n}"
            if not klic:
                raise AppError("api_key", "Vyplň API klíč.")
            pristup = json.dumps({"url": url, "klic": klic})
            con.execute("INSERT OR IGNORE INTO kanal_ucty (id, kanal, nazev, mena, jazyk, pristup) VALUES (?,?,?,?,?,?)",
                        (uid, KANAL, nazev, mena, jazyk, pristup))
            con.execute("UPDATE kanal_ucty SET nazev=?, mena=?, jazyk=?, pristup=?, aktivni=1 WHERE id=?",
                        (nazev, mena, jazyk, pristup, uid))
            con.commit()
        finally:
            con.close()
    return {"ok": True, "id": uid}


def delete_account(body, db_path=None, demo=False):
    """Odebere e-shop z aplikace: jeho nabídky se odpojí, v e-shopu se nic nemaže. Objednávky zůstanou."""
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    with LOCK:
        con = db(db_path)
        try:
            uid = account(con, body.get("id"))["id"]
            for sql in ("DELETE FROM nabidky WHERE ucet_id=?", "DELETE FROM kanal_produkty WHERE ucet_id=?",
                        "DELETE FROM kanal_ucty WHERE id=?", "DELETE FROM stav WHERE shop_id=?"):
                con.execute(sql, (uid,))
            con.commit()
        finally:
            con.close()
    return {"ok": True}


# ------------------------------------------------------------------ produkty

def _media(product, offer, digital):
    """Fotky a soubory produktu s obsahem z data/media (stejný výběr a pořadí jako u nabídky)."""
    wanted = {"foto": [m["url"] for m in offer.get("obrazky") or []], "soubor": [m["url"] for m in offer.get("soubory") or []]}
    out = {"foto": [], "soubor": []}
    for druh in out:
        by_url = {m["url"]: m for m in product["media"] if m["druh"] == druh and m.get("cesta")}
        for url in wanted[druh]:
            m = by_url.get(url)
            path = os.path.join(MEDIA_DIR, m["cesta"]) if m else None
            if not path or not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                data = f.read()
            out[druh].append({"filename": m["nazev"], "content_type": MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream"),
                              "sha256": hashlib.sha256(data).hexdigest(), "_data": data})
    return out["foto"], out["soubor"] if digital else []


def payload(product, offer, create, status="active"):
    """Tělo PUT /products/{sku} z vyřešené nabídky (vrstvy → produkt → pravidla účtu)."""
    digital = product["typ"] == "digital"
    variants = []
    for v in offer.get("produkty") or []:
        variants.append({"sku": v.get("sku") or "", "options": {h["nazev"]: h["hodnota"] for h in v.get("hodnoty") or []},
                         "price": v.get("cena"), "quantity": None if digital else v.get("mnozstvi"), "active": v.get("aktivni", True)})
    dims = {"length": product.get("delka_mm"), "width": product.get("sirka_mm"), "height": product.get("vyska_mm")}
    images, files = _media(product, offer, digital)
    body = {"sku": product["sku"], "title": offer["nazev"], "description": offer["popis"], "price": offer["cena"],
            "currency": offer["mena"], "quantity": None if digital else offer.get("mnozstvi"), "type": "digital" if digital else "physical",
            "tags": offer.get("stitky") or [], "category": product.get("kategorie") or None, "weight_g": product.get("hmotnost_g"),
            "dimensions_mm": dims if any(dims.values()) else None, "variants": variants, "images": images, "files": files}
    if variants and not digital and product.get("vyroba") == "sklad":
        body["quantity"] = sum(v["quantity"] or 0 for v in variants)
    if create:
        body["status"] = status if status in ("active", "draft") else "active"
    return body


def _with_data(items, have):
    return [{**{k: v for k, v in it.items() if k != "_data"},
             **({} if it["sha256"] in have else {"data": base64.b64encode(it["_data"]).decode()})} for it in items]


def _store_live(con, ucet, p, now=None):
    con.execute("INSERT OR REPLACE INTO kanal_produkty VALUES (?,?,?,?,?,?,?,?,?)", (
        ucet, str(p.get("sku")), p.get("title") or "", p.get("status") or "", p.get("price"), p.get("currency") or "",
        p.get("quantity"), p.get("url") or "", int(p.get("updated_at") or now or time.time())))


def publish(cfg, body, db_path=None, demo=False):
    """Vystaví produkt z katalogu do e-shopu, nebo do něj pošle změny (celý produkt znovu, fotky jen nové).
    Volá se jen po kliknutí uživatele."""
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    from katalog import catalog_data, store_snapshot  # katalog importuje nabídky, ty tenhle modul
    from nabidky import resolve_offer
    from produkty import cached_rates
    ucet, pid = str(body.get("ucet_id") or ""), int(body.get("produkt_id") or 0)
    data = catalog_data(db_path, cfg.get("zakladni_mena", ""), cfg.get("jazyk_katalogu") or "en")
    product = next((p for p in data["produkty"] if p["id"] == pid), None)
    acct_c = next((a for a in data["ucty"] if a["id"] == ucet), None)
    if not product or not acct_c:
        raise AppError("adopt_product", "Zvolený produkt v katalogu není.")
    if not product.get("sku"):
        raise AppError("api_sku", "Produkt nemá SKU. Doplň ho v detailu produktu, e-shop podle něj produkt pozná.")
    con = db(db_path)
    try:
        acct = account(con, ucet)
        linked = con.execute("SELECT externi_id FROM nabidky WHERE produkt_id=? AND ucet_id=?", (pid, ucet)).fetchone()
        clash = con.execute("SELECT produkt_id FROM nabidky WHERE ucet_id=? AND externi_id=? AND produkt_id<>?",
                            (ucet, product["sku"], pid)).fetchone()
    finally:
        con.close()
    if clash:
        raise AppError("api_sku_taken", "V tomhle e-shopu už je pod stejným SKU jiný produkt z katalogu.")
    sku = linked[0] if linked else product["sku"]
    offer = resolve_offer(product, acct_c, data["zakladni_mena"], cached_rates())
    if offer.get("cena") is None:
        raise AppError("api_price", "Produkt nemá cenu pro tenhle e-shop. Doplň základní cenu nebo cenu ve vrstvě e-shopu.")
    path = "/products/" + urllib.parse.quote(sku, safe="")
    current = call(acct, "GET", path, ok404=True)
    req = payload(product, offer, create=current is None, status=body.get("stav"))
    req["sku"] = sku
    have = {i.get("sha256") for i in ((current or {}).get("images") or []) + ((current or {}).get("files") or [])}
    raw_images, raw_files = req["images"], req["files"]
    req["images"], req["files"] = _with_data(raw_images, have), _with_data(raw_files, have)
    try:
        result = call(acct, "PUT", path, req)
    except AppError as e:
        if e.param.get("status") != 409:
            raise
        req["images"], req["files"] = _with_data(raw_images, set()), _with_data(raw_files, set())  # e-shop fotku nezná: poslat vše
        result = call(acct, "PUT", path, req)
    result = result or {}
    now = int(time.time())
    with LOCK:
        con = db(db_path)
        try:
            con.execute("INSERT OR IGNORE INTO nabidky (produkt_id, ucet_id, externi_id, stav, vytvoreno_ts) VALUES (?,?,?,?,?)",
                        (pid, ucet, sku, result.get("status") or req.get("status") or "", now))
            _store_live(con, ucet, {**req, **result, "sku": sku}, now)
            con.execute("UPDATE nabidky SET stav=? WHERE ucet_id=? AND externi_id=?",
                        (result.get("status") or (current or {}).get("status") or req.get("status") or "", ucet, sku))
            store_snapshot(con, ucet, sku, offer, pushed=True, modified=result.get("updated_at"))
            con.commit()
        finally:
            con.close()
    return {"ok": True, "novy": current is None, "url": result.get("url") or "", "sku": sku}


def readopt(pid, ucet, sku, db_path=None):
    """Převezme produkt tak, jak je teď v e-shopu, do vrstvy e-shopu v katalogu."""
    from katalog import _layer, store_snapshot
    con = db(db_path)
    try:
        acct = account(con, ucet)
    finally:
        con.close()
    p = call(acct, "GET", "/products/" + urllib.parse.quote(sku, safe=""), ok404=True)
    if p is None:
        raise AppError("api_gone", "Produkt už v e-shopu není.")
    now = int(time.time())
    with LOCK:
        con = db(db_path)
        try:
            prod = con.execute("SELECT nazev, popis FROM produkty WHERE id=?", (pid,)).fetchone()
            lay = _layer(con, pid, ucet, acct["jazyk"] or "en")
            con.execute("UPDATE kanal_data SET nazev=?, popis=?, stitky=?, cena=?, mena=?, zmeneno_ts=? WHERE id=?", (
                p.get("title") if p.get("title") != prod[0] else None, p.get("description") if p.get("description") != prod[1] else None,
                json.dumps(p.get("tags") or [], ensure_ascii=False), p.get("price"), p.get("currency") or acct["mena"], now, lay))
            _store_live(con, ucet, {**p, "sku": sku}, now)
            con.execute("UPDATE nabidky SET stav=? WHERE ucet_id=? AND externi_id=?", (p.get("status") or "", ucet, sku))
            store_snapshot(con, ucet, sku, {"nazev": p.get("title"), "popis": p.get("description"), "stitky": p.get("tags") or [],
                                            "cena": p.get("price"), "kategorie": None}, modified=p.get("updated_at"))
            con.commit()
        finally:
            con.close()


# ------------------------------------------------------------------ synchronizace

def _pages(acct, path, params=None):
    page, seen = 1, 0
    while page and seen < 500:
        r = call(acct, "GET", path, params={**(params or {}), "page": page}) or {}
        yield r
        nxt = r.get("next_page")
        page = int(nxt) if nxt not in (None, "", 0) and int(nxt) != page else None
        seen += 1


def _local_id(con, ucet, ext, cislo):
    con.execute("INSERT OR IGNORE INTO obj_kanal (ucet_id, externi_id, cislo) VALUES (?,?,?)", (ucet, ext, cislo))
    con.execute("UPDATE obj_kanal SET cislo=? WHERE ucet_id=? AND externi_id=?", (cislo, ucet, ext))
    # číslo objednávky v dashboardu: vysoko nad čísly objednávek z Etsy, aby se nepotkaly
    return 10 ** 15 + con.execute("SELECT id FROM obj_kanal WHERE ucet_id=? AND externi_id=?", (ucet, ext)).fetchone()[0]


def _money(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def sync_account(cfg, con, acct):
    """Stáhne stav produktů a nové nebo změněné objednávky. Vrací texty novinek."""
    ucet, name, now = acct["id"], acct["nazev"], int(time.time())
    seen = set()
    for r in _pages(acct, "/products"):
        for p in r.get("products") or []:
            if p.get("sku"):
                _store_live(con, ucet, p, now)
                seen.add(str(p["sku"]))
    for (ext,) in con.execute("SELECT externi_id FROM kanal_produkty WHERE ucet_id=?", (ucet,)).fetchall():
        if ext not in seen:
            con.execute("DELETE FROM kanal_produkty WHERE ucet_id=? AND externi_id=?", (ucet, ext))
    for ext, in con.execute("SELECT externi_id FROM nabidky WHERE ucet_id=?", (ucet,)).fetchall():
        st = con.execute("SELECT stav FROM kanal_produkty WHERE ucet_id=? AND externi_id=?", (ucet, ext)).fetchone()
        con.execute("UPDATE nabidky SET stav=? WHERE ucet_id=? AND externi_id=?", (st[0] if st else "removed", ucet, ext))

    news = []
    since = last_ts(con, ucet, "objednavky")
    first = since is None
    start = now - int(cfg.get("prvni_stazeni_dni") or 365) * 86400 if first else since - OVERLAP
    for r in _pages(acct, "/orders", {"since": start}):
        for o in r.get("orders") or []:
            if o.get("id") in (None, ""):
                continue
            ext = str(o["id"])
            rid = _local_id(con, ucet, ext, str(o.get("number") or ext))
            cur = o.get("currency") or acct["mena"]
            lines = o.get("items") or []
            items = "; ".join(f"{i.get('quantity') or 1}x {i.get('title') or i.get('sku') or ''}" for i in lines)
            cust = o.get("customer") or {}
            old = con.execute("SELECT pridano_ts FROM objednavky WHERE receipt_id=?", (rid,)).fetchone()
            added = old[0] if old else (0 if first else now)
            con.execute("INSERT OR REPLACE INTO objednavky VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                name, rid, int(o.get("created_at") or now), cust.get("name") or "", items, _money(o.get("total")), cur,
                int(bool(o.get("paid"))), int(bool(o.get("shipped"))), str(o.get("status") or ""),
                int(o.get("updated_at") or o.get("created_at") or now), added))
            con.execute("INSERT OR REPLACE INTO obj_info VALUES (?,?,?,?)", (
                rid, str(cust.get("email") or cust.get("name") or ""), (cust.get("city") or "").strip(), (cust.get("country") or "").upper()))
            for i in lines:
                if i.get("id") in (None, ""):
                    continue
                con.execute("INSERT OR REPLACE INTO obj_polozky VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    ucet, rid, f"{ext}/{i['id']}", str(i.get("product_sku") or i.get("sku") or ""), str(i.get("sku") or ""),
                    i.get("title") or "", int(i.get("quantity") or 1), _money(i.get("price")), cur,
                    i.get("variant") or "", i.get("personalization") or ""))
            ship = o.get("shipment") or {}
            if ship.get("carrier") or ship.get("tracking_number"):  # jen když je uživatel nevyplnil sám
                con.execute("INSERT OR IGNORE INTO doprava VALUES (?,?,?,?,?,?)", (rid, "", "", None, "", 0))
                con.execute("UPDATE doprava SET dopravce=? WHERE receipt_id=? AND dopravce=''", (ship.get("carrier") or "", rid))
                con.execute("UPDATE doprava SET cislo=? WHERE receipt_id=? AND cislo=''", (ship.get("tracking_number") or "", rid))
            if not old and not first:
                news.append(tr(cfg, "order", shop=name, total=f"{_money(o.get('total')):.2f} {cur}", buyer=cust.get("name") or "", items=items))
    set_last_ts(con, ucet, "objednavky", now)
    return news


def sync_all(cfg, db_path=None):
    """Všechny e-shopy přes Vlastní API. Chyba jednoho nezastaví ostatní. Vrací (novinky, chyby podle názvu)."""
    con = db(db_path)
    news, errors = [], {}
    try:
        for acct in accounts(con):
            try:
                news += sync_account(cfg, con, acct)
                con.commit()
                errors[acct["nazev"]] = None
            except Exception as e:
                con.rollback()
                errors[acct["nazev"]] = str(e)
    finally:
        con.close()
    return news, errors


def order_ref(con, rid):
    """(účet, id objednávky v e-shopu) pro objednávku z Vlastního API, jinak None."""
    rid = int(rid)
    if rid < 10 ** 15:
        return None
    row = con.execute("SELECT ucet_id, externi_id FROM obj_kanal WHERE id=?", (rid - 10 ** 15,)).fetchone()
    return tuple(row) if row else None


def ship(con, ucet, ext, carrier, number):
    acct = account(con, ucet)
    return call(acct, "POST", f"/orders/{urllib.parse.quote(ext, safe='')}/shipment",
                {"carrier": carrier or "", "tracking_number": number})


def order_numbers(con):
    return {10 ** 15 + r[0]: r[1] for r in con.execute("SELECT id, cislo FROM obj_kanal")}
