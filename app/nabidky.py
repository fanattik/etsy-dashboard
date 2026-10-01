"""Nabídky (fáze 3): data pro vystavení produktu z katalogu do účtu, propojení vytvořeného listingu
s produktem, poznání neodeslaných změn a změn udělaných přímo na Etsy, převzetí z Etsy a odpojení."""

import json
import time

from zaklad import AppError, LOCK
from databaze import db
from katalog import catalog_data, store_etsy_layer, store_snapshot
from etsy_listingy import listing_detail
from produkty import account_price, cached_rates, convert, round_price
from kanal_api import is_api, readopt

CUSTOM_PIDS = (513, 514)  # vlastní varianty na Etsy (první a druhá vlastnost)


def _pick(layers, key, empty=(None, "", [])):
    for lay in layers:
        if lay and lay.get(key) not in empty:
            return lay[key]
    return None


def resolve_offer(product, account, base_cur, rates):
    """Nabídka produktu pro účet ve tvaru listing_detail: vrstva účtu → vrstva kanálu → produkt → pravidla účtu."""
    rules = account.get("pravidla") or {}
    by_scope = {l["rozsah"]: l for l in product["vrstvy"]}
    layers = [by_scope.get(account["id"]), by_scope.get(account["kanal"])]
    extra = {}
    for lay in reversed(layers):  # bližší vrstva přepíše vzdálenější
        extra.update({k: v for k, v in ((lay or {}).get("extra") or {}).items() if v not in (None, "", [])})
    price = account_price(product, product["vrstvy"], account, base_cur, rates) or {}
    cur = price.get("mena") or account.get("mena") or ""
    popis = _pick(layers, "popis") or product["popis"] or ""
    footer = (rules.get("paticka") or "").strip()
    if footer and not popis.rstrip().endswith(footer):
        popis = popis.rstrip() + "\n\n" + footer
    digital = product["typ"] == "digital"
    qty = 999 if digital else (rules.get("mnozstvi") or extra.get("mnozstvi") or 1)
    variants, cena_dle, sku_dle = [], [], []
    known = {}  # vlastnosti, které Etsy už zná (z převzatého listingu), aby zůstala jejich ID
    for p in extra.get("produkty") or []:
        for h in p.get("hodnoty") or []:
            known.setdefault(h["nazev"], (h["property_id"], h.get("scale_id")))
    active = [v for v in product["varianty"] if v["aktivni"]]
    names = []
    for v in active:
        names += [n for n in v["vlastnosti"] if n not in names]
    names = names[:2]  # Etsy dovolí nejvýš dvě vlastnosti variant
    pids = [known.get(n, (CUSTOM_PIDS[i], None)) for i, n in enumerate(names)]
    for v in active:
        extra_price = convert(v["cena_rozdil"], base_cur or cur, cur or base_cur, rates) if v["cena_rozdil"] else 0
        base = price.get("cena")
        variants.append({"sku": v["sku"] or "", "aktivni": True, "zpracovani": None,
                         "hodnoty": [{"property_id": pids[i][0], "scale_id": pids[i][1], "nazev": n, "value_id": None,
                                      "hodnota": v["vlastnosti"].get(n, "")} for i, n in enumerate(names)],
                         "cena": round_price(base + (extra_price or 0), (rules.get("zaokrouhleni") or "")) if base is not None else None,
                         "mnozstvi": v["sklad"] if product["vyroba"] == "sklad" and v["sklad"] is not None else qty})
    if any(v["cena_rozdil"] for v in active):
        cena_dle = [pids[0][0]]
    if any(v["sku"] for v in active):
        sku_dle = [p[0] for p in pids]
    fotky = [m for m in product["media"] if m["druh"] == "foto" and m["url"]][:rules.get("max_fotek") or 10]
    soubory = [m for m in product["media"] if m["druh"] == "soubor" and m["url"]][:5] if digital else []
    return {"listing_id": None, "produkt_id": product["id"], "ucet_id": account["id"], "typ": product["typ"],
            "nazev": _pick(layers, "nazev") or product["nazev"], "popis": popis,
            "stitky": _pick(layers, "stitky") or [], "atributy": _pick(layers, "atributy") or [],
            "kategorie": _pick(layers, "kategorie_id") or rules.get("kategorie_id"),
            "cena": price.get("cena"), "mena": cur, "mnozstvi": qty, "sku": product["sku"],
            "doprava": extra.get("doprava") or rules.get("doprava_id"), "zpracovani": extra.get("zpracovani") or rules.get("zpracovani_id"),
            "personalizace": extra.get("personalizace") or [], "personalizace_nove": extra.get("personalizace_nove", True),
            "produkty": variants, "cena_dle": cena_dle, "mnozstvi_dle": [], "sku_dle": sku_dle,
            "obrazky": [{"url": m["url"], "nazev": m["nazev"]} for m in fotky],
            "soubory": [{"url": m["url"], "nazev": m["nazev"], "velikost": m["velikost"]} for m in soubory]}


def norm_snapshot(d):
    """Porovnatelná podoba nabídky: to, co katalog do kanálu posílá (název, popis, štítky, cena, kategorie)."""
    if not d:
        return None
    try:
        price = round(float(str(d.get("cena")).replace(",", ".")), 2)
    except (TypeError, ValueError):
        price = None
    try:
        cat = int(d.get("kategorie")) if d.get("kategorie") else None
    except (TypeError, ValueError):
        cat = None
    return {"nazev": " ".join(str(d.get("nazev") or "").split()), "popis": str(d.get("popis") or "").strip().replace("\r", ""),
            "stitky": [" ".join(str(t).split()).lower() for t in d.get("stitky") or []], "cena": price, "kategorie": cat}


def changed_fields(product, account, offer_sent, base_cur, rates):
    now, sent = norm_snapshot(resolve_offer(product, account, base_cur, rates)), norm_snapshot(offer_sent)
    if not sent:
        return []
    return [k for k in now if now[k] != sent[k] and not (k == "cena" and now[k] is None)]


def offer_data(cfg, db_path, ucet, pid):
    data = catalog_data(db_path, cfg.get("zakladni_mena", ""), cfg.get("jazyk_katalogu") or "en")
    account = next((a for a in data["ucty"] if a["id"] == ucet), None)
    product = next((p for p in data["produkty"] if p["id"] == int(pid or 0)), None)
    if not account or not product:
        raise AppError("adopt_product", "Zvolený produkt v katalogu není.")
    return resolve_offer(product, account, data["zakladni_mena"], cached_rates())


def link_offer(body, result, db_path=None):
    """Po vytvoření nebo úpravě listingu z katalogu: propojit s produktem a uložit, co se odeslalo."""
    pid, lid = body.get("produkt_id"), result.get("listing_id")
    if not pid or not lid:
        return
    ucet = f"etsy:{body.get('shop_id')}"
    sent = {"nazev": body.get("nazev"), "popis": body.get("popis"), "stitky": body.get("stitky"),
            "cena": body.get("cena"), "kategorie": body.get("kategorie")}
    with LOCK:
        con = db(db_path)
        try:
            if not con.execute("SELECT 1 FROM produkty WHERE id=?", (int(pid),)).fetchone():
                return
            con.execute("INSERT OR IGNORE INTO nabidky (produkt_id, ucet_id, externi_id, stav, vytvoreno_ts) VALUES (?,?,?,?,?)",
                        (int(pid), ucet, str(lid), result.get("stav") or "draft", int(time.time())))
            con.execute("UPDATE nabidky SET stav=?, posledni_chyba=? WHERE ucet_id=? AND externi_id=?",
                        (result.get("stav") or "draft", "\n".join(result.get("chyby") or []) or None, ucet, str(lid)))
            store_snapshot(con, ucet, lid, sent, pushed=True)
            con.commit()
        finally:
            con.close()


def _offer(con, oid):
    row = con.execute("SELECT produkt_id, ucet_id, externi_id FROM nabidky WHERE id=?", (oid,)).fetchone()
    if not row:
        raise AppError("offer_missing", "Tahle nabídka už v katalogu není.")
    return row


def readopt_offer(cfg, body, db_path=None, demo=False):
    """Změny udělané přímo na Etsy převezme do vrstvy shopy, takže katalog a listing se zase shodují."""
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    con = db(db_path)
    try:
        pid, ucet, lid = _offer(con, body.get("id"))
    finally:
        con.close()
    if is_api(ucet):
        readopt(pid, ucet, lid, db_path)
        return {"ok": True}
    det = listing_detail(cfg, ucet.split(":", 1)[1], lid)
    with LOCK:
        con = db(db_path)
        try:
            store_etsy_layer(con, pid, ucet, det, int(time.time()))
            con.execute("UPDATE nabidky SET stav=? WHERE ucet_id=? AND externi_id=?", (det["stav"], ucet, str(lid)))
            store_snapshot(con, ucet, lid, det)
            con.commit()
        finally:
            con.close()
    return {"ok": True}


def unlink_offer(body, db_path=None, demo=False):
    """Odpojí listing od produktu. Na Etsy se nic nemaže."""
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    with LOCK:
        con = db(db_path)
        try:
            _offer(con, body.get("id"))
            con.execute("DELETE FROM nabidky WHERE id=?", (body.get("id"),))
            con.commit()
        finally:
            con.close()
    return {"ok": True}


def offer_states(con, products, accounts, base_cur, rates):
    """Doplní k nabídkám živý stav listingu a seznam změn: v katalogu (k odeslání) a na Etsy (cizí úprava)."""
    live = {("etsy", str(r[0])): (r[1], r[2], r[3], None) for r in con.execute("SELECT listing_id, stav, zmeneno_ts, mnozstvi FROM listingy")}
    live.update({(r[0], r[1]): (r[2], r[3], r[4], r[5]) for r in con.execute(
        "SELECT ucet_id, externi_id, stav, zmeneno_ts, mnozstvi, url FROM kanal_produkty")})
    sent = {r[0]: r[1] for r in con.execute("SELECT id, odeslano FROM nabidky")}
    by_id = {a["id"]: a for a in accounts}
    for p in products:
        for n in p["nabidky"]:
            key = ("etsy", n["externi_id"]) if n["ucet_id"].startswith("etsy:") else (n["ucet_id"], n["externi_id"])
            state, modified, qty, url = live.get(key, (None, None, None, None))
            if state:
                n["stav"] = state
            n["url"] = url or ""
            n["zmeny"] = []
            acct = by_id.get(n["ucet_id"])
            raw = sent.get(n["id"])
            if acct and raw:
                try:
                    n["zmeny_pole"] = changed_fields(p, acct, json.loads(raw), base_cur, rates)
                except (ValueError, TypeError):
                    n["zmeny_pole"] = []
                if n["zmeny_pole"]:
                    n["zmeny"].append("katalog")
            stock = [v["sklad"] for v in p["varianty"] if v["aktivni"] and v["sklad"] is not None]
            if p["vyroba"] == "sklad" and stock and qty is not None and qty != sum(stock):  # po prodeji ze skladu
                n["zmeny"].append("sklad")
            if modified and n.get("zmeneno_v_kanalu_ts") and modified > n["zmeneno_v_kanalu_ts"] + (0 if is_api(n["ucet_id"]) else 60):
                n["zmeny"].append("kanal")
