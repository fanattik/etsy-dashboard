"""Úpravy katalogu (fáze 2): produkt, varianty, média, vrstvy obsahu pro kanály a pravidla prodejních účtů.
Výpočet ceny pro účet ze základní ceny. Do kanálů se nic nezapisuje."""

import json
import math
import os
import shutil
import time

from zaklad import AppError, LOCK, MEDIA_DIR, RATES_PATH
from databaze import db
from katalog import _decode, _jl, add_media, propose_sku

TYPY = ("digital", "physical")
VYROBA = ("objednavka", "sklad")
ZAOKROUHLENI = ("", "99", "90", "00")


def _num(v, cast=float):
    s = str(v if v is not None else "").strip().replace(",", ".").replace(" ", "")
    if not s:
        return None
    try:
        return cast(float(s))
    except ValueError:
        raise AppError("product_number", f"„{v}“ není číslo.", hodnota=str(v))


def _text(v):
    return " ".join(str(v or "").split())


def _demo(demo):
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")


def _product(con, pid):
    if not pid or not con.execute("SELECT 1 FROM produkty WHERE id=?", (pid,)).fetchone():
        raise AppError("adopt_product", "Zvolený produkt v katalogu není.")
    return int(pid)


def save_product(body, db_path=None, demo=False):
    """Založí nebo upraví obecné údaje produktu."""
    _demo(demo)
    nazev = _text(body.get("nazev"))
    if not nazev:
        raise AppError("product_title", "Produkt potřebuje název.")
    typ = body.get("typ") if body.get("typ") in TYPY else "digital"
    vyroba = body.get("vyroba") if body.get("vyroba") in VYROBA else "objednavka"
    vals = {"nazev": nazev, "popis": str(body.get("popis") or "").strip(), "typ": typ, "vyroba": vyroba,
            "kategorie": _text(body.get("kategorie")), "zakladni_cena": _num(body.get("zakladni_cena")),
            "hmotnost_g": _num(body.get("hmotnost_g")), "delka_mm": _num(body.get("delka_mm")),
            "sirka_mm": _num(body.get("sirka_mm")), "vyska_mm": _num(body.get("vyska_mm"))}
    if any(vals[k] is not None and vals[k] < 0 for k in ("zakladni_cena", "hmotnost_g", "delka_mm", "sirka_mm", "vyska_mm")):
        raise AppError("product_negative", "Cena ani rozměry nemůžou být záporné.")
    sku = _text(body.get("sku")).upper().replace(" ", "-")
    now = int(time.time())
    with LOCK:
        con = db(db_path)
        try:
            pid = _product(con, body["id"]) if body.get("id") else None
            sku = sku or (con.execute("SELECT sku FROM produkty WHERE id=?", (pid,)).fetchone()[0] if pid else propose_sku(con, nazev))
            if con.execute("SELECT 1 FROM produkty WHERE sku=? AND id IS NOT ?", (sku, pid)).fetchone():
                raise AppError("product_sku", f"SKU {sku} už má jiný produkt.", sku=sku)
            vals["sku"] = sku
            if pid:
                con.execute(f"UPDATE produkty SET {', '.join(k + '=?' for k in vals)}, zmeneno_ts=? WHERE id=?",
                            (*vals.values(), now, pid))
            else:
                vals["jazyk"] = str(body.get("jazyk") or "en")
                pid = con.execute(f"INSERT INTO produkty ({', '.join(vals)}, vytvoreno_ts, zmeneno_ts) VALUES "
                                  f"({', '.join('?' * len(vals))}, ?, ?)", (*vals.values(), now, now)).lastrowid
            con.commit()
        finally:
            con.close()
    return {"produkt_id": pid, "sku": sku}


def delete_product(body, db_path=None, demo=False):
    """Smaže produkt z katalogu i s lokálními médii. Listingy na Etsy zůstanou, jen přestanou být propojené."""
    _demo(demo)
    with LOCK:
        con = db(db_path)
        try:
            pid = _product(con, body.get("id"))
            offers = con.execute("SELECT COUNT(*) FROM nabidky WHERE produkt_id=?", (pid,)).fetchone()[0]
            for table in ("produkt_varianty", "produkt_media", "kanal_data", "nabidky"):
                con.execute(f"DELETE FROM {table} WHERE produkt_id=?", (pid,))
            con.execute("DELETE FROM produkty WHERE id=?", (pid,))
            con.commit()
        finally:
            con.close()
    shutil.rmtree(os.path.join(MEDIA_DIR, str(pid)), ignore_errors=True)
    return {"ok": True, "odpojeno": offers}


def save_variants(body, db_path=None, demo=False):
    """Nahradí varianty produktu seznamem z formuláře (vlastnosti, SKU, příplatek, sklad)."""
    _demo(demo)
    rows, seen = [], set()
    for n, v in enumerate(body.get("varianty") or [], 1):
        props = {_text(k): _text(x) for k, x in (v.get("vlastnosti") or {}).items() if _text(k) and _text(x)}
        if not props:
            continue
        sku = _text(v.get("sku")).upper().replace(" ", "-")
        if sku and sku in seen:
            raise AppError("variant_sku", f"SKU {sku} je u dvou variant.", sku=sku)
        seen.add(sku)
        sklad = _num(v.get("sklad"), int)
        rows.append((sku, json.dumps(props, ensure_ascii=False), _num(v.get("cena_rozdil")),
                     sklad, int(v.get("aktivni", True) is not False), n))
    with LOCK:
        con = db(db_path)
        try:
            pid = _product(con, body.get("produkt_id"))
            for sku, *_ in rows:
                if sku and con.execute("SELECT 1 FROM produkty WHERE sku=? AND id<>?", (sku, pid)).fetchone():
                    raise AppError("product_sku", f"SKU {sku} už má jiný produkt.", sku=sku)
            con.execute("DELETE FROM produkt_varianty WHERE produkt_id=?", (pid,))
            con.executemany("INSERT INTO produkt_varianty (produkt_id, sku, vlastnosti, cena_rozdil, sklad, aktivni, poradi) "
                            "VALUES (?,?,?,?,?,?,?)", [(pid, *r) for r in rows])
            con.execute("UPDATE produkty SET zmeneno_ts=? WHERE id=?", (int(time.time()), pid))
            con.commit()
        finally:
            con.close()
    return {"ok": True, "pocet": len(rows)}


def media_action(body, db_path=None, demo=False):
    """Fotky a soubory produktu: přidat, smazat, seřadit."""
    _demo(demo)
    akce = body.get("akce")
    files = _decode(body.get("soubory")) if akce == "pridat" else []
    with LOCK:
        con = db(db_path)
        try:
            pid = _product(con, body.get("produkt_id"))
            out = {"ok": True}
            if akce == "pridat":
                druh = "foto" if body.get("druh") == "foto" else "soubor"
                out["pridano"] = sum(add_media(con, pid, druh, n, d, "rucne") for n, d in files)
            elif akce == "smazat":
                row = con.execute("SELECT cesta FROM produkt_media WHERE id=? AND produkt_id=?", (body.get("id"), pid)).fetchone()
                con.execute("DELETE FROM produkt_media WHERE id=? AND produkt_id=?", (body.get("id"), pid))
                if row and row[0] and not con.execute("SELECT 1 FROM produkt_media WHERE cesta=?", (row[0],)).fetchone():
                    try:
                        os.remove(os.path.join(MEDIA_DIR, row[0]))
                    except OSError:
                        pass
            elif akce == "poradi":
                for n, mid in enumerate(body.get("ids") or [], 1):
                    con.execute("UPDATE produkt_media SET poradi=? WHERE id=? AND produkt_id=?", (n, mid, pid))
            else:
                raise AppError("bad_action", "Neznámá akce.")
            con.execute("UPDATE produkty SET zmeneno_ts=? WHERE id=?", (int(time.time()), pid))
            con.commit()
        finally:
            con.close()
    return out


def save_layer(body, db_path=None, demo=False):
    """Vrstva obsahu pro kanál (celé Etsy) nebo účet (jeden shop). Prázdné pole = dědí se o úroveň výš.
    Atributy, personalizace a další údaje z Etsy (extra) zůstávají, upravují se ve formuláři nabídky."""
    _demo(demo)
    rozsah = str(body.get("rozsah") or "")
    if not (rozsah == "etsy" or rozsah.startswith("etsy:") or rozsah.startswith("api:")):
        raise AppError("layer_scope", "Neznámý kanál.")
    etsy = rozsah.startswith("etsy")
    nazev = _text(body.get("nazev")) or None
    popis = str(body.get("popis") or "").strip() or None
    tags = body.get("stitky")
    tags = [_text(t) for t in (tags if isinstance(tags, list) else str(tags or "").split(",")) if _text(t)]
    if etsy and nazev and len(nazev) > 140:
        raise AppError("layer_title", "Název pro Etsy může mít nejvýš 140 znaků.")
    if etsy and (len(tags) > 13 or any(len(t) > 20 for t in tags)):
        raise AppError("listing_tags", "Etsy dovolí nejvýš 13 štítků, každý do 20 znaků.")
    cena = _num(body.get("cena"))
    kat = _num(body.get("kategorie_id"), int)
    with LOCK:
        con = db(db_path)
        try:
            pid = _product(con, body.get("produkt_id"))
            jazyk = str(body.get("jazyk") or "en")
            row = con.execute("SELECT id, atributy, extra FROM kanal_data WHERE produkt_id=? AND rozsah=? AND jazyk=?",
                              (pid, rozsah, jazyk)).fetchone()
            mena = (str(body.get("mena") or "").upper() or None) if cena is not None else None
            empty = not any((nazev, popis, tags, cena is not None, kat))
            if empty and not (row and (_jl(row[1], []) or _jl(row[2], {}))):
                if row:
                    con.execute("DELETE FROM kanal_data WHERE id=?", (row[0],))
            else:
                vals = (nazev, popis, json.dumps(tags, ensure_ascii=False) if tags else None, kat, cena, mena, int(time.time()))
                if row:
                    con.execute("UPDATE kanal_data SET nazev=?, popis=?, stitky=?, kategorie_id=?, cena=?, mena=?, zmeneno_ts=? WHERE id=?",
                                (*vals, row[0]))
                else:
                    con.execute("INSERT INTO kanal_data (produkt_id, rozsah, jazyk, nazev, popis, stitky, kategorie_id, cena, mena, zmeneno_ts) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?)", (pid, rozsah, jazyk, *vals))
            con.commit()
        finally:
            con.close()
    return {"ok": True}


def save_rules(body, db_path=None, demo=False):
    """Pravidla prodejního účtu: výpočet ceny, množství, fotky, patička popisu a výchozí profily."""
    _demo(demo)
    p = body.get("pravidla") or {}
    koef = _num(p.get("koeficient"))
    if koef is not None and koef <= 0:
        raise AppError("rules_coef", "Koeficient musí být větší než nula.")
    fotky = _num(p.get("max_fotek"), int)
    rules = {"koeficient": koef if koef is not None else 1.0,
             "zaokrouhleni": p.get("zaokrouhleni") if p.get("zaokrouhleni") in ZAOKROUHLENI else "",
             "mnozstvi": _num(p.get("mnozstvi"), int),
             "max_fotek": max(1, min(10, fotky)) if fotky else 10,
             "paticka": str(p.get("paticka") or "").strip(),
             "doprava_id": _num(p.get("doprava_id"), int), "zpracovani_id": _num(p.get("zpracovani_id"), int),
             "kategorie_id": _num(p.get("kategorie_id"), int)}
    with LOCK:
        con = db(db_path)
        try:
            row = con.execute("SELECT kanal FROM kanal_ucty WHERE id=?", (body.get("ucet_id"),)).fetchone()
            if not row:
                raise AppError("rules_account", "Tenhle účet v aplikaci není.")
            con.execute("UPDATE kanal_ucty SET pravidla=? WHERE id=?", (json.dumps(rules, ensure_ascii=False), body["ucet_id"]))
            if row[0] != "etsy" and body.get("mena"):  # u Etsy určuje měnu shop
                con.execute("UPDATE kanal_ucty SET mena=? WHERE id=?", (str(body["mena"]).upper()[:3], body["ucet_id"]))
            con.commit()
        finally:
            con.close()
    return {"ok": True, "pravidla": rules}


def cached_rates():
    """Poslední stažené kurzy ECB (1 EUR = x měny), bez stahování."""
    try:
        with open(RATES_PATH, encoding="utf-8") as f:
            return json.load(f).get("kurzy") or {}
    except (OSError, ValueError):
        return {}


def convert(v, cur_from, cur_to, rates):
    if v is None or not cur_from or not cur_to or cur_from == cur_to:
        return v
    if cur_from not in rates or cur_to not in rates:
        return None
    return v / rates[cur_from] * rates[cur_to]


def round_price(v, mode):
    """Zaokrouhlení na konec .99 / .90 / celé číslo, k nejbližší takové ceně."""
    if v is None:
        return None
    if mode == "00":
        return float(max(1, round(v)))
    if mode in ("99", "90"):
        end = int(mode) / 100
        options = [c for c in (math.floor(v) - 1 + end, math.floor(v) + end) if c > 0] or [end]
        return round(min(options, key=lambda c: abs(c - v)), 2)
    return round(v, 2)


def account_price(product, layers, account, base_cur, rates):
    """Cena produktu pro účet a odkud se vzala: vrstva účtu → vrstva kanálu → výpočet ze základní ceny."""
    cur = account.get("mena") or ""
    for scope, src in ((account["id"], "ucet"), (account["kanal"], "kanal")):
        lay = next((l for l in layers if l["rozsah"] == scope and l.get("cena") is not None), None)
        if lay:
            v = convert(lay["cena"], lay.get("mena") or cur, cur, rates)
            if v is not None:
                return {"cena": round(v, 2), "mena": cur or lay.get("mena") or "", "zdroj": src}
    rules = account.get("pravidla") or {}
    if product.get("zakladni_cena") is None:
        return None
    v = convert(product["zakladni_cena"], base_cur or cur, cur or base_cur, rates)
    if v is None:
        return {"cena": None, "mena": cur, "zdroj": "bez_kurzu"}
    return {"cena": round_price(v * (rules.get("koeficient") or 1), rules.get("zaokrouhleni") or ""),
            "mena": cur or base_cur, "zdroj": "vypocet"}
