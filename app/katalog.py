"""Katalog produktů: produkt je v katalogu jednou, nad ním vrstvy obsahu pro kanály (kanal_data)
a nabídky v jednotlivých prodejních účtech (nabidky). Fáze 1: převzetí listingů z Etsy a import složky,
do kanálů se nic nezapisuje."""

import base64
import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request

from zaklad import AppError, LOCK, MEDIA_DIR, SSL_CTX, load_tokens
from databaze import db, rows_as_dicts, save_account
from etsy_api import api_get
from etsy_listingy import listing_detail

MAX_MEDIA = 50 * 1024 * 1024
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
        ".pdf": "application/pdf", ".zip": "application/zip"}


def etsy_account(shop_id):
    return f"etsy:{shop_id}"


def norm_title(s):
    """Název pro párování: bez diakritiky, interpunkce a rozdílů v mezerách."""
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def propose_sku(con, title):
    """Návrh SKU z prvních slov názvu (CALM-BUDGET-PLANNER), při shodě s číslem na konci."""
    words = [w for w in norm_title(title).upper().split() if len(w) > 2 and w not in ("THE", "AND", "FOR", "WITH")]
    base = "-".join(words[:3])[:24].strip("-") or "PRODUKT"
    sku, n = base, 1
    while con.execute("SELECT 1 FROM produkty WHERE sku=?", (sku,)).fetchone():
        n += 1
        sku = f"{base}-{n}"
    return sku


def _jl(v, default):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def ensure_accounts(con):
    """Účty z přihlášených Etsy shop (měnu doplní synchronizace)."""
    for shop_id, tok in load_tokens().items():
        save_account(con, etsy_account(shop_id), "etsy", tok.get("shop_name", shop_id), None, "en")


def catalog_data(db_path=None):
    """Katalog pro stránku Produkty: účty, produkty s médii, variantami, vrstvami a nabídkami."""
    con = db(db_path)
    if db_path is None:
        ensure_accounts(con)
        con.commit()
    products = rows_as_dicts(con, "SELECT * FROM produkty ORDER BY nazev COLLATE NOCASE")
    by_id = {p["id"]: p for p in products}
    for p in products:
        p.update(media=[], varianty=[], vrstvy=[], nabidky=[])
    for m in rows_as_dicts(con, "SELECT * FROM produkt_media ORDER BY produkt_id, druh, poradi, id"):
        if m["produkt_id"] in by_id:
            m["url"] = f"/media/{m['cesta']}" if m["cesta"] else ""
            by_id[m["produkt_id"]]["media"].append(m)
    for v in rows_as_dicts(con, "SELECT * FROM produkt_varianty ORDER BY produkt_id, poradi, id"):
        if v["produkt_id"] in by_id:
            v["vlastnosti"] = _jl(v["vlastnosti"], {})
            by_id[v["produkt_id"]]["varianty"].append(v)
    for k in rows_as_dicts(con, "SELECT * FROM kanal_data ORDER BY produkt_id, rozsah"):
        if k["produkt_id"] in by_id:
            k["stitky"], k["atributy"], k["extra"] = _jl(k["stitky"], []), _jl(k["atributy"], []), _jl(k["extra"], {})
            by_id[k["produkt_id"]]["vrstvy"].append(k)
    for n in rows_as_dicts(con, "SELECT id, produkt_id, ucet_id, externi_id, stav, zmeneno_v_kanalu_ts, posledni_chyba, "
                                "vytvoreno_ts FROM nabidky ORDER BY produkt_id, ucet_id"):
        if n["produkt_id"] in by_id:
            by_id[n["produkt_id"]]["nabidky"].append(n)
    sold = {}  # prodané kusy podle produktu: řádek objednávky → nabídka (listing) nebo SKU varianty
    sku_map = {r[0]: r[1] for r in con.execute("SELECT sku, produkt_id FROM produkt_varianty WHERE sku<>''")}
    sku_map.update({r[0]: r[1] for r in con.execute("SELECT sku, id FROM produkty WHERE sku<>''")})
    offer_map = {(r[0], r[1]): r[2] for r in con.execute("SELECT ucet_id, externi_id, produkt_id FROM nabidky")}
    for ucet, lid, sku, q in con.execute("SELECT ucet_id, externi_nabidka_id, sku, mnozstvi FROM obj_polozky"):
        pid = offer_map.get((ucet, lid)) or sku_map.get(sku)
        if pid:
            sold[pid] = sold.get(pid, 0) + (q or 0)
    for p in products:
        p["prodano"] = sold.get(p["id"], 0)
    data = {"ucty": rows_as_dicts(con, "SELECT id, kanal, nazev, mena, jazyk FROM kanal_ucty WHERE aktivni=1 ORDER BY nazev"),
            "produkty": products}
    con.close()
    return data


def adoption_proposal(cfg, db_path=None):
    """Listingy z Etsy, které ještě nemají nabídku v katalogu, s návrhem: spárovat s produktem, nebo založit nový."""
    tokens = load_tokens()
    by_name = {tok.get("shop_name", sid): sid for sid, tok in tokens.items()}
    con = db(db_path)
    linked = {(r[0], r[1]) for r in con.execute("SELECT ucet_id, externi_id FROM nabidky")}
    prods = rows_as_dicts(con, "SELECT id, sku, nazev FROM produkty")
    by_sku = {p["sku"]: p["id"] for p in prods if p["sku"]}
    by_sku.update({r[0]: r[1] for r in con.execute("SELECT sku, produkt_id FROM produkt_varianty WHERE sku<>''")})
    by_title = {norm_title(p["nazev"]): p["id"] for p in prods}
    out = []
    for l in rows_as_dicts(con, "SELECT shop, listing_id, nazev, stav, cena, mena, obrazek, sku FROM listingy "
                                "WHERE listing_id > 0 ORDER BY shop, nazev"):
        shop_id = by_name.get(l["shop"])
        if not shop_id or (etsy_account(shop_id), str(l["listing_id"])) in linked:
            continue
        skus = [s.strip() for s in (l["sku"] or "").split(",") if s.strip()]
        match = next((by_sku[s] for s in skus if s in by_sku), None)
        why = "sku" if match else None
        if not match and norm_title(l["nazev"]) in by_title:
            match, why = by_title[norm_title(l["nazev"])], "nazev"
        out.append(dict(l, shop_id=shop_id, produkt_id=match, duvod=why))
    con.close()
    return {"listingy": out, "produkty": prods}


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "etsy-dashboard"})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
        data = resp.read(MAX_MEDIA + 1)
    if len(data) > MAX_MEDIA:
        raise AppError("media_big", "Soubor je větší než 50 MB.")
    return data


def _safe_name(name):
    name = os.path.basename(str(name or "")).strip().replace("\\", "_")
    return re.sub(r"[^\w.\- ]+", "_", name)[:120] or "soubor"


def add_media(con, pid, druh, nazev, data, zdroj, poradi=None):
    """Uloží fotku nebo soubor produktu do data/media/<produkt>/. Stejný obsah se podruhé neuloží."""
    digest = hashlib.sha256(data).hexdigest()
    if con.execute("SELECT 1 FROM produkt_media WHERE produkt_id=? AND hash=?", (pid, digest)).fetchone():
        return False
    nazev = _safe_name(nazev)
    folder = os.path.join(MEDIA_DIR, str(pid))
    os.makedirs(folder, exist_ok=True)
    stem, ext = os.path.splitext(nazev)
    fname, n = nazev, 1
    while os.path.exists(os.path.join(folder, fname)):
        n += 1
        fname = f"{stem}-{n}{ext}"
    with open(os.path.join(folder, fname), "wb") as f:
        f.write(data)
    if poradi is None:
        poradi = (con.execute("SELECT MAX(poradi) FROM produkt_media WHERE produkt_id=? AND druh=?", (pid, druh)).fetchone()[0] or 0) + 1
    # soubor známý jen podle jména (např. z Etsy, kam API nedovolí sáhnout) teď dostane obsah
    row = con.execute("SELECT id FROM produkt_media WHERE produkt_id=? AND druh=? AND nazev=? AND cesta IS NULL",
                      (pid, druh, nazev)).fetchone()
    if row:
        con.execute("UPDATE produkt_media SET cesta=?, velikost=?, hash=?, zdroj=? WHERE id=?",
                    (f"{pid}/{fname}", len(data), digest, zdroj, row[0]))
    else:
        con.execute("INSERT INTO produkt_media (produkt_id, druh, nazev, cesta, velikost, hash, poradi, zdroj) VALUES (?,?,?,?,?,?,?,?)",
                    (pid, druh, nazev, f"{pid}/{fname}", len(data), digest, poradi, zdroj))
    return True


def _layer(con, pid, rozsah, jazyk="en"):
    row = con.execute("SELECT id FROM kanal_data WHERE produkt_id=? AND rozsah=? AND jazyk=?", (pid, rozsah, jazyk)).fetchone()
    if row:
        return row[0]
    return con.execute("INSERT INTO kanal_data (produkt_id, rozsah, jazyk, zmeneno_ts) VALUES (?,?,?,?)",
                       (pid, rozsah, jazyk, int(time.time()))).lastrowid


def _snapshot_hash(snap):
    return hashlib.sha256(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def adopt_listing(cfg, body, db_path=None, demo=False):
    """Převezme existující listing do katalogu: založí produkt (nebo použije zvolený), stáhne fotky,
    Etsy údaje uloží do vrstvy shopy a listing propojí jako nabídku. Na Etsy nic nemění."""
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    shop_id = str(body.get("shop_id") or "")
    lid = int(body.get("listing_id") or 0)
    det = listing_detail(cfg, shop_id, lid)
    tokens = load_tokens()
    images = sorted(api_get(cfg, tokens, shop_id, f"/listings/{lid}/images").get("results", []), key=lambda i: i.get("rank", 0))
    pictures, errors = [], []
    for i, im in enumerate(images, 1):  # stáhnout dřív, než se cokoli zapíše
        url = im.get("url_fullxfull") or im.get("url_570xN")
        try:
            ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".jpg"
            pictures.append((f"etsy-{lid}-{i}{ext}", _download(url)))
        except Exception as e:
            errors.append(f"{url}: {e}")
    ucet = etsy_account(shop_id)
    now = int(time.time())
    with LOCK:
        con = db(db_path)
        try:
            ensure_accounts(con)
            if con.execute("SELECT 1 FROM nabidky WHERE ucet_id=? AND externi_id=?", (ucet, str(lid))).fetchone():
                raise AppError("adopt_done", "Tenhle listing už je v katalogu.")
            pid = int(body["produkt_id"]) if body.get("produkt_id") else None
            prod = con.execute("SELECT id, nazev, popis FROM produkty WHERE id=?", (pid,)).fetchone() if pid else None
            if pid and not prod:
                raise AppError("adopt_product", "Zvolený produkt v katalogu není.")
            skus = [p["sku"] for p in det["produkty"] if p.get("sku")]
            variants = [p for p in det["produkty"] if p.get("hodnoty")]
            if not prod:
                sku = skus[0] if len(det["produkty"]) == 1 and skus else propose_sku(con, det["nazev"])
                if con.execute("SELECT 1 FROM produkty WHERE sku=?", (sku,)).fetchone():
                    sku = propose_sku(con, det["nazev"])
                pid = con.execute("INSERT INTO produkty (sku, typ, nazev, popis, jazyk, vytvoreno_ts, zmeneno_ts) VALUES (?,?,?,?,?,?,?)",
                                  (sku, det["typ"], det["nazev"], det["popis"], "en", now, now)).lastrowid
                prod = (pid, det["nazev"], det["popis"])
                for n, v in enumerate(variants, 1):
                    con.execute("INSERT INTO produkt_varianty (produkt_id, sku, vlastnosti, aktivni, poradi) VALUES (?,?,?,?,?)",
                                (pid, v.get("sku") or "", json.dumps({h["nazev"]: h["hodnota"] for h in v["hodnoty"]}, ensure_ascii=False),
                                 int(v.get("aktivni", True) is not False), n))
            else:
                have = {r[0] for r in con.execute("SELECT vlastnosti FROM produkt_varianty WHERE produkt_id=?", (pid,))}
                for v in variants:
                    props = json.dumps({h["nazev"]: h["hodnota"] for h in v["hodnoty"]}, ensure_ascii=False)
                    if props not in have:
                        con.execute("INSERT INTO produkt_varianty (produkt_id, sku, vlastnosti, aktivni, poradi) VALUES (?,?,?,?,?)",
                                    (pid, v.get("sku") or "", props, int(v.get("aktivni", True) is not False), 99))
            for i, (name, data) in enumerate(pictures, 1):
                add_media(con, pid, "foto", name, data, "etsy", i)
            for f in det["soubory"]:  # soubory ke stažení API neposkytne, zůstanou jen jménem, dokud nepřijdou ze složky
                name = _safe_name(f["nazev"])
                if not con.execute("SELECT 1 FROM produkt_media WHERE produkt_id=? AND druh='soubor' AND nazev=?", (pid, name)).fetchone():
                    con.execute("INSERT INTO produkt_media (produkt_id, druh, nazev, velikost, poradi, zdroj) VALUES (?,?,?,?,?,?)",
                                (pid, "soubor", name, _bytes(f.get("velikost")), 99, "etsy"))
            # vrstva shopy: Etsy údaje; název a popis jen když se liší od produktu
            lay = _layer(con, pid, ucet)
            extra = {k: det.get(k) for k in ("doprava", "zpracovani", "produkty", "cena_dle", "mnozstvi_dle", "sku_dle",
                                             "personalizace", "personalizace_nove", "mnozstvi")}
            con.execute("UPDATE kanal_data SET nazev=?, popis=?, stitky=?, atributy=?, kategorie_id=?, cena=?, mena=?, extra=?, zmeneno_ts=? WHERE id=?",
                        (det["nazev"] if det["nazev"] != prod[1] else None, det["popis"] if det["popis"] != prod[2] else None,
                         json.dumps(det["stitky"], ensure_ascii=False), json.dumps(det["atributy"], ensure_ascii=False),
                         det["kategorie"], det["cena"], _account_currency(con, ucet, lid),
                         json.dumps(extra, ensure_ascii=False), now, lay))
            snap = {k: v for k, v in det.items() if k not in ("url",)}
            modified = con.execute("SELECT zmeneno_ts FROM listingy WHERE listing_id=?", (lid,)).fetchone()
            con.execute("INSERT INTO nabidky (produkt_id, ucet_id, externi_id, stav, odeslano, hash, zmeneno_v_kanalu_ts, vytvoreno_ts) "
                        "VALUES (?,?,?,?,?,?,?,?)", (pid, ucet, str(lid), det["stav"], json.dumps(snap, ensure_ascii=False),
                                                     _snapshot_hash(snap), modified[0] if modified else None, now))
            sku = con.execute("SELECT sku FROM produkty WHERE id=?", (pid,)).fetchone()[0]
            con.commit()
        finally:
            con.close()
    return {"produkt_id": pid, "sku": sku, "chyby": errors}


def _bytes(v):
    """Velikost od Etsy („1.2 MB“) na bajty."""
    m = re.match(r"\s*([\d.]+)\s*([KMG]?B)?", str(v or ""), re.I)
    if not m:
        return None
    return int(float(m.group(1)) * {"KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}.get((m.group(2) or "").upper(), 1))


def _account_currency(con, ucet, lid):
    """Měna účtu; dokud ji synchronizace nezná, měna listingu."""
    row = con.execute("SELECT mena FROM kanal_ucty WHERE id=?", (ucet,)).fetchone()
    if row and row[0]:
        return row[0]
    row = con.execute("SELECT mena FROM listingy WHERE listing_id=?", (lid,)).fetchone()
    return row[0] if row and row[0] else ""


def _price(text):
    """„$4.99 (launch sale…)“ → (4.99, "USD")."""
    s = str(text or "")
    m = re.search(r"\d+(?:[.,]\d+)?", s)
    if not m:
        return None, ""
    cur = next((c for sym, c in (("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("Kč", "CZK"), ("CZK", "CZK"), ("EUR", "EUR"),
                                  ("USD", "USD")) if sym in s), "")
    return float(m.group(0).replace(",", ".")), cur


def _decode(items):
    out = []
    for it in items or []:
        try:
            data = base64.b64decode(it.get("data") or "", validate=False)
        except (ValueError, TypeError):
            raise AppError("listing_bad_file", f"Soubor {it.get('nazev')} se nepodařilo přečíst.", soubor=it.get("nazev", ""))
        if len(data) > MAX_MEDIA:
            raise AppError("media_big", "Soubor je větší než 50 MB.")
        out.append((it.get("nazev") or "soubor", data))
    return out


def import_folder(body, db_path=None, demo=False):
    """Jeden produkt ze složky (etsy-listing.md + obrázky + soubory). Spáruje se s produktem podle složky nebo názvu,
    jinak se založí nový. Text z etsy-listing.md patří do vrstvy pro celé Etsy."""
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    folder = str(body.get("slozka") or "").strip()
    title = " ".join(str(body.get("nazev") or "").split())
    if not title:
        raise AppError("folder_title", "V etsy-listing.md chybí název.")
    images, files = _decode(body.get("obrazky")), _decode(body.get("soubory"))
    tags = [t.strip() for t in str(body.get("stitky") or "").split(",") if t.strip()]
    price, cur = _price(body.get("cena"))
    now = int(time.time())
    with LOCK:
        con = db(db_path)
        try:
            row = None
            if body.get("produkt_id"):
                row = con.execute("SELECT id FROM produkty WHERE id=?", (int(body["produkt_id"]),)).fetchone()
            if not row and folder:
                row = con.execute("SELECT id FROM produkty WHERE slozka=?", (folder,)).fetchone()
            if not row:
                key = norm_title(title)
                row = next(((r[0],) for r in con.execute("SELECT id, nazev FROM produkty") if norm_title(r[1]) == key), None)
            created = not row
            if created:
                pid = con.execute("INSERT INTO produkty (sku, typ, nazev, popis, slozka, jazyk, vytvoreno_ts, zmeneno_ts) VALUES (?,?,?,?,?,?,?,?)",
                                  (propose_sku(con, title), "digital" if files else "physical", title, body.get("popis") or "",
                                   folder, "en", now, now)).lastrowid
            else:
                pid = row[0]
                con.execute("UPDATE produkty SET slozka=COALESCE(NULLIF(slozka,''), ?), zmeneno_ts=? WHERE id=?", (folder, now, pid))
            added = sum(add_media(con, pid, "foto", n, d, "slozka") for n, d in images)
            added += sum(add_media(con, pid, "soubor", n, d, "slozka") for n, d in files)
            lay = _layer(con, pid, "etsy")
            cur_tags, cur_price = con.execute("SELECT stitky, cena FROM kanal_data WHERE id=?", (lay,)).fetchone()
            if not _jl(cur_tags, []) and tags:
                con.execute("UPDATE kanal_data SET stitky=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), lay))
            if cur_price is None and price is not None:
                con.execute("UPDATE kanal_data SET cena=?, mena=? WHERE id=?", (price, cur, lay))
            sku = con.execute("SELECT sku FROM produkty WHERE id=?", (pid,)).fetchone()[0]
            con.commit()
        finally:
            con.close()
    return {"produkt_id": pid, "sku": sku, "novy": created, "pridano": added}


def media_file(rel):
    """Cesta k souboru v data/media pro /media/<produkt>/<soubor>, nebo None (nic mimo složku médií)."""
    full = os.path.realpath(os.path.join(MEDIA_DIR, urllib.parse.unquote(rel)))
    if not full.startswith(os.path.realpath(MEDIA_DIR) + os.sep) or not os.path.isfile(full):
        return None
    return full, MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
