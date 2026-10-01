"""Import CSV souborů stažených z Etsy."""

import csv
import hashlib
import html
import io
import re as _re
import time
from datetime import datetime

from zaklad import AppError
from databaze import db


# ------------------------------------------------------------- import CSV z Etsy

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
