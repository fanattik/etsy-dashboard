"""Data pro web (přehled, statistiky), export CSV, ruční údaje o dopravě a demo data."""

import csv
import io
import json
import os
import random
import time
from datetime import datetime, timedelta

from zaklad import AppError
from databaze import db, rows_as_dicts


def stats_data(con):
    """Denní přírůstky zobrazení a oblíbených z uložených stavů (jen nenulové), sledující, recenze, města objednávek."""
    delty, prev = [], {}
    for d, lid, v, f in con.execute("SELECT datum, listing_id, zobrazeni, oblibene FROM stat_listingy ORDER BY listing_id, datum"):
        if lid in prev:
            pv, pf = prev[lid]
            dv = max(0, (v or 0) - (pv or 0)) if v is not None and pv is not None else 0
            df = (f or 0) - (pf or 0) if f is not None and pf is not None else 0
            if dv or df:
                delty.append([d, lid, dv, df])
        prev[lid] = (v, f)
    first = con.execute("SELECT MIN(datum) FROM stat_listingy").fetchone()[0]
    sled = {}
    for d, shop, n in con.execute("SELECT datum, shop, sledujici FROM stat_shop ORDER BY datum"):
        sled.setdefault(shop, []).append([d, n])
    return {"od": first, "delty": delty, "sledujici": sled,
            "recenze": rows_as_dicts(con, "SELECT shop, listing_id, hodnoceni, text, ts FROM recenze ORDER BY ts DESC"),
            "info": {r[0]: [r[1], r[2], r[3]] for r in con.execute("SELECT receipt_id, kupujici, mesto, zeme FROM obj_info")}}


def dashboard_data(db_path=None, lang="cs"):
    from objednavky import orders_data  # objednavky importuje databázi i Etsy API
    con = db(db_path)
    data = {
        "objednavky": rows_as_dicts(con, "SELECT * FROM objednavky ORDER BY vytvoreno_ts DESC"),
        "vypis": rows_as_dicts(con, "SELECT * FROM vypis ORDER BY datum_ts DESC, entry_id DESC"),
        "listingy": rows_as_dicts(con, "SELECT * FROM listingy ORDER BY nazev"),
        "doprava": rows_as_dicts(con, "SELECT * FROM doprava"),
        "slevy": rows_as_dicts(con, "SELECT id, shop_id, listing_id, procento, od_ts, do_ts, stav, chyba FROM slevy "
                                    "WHERE stav IN ('naplanovano','bezi') OR do_ts > strftime('%s','now') - 30*86400 ORDER BY od_ts"),
        "statistiky": stats_data(con),
        **orders_data(con, lang),
    }
    con.close()
    return data


CSV_HEADERS = {
    "cs": {"stav_vlastni": "Vlastní stav", "dopravce": "Dopravce", "cislo_zasilky": "Číslo zásilky", "cena_dopravy": "Cena dopravy", "mena_dopravy": "Měna dopravy", "listing_id": "ID listingu", "nazev": "Název", "cena": "Cena", "mnozstvi": "Skladem", "zobrazeni": "Zobrazení", "oblibene": "Oblíbené", "stitky": "Štítky", "sku": "SKU", "url": "Odkaz", "shop": "Shopa", "receipt_id": "Číslo objednávky", "vytvoreno_ts": "Datum", "zakaznik": "Zákazník",
           "polozky": "Položky", "celkem": "Celkem", "mena": "Měna", "zaplaceno": "Zaplaceno",
           "odeslano": "Odesláno", "stav": "Stav", "entry_id": "ID pohybu", "datum_ts": "Datum",
           "typ": "Typ", "popis": "Popis", "castka": "Částka", "zustatek": "Zůstatek",
           "reference": "Reference"},
    "en": {"stav_vlastni": "Own state", "dopravce": "Carrier", "cislo_zasilky": "Tracking number", "cena_dopravy": "Shipping cost", "mena_dopravy": "Shipping currency", "listing_id": "Listing ID", "nazev": "Title", "cena": "Price", "mnozstvi": "Quantity", "zobrazeni": "Views", "oblibene": "Favorites", "stitky": "Tags", "sku": "SKU", "url": "URL", "shop": "Shop", "receipt_id": "Order ID", "vytvoreno_ts": "Date", "zakaznik": "Buyer",
           "polozky": "Items", "celkem": "Total", "mena": "Currency", "zaplaceno": "Paid",
           "odeslano": "Shipped", "stav": "Status", "entry_id": "Entry ID", "datum_ts": "Date",
           "typ": "Type", "popis": "Description", "castka": "Amount", "zustatek": "Balance",
           "reference": "Reference"},
    "de": {"stav_vlastni": "Eigener Status", "dopravce": "Versanddienst", "cislo_zasilky": "Sendungsnummer", "cena_dopravy": "Versandkosten", "mena_dopravy": "Versandwährung", "listing_id": "Angebots-ID", "nazev": "Titel", "cena": "Preis", "mnozstvi": "Bestand", "zobrazeni": "Aufrufe", "oblibene": "Favoriten", "stitky": "Tags", "sku": "SKU", "url": "Link", "shop": "Shop", "receipt_id": "Bestellnr.", "vytvoreno_ts": "Datum", "zakaznik": "Kunde",
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
                "zaplaceno", "odeslano", "stav", "stav_vlastni", "dopravce", "cislo_zasilky", "cena_dopravy", "mena_dopravy"]
        rows = con.execute("SELECT o.shop, o.receipt_id, o.vytvoreno_ts, o.zakaznik, o.polozky, o.celkem, o.mena, "
                           "o.zaplaceno, o.odeslano, o.stav, so.nazev, d.dopravce, d.cislo, d.cena, d.mena FROM objednavky o "
                           "LEFT JOIN doprava d ON d.receipt_id=o.receipt_id LEFT JOIN obj_stav os ON os.receipt_id=o.receipt_id "
                           "LEFT JOIN stavy_objednavek so ON so.id=os.stav_id ORDER BY o.shop, o.vytvoreno_ts").fetchall()
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
    # demo statistiky: denní stavy zobrazení a oblíbených za 120 dní, sledující, recenze, města
    cities = [("Austin", "US"), ("Denver", "US"), ("Seattle", "US"), ("Toronto", "CA"), ("London", "GB"), ("Berlin", "DE"), ("Prague", "CZ")]
    buyers = [f"u{i}" for i in range(60)]
    for (rid,) in con.execute("SELECT receipt_id FROM objednavky").fetchall():
        c = rnd.choice(cities)
        con.execute("INSERT INTO obj_info VALUES (?,?,?,?)", (rid, rnd.choice(buyers), c[0], c[1]))
    for shop, lid0, views, favs in con.execute("SELECT shop, listing_id, zobrazeni, oblibene FROM listingy WHERE stav != 'draft'").fetchall():
        v, f = views, favs
        for d in range(0, 121):
            day = (now - timedelta(days=d)).strftime("%Y-%m-%d")
            con.execute("INSERT INTO stat_listingy VALUES (?,?,?,?,?)", (day, lid0, shop, v, f))
            v -= rnd.choice([0, 1, 2, 3, 5, 8]) * (2 if (now - timedelta(days=d)).weekday() >= 5 else 1)
            f -= 1 if rnd.random() < .15 else 0
    for shop, n in (("DemoPrintables", 412), ("DemoHandmade", 158)):
        for d in range(0, 121):
            con.execute("INSERT INTO stat_shop VALUES (?,?,?)", ((now - timedelta(days=d)).strftime("%Y-%m-%d"), shop, n))
            n -= 1 if rnd.random() < .3 else 0
    texts = ["Love it, thank you!", "Exactly as described.", "Beautiful quality, fast delivery.", "Great value.", ""]
    for i, (shop, lid0, ts) in enumerate(con.execute("SELECT o.shop, l.listing_id, o.vytvoreno_ts FROM objednavky o JOIN listingy l "
                                                     "ON l.shop = o.shop AND o.polozky LIKE '%' || l.nazev WHERE o.vytvoreno_ts > ?",
                                                     (int(time.time()) - 200 * 86400,)).fetchall()):
        if rnd.random() < .25:
            con.execute("INSERT OR IGNORE INTO recenze VALUES (?,?,?,?,?,?)", (f"demo{i}", shop, lid0, rnd.choice([5, 5, 5, 4, 4, 3]), rnd.choice(texts), ts + 6 * 86400))
    for (rid, ts) in con.execute("SELECT receipt_id, vytvoreno_ts FROM objednavky WHERE shop='DemoHandmade' AND odeslano=1").fetchall():
        if rnd.random() < .8:
            carrier = rnd.choice(["Zásilkovna", "Česká pošta", "PPL", "DPD"])
            con.execute("INSERT INTO doprava VALUES (?,?,?,?,?,?)", (rid, carrier, f"Z{rnd.randint(10**9, 10**10 - 1)}",
                        rnd.choice([3.2, 3.9, 4.6, 5.8]), "USD", ts))
    demo_catalog(con)
    con.commit()
    con.close()


def demo_catalog(con):
    """Demo katalog: většina listingů je převzatá jako produkt s nabídkou, dva zůstávají mimo katalog
    a jeden produkt (ze složky) ještě nikde vystavený není."""
    now = int(time.time())
    accounts = {"DemoPrintables": "etsy:1001", "DemoHandmade": "etsy:1002"}
    for shop, ucet in accounts.items():
        con.execute("INSERT INTO kanal_ucty (id, kanal, nazev, mena, jazyk) VALUES (?,?,?,?,?)", (ucet, "etsy", shop, "USD", "en"))
    skip = {"Meal Planner", "Wooden Coaster Set"}
    for shop, lid, name, state, price, cur, tags, sku, desc in con.execute(
            "SELECT shop, listing_id, nazev, stav, cena, mena, stitky, sku, popis FROM listingy ORDER BY listing_id").fetchall():
        if name in skip:
            continue
        digital = shop == "DemoPrintables"
        pid = con.execute("INSERT INTO produkty (sku, typ, nazev, popis, jazyk, vytvoreno_ts, zmeneno_ts) VALUES (?,?,?,?,?,?,?)",
                          (sku, "digital" if digital else "physical", name, desc, "en", now, now)).lastrowid
        con.execute("INSERT INTO kanal_data (produkt_id, rozsah, jazyk, stitky, cena, mena, zmeneno_ts) VALUES (?,?,?,?,?,?,?)",
                    (pid, accounts[shop], "en", json.dumps([t.strip() for t in tags.split(",")]), price, cur, now))
        con.execute("INSERT INTO nabidky (produkt_id, ucet_id, externi_id, stav, odeslano, zmeneno_v_kanalu_ts, vytvoreno_ts) VALUES (?,?,?,?,?,?,?)",
                    (pid, accounts[shop], str(lid), state, "{}", now, now))
        if digital:
            con.execute("INSERT INTO produkt_media (produkt_id, druh, nazev, velikost, poradi, zdroj) VALUES (?,?,?,?,?,?)",
                        (pid, "soubor", name.replace(" ", "-") + ".pdf", 240000, 1, "etsy"))
        elif name == "Ceramic Mug":
            for n, (color, vsku) in enumerate((("White", "MUG-W"), ("Sage", "MUG-S"), ("Terracotta", "MUG-T")), 1):
                con.execute("INSERT INTO produkt_varianty (produkt_id, sku, vlastnosti, aktivni, poradi) VALUES (?,?,?,?,?)",
                            (pid, vsku, json.dumps({"Color": color}), 1, n))
    pid = con.execute("INSERT INTO produkty (sku, typ, nazev, popis, slozka, jazyk, vytvoreno_ts, zmeneno_ts) VALUES (?,?,?,?,?,?,?,?)",
                      ("HALLOWEEN-ACTIVITY-PACK", "digital", "Halloween Activity Pack", "Printable Halloween activities for kids.",
                       "halloween-activity-pack", "en", now, now)).lastrowid
    con.execute("INSERT INTO kanal_data (produkt_id, rozsah, jazyk, stitky, cena, mena, zmeneno_ts) VALUES (?,?,?,?,?,?,?)",
                (pid, "etsy", "en", json.dumps(["halloween", "kids activity", "printable"]), 3.99, "USD", now))
    for name in ("Halloween-Activity-Pack_US-Letter.pdf", "Halloween-Activity-Pack_A4.pdf"):
        con.execute("INSERT INTO produkt_media (produkt_id, druh, nazev, velikost, poradi, zdroj) VALUES (?,?,?,?,?,?)",
                    (pid, "soubor", name, 190000, 1, "slozka"))
    # řádky objednávek jako z Etsy (listing a SKU), aby katalog ukazoval prodané kusy
    lids = {(r[0], r[1]): (r[2], r[3]) for r in con.execute("SELECT shop, nazev, listing_id, sku FROM listingy")}
    for shop, rid, items, total, cur in con.execute("SELECT shop, receipt_id, polozky, celkem, mena FROM objednavky").fetchall():
        q, name = items.split("x ", 1)
        lid, sku = lids.get((shop, name), ("", ""))
        con.execute("INSERT INTO obj_polozky VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (accounts[shop], rid, f"{rid}1", str(lid), sku, name, int(q), total, cur, "", ""))
