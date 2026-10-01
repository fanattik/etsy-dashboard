"""Objednávky (fáze 4): vlastní stavy uživatele a jejich pravidla, ruční změna stavu, odeslání trackingu
do kanálu (Etsy createReceiptShipment) a odečet skladu u produktů v režimu „ze skladu“."""

import json
import time

from zaklad import AppError, load_tokens, LOCK, tr
from databaze import db, last_ts, rows_as_dicts, set_last_ts
from etsy_api import api_send, can_ship
from kanal_api import order_numbers, order_ref, ship as api_ship

# Výchozí sada pro novou instalaci. Jde přejmenovat, doplnit nebo smazat v Nastavení.
DEFAULT_STATES = {
    "cs": ["Nová", "Ve výrobě", "Připraveno", "Odesláno"],
    "en": ["New", "In production", "Ready", "Shipped"],
    "de": ["Neu", "In Produktion", "Bereit", "Versandt"],
}
DEFAULT_RULES = [
    ("#8a8f98", 0, {}, {}),
    ("#d9932b", 0, {"kdyz": ["zaplaceno"], "vse": True}, {}),
    ("#3b7dd8", 0, {}, {}),
    ("#2f9e6b", 1, {"kdyz": ["odeslano"], "vse": True}, {"tracking": True}),
]
CONDITIONS = ("zaplaceno", "odeslano", "tracking", "personalizace", "digital")


def _jl(raw, default):
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def ensure_states(con, lang="cs"):
    """Při prvním spuštění založí výchozí stavy. Když je uživatel později všechny smaže, znovu se nezaloží."""
    if last_ts(con, "", "stavy") is not None:
        return
    if not con.execute("SELECT 1 FROM stavy_objednavek").fetchone():
        names = DEFAULT_STATES.get(lang, DEFAULT_STATES["en"])
        for i, (name, (color, final, rule, action)) in enumerate(zip(names, DEFAULT_RULES), 1):
            con.execute("INSERT INTO stavy_objednavek (nazev, barva, poradi, koncovy, pravidlo, akce) VALUES (?,?,?,?,?,?)",
                        (name, color, i, final, json.dumps(rule), json.dumps(action)))
    set_last_ts(con, "", "stavy", int(time.time()))


def states(con):
    out = rows_as_dicts(con, "SELECT * FROM stavy_objednavek ORDER BY poradi, id")
    for s in out:
        s["pravidlo"], s["akce"], s["koncovy"] = _jl(s["pravidlo"], {}), _jl(s["akce"], {}), bool(s["koncovy"])
    return out


def _facts(con, rids=None):
    """Co o objednávce víme pro pravidla: zaplaceno, odesláno v kanálu, tracking, personalizace, digitální, účet."""
    where = f" WHERE o.receipt_id IN ({','.join('?' * len(rids))})" if rids else ""
    rows = con.execute("SELECT o.receipt_id, o.shop, o.zaplaceno, o.odeslano, o.zmeneno_ts, o.pridano_ts, d.cislo "
                       "FROM objednavky o LEFT JOIN doprava d ON d.receipt_id=o.receipt_id" + where, list(rids or []))
    facts = {r[0]: {"shop": r[1], "zaplaceno": bool(r[2]), "odeslano": bool(r[3]), "zmeneno_ts": r[4] or 0,
                    "pridano_ts": r[5] or 0, "tracking": bool((r[6] or "").strip()), "personalizace": False,
                    "digital": None, "ucet": ""} for r in rows}
    types = {r[0]: r[1] for r in con.execute("SELECT n.ucet_id || '|' || n.externi_id, p.typ FROM nabidky n "
                                              "JOIN produkty p ON p.id=n.produkt_id")}
    for ucet, rid, lid, perso in con.execute("SELECT ucet_id, objednavka_id, externi_nabidka_id, personalizace FROM obj_polozky"):
        f = facts.get(rid)
        if not f:
            continue
        f["ucet"] = ucet
        f["personalizace"] = f["personalizace"] or bool((perso or "").strip())
        typ = types.get(f"{ucet}|{lid}")
        if typ:  # digitální je objednávka, kde jsou jen digitální produkty
            f["digital"] = (f["digital"] is not False) and typ == "digital"
    return facts


def _matches(rule, f):
    conds = [c for c in rule.get("kdyz") or [] if c in CONDITIONS]
    checks = [bool(f.get(c)) for c in conds]
    if rule.get("ucet"):
        checks.append(f["ucet"] == rule["ucet"])
    if not checks:
        return False
    return all(checks) if rule.get("vse", True) else any(checks)


def apply_rules(con, cfg=None, rids=None, only_missing=False):
    """Posune objednávky podle pravidel stavů: nová objednávka dostane první stav, pak se posouvá jen dopředu
    do nejvyššího stavu, jehož pravidlo platí. Koncový stav se nemění. Po ruční změně čeká pravidlo na nová
    data z kanálu. Vrací texty upozornění pro stavy, které je mají zapnuté."""
    st = states(con)
    if not st:
        return []
    by_id = {s["id"]: s for s in st}
    cur = {r[0]: (r[1], r[2] or 0, r[3]) for r in con.execute("SELECT receipt_id, stav_id, zmeneno_ts, rucne FROM obj_stav")}
    if only_missing:
        missing = [r for (r,) in con.execute("SELECT receipt_id FROM objednavky")
                   if r not in cur or cur[r][0] not in by_id]
        if not missing:
            return []
        rids = missing
    now, news = int(time.time()), []
    for rid, f in _facts(con, rids).items():
        old = cur.get(rid)
        state = by_id.get(old[0]) if old else None
        if state and (state["koncovy"] or (old[2] and f["zmeneno_ts"] <= old[1])):
            continue
        target = state or st[0]
        for s in st:
            if s["poradi"] > target["poradi"] and _matches(s["pravidlo"], f):
                target = s
        if state and target["id"] == state["id"]:
            continue
        con.execute("INSERT INTO obj_stav (receipt_id, stav_id, zmeneno_ts, rucne) VALUES (?,?,?,0) "
                    "ON CONFLICT(receipt_id) DO UPDATE SET stav_id=excluded.stav_id, zmeneno_ts=excluded.zmeneno_ts, rucne=0",
                    (rid, target["id"], now))
        if cfg and target["akce"].get("ntfy") and (state or f["pridano_ts"]):  # staré objednávky při prvním zařazení mlčí
            news.append(tr(cfg, "state", shop=f["shop"], id=rid, state=target["nazev"]))
    return news


def save_states(body, db_path=None, demo=False):
    """Uloží celý seznam stavů v daném pořadí. Stavy, které v seznamu nejsou, se smažou."""
    items = body.get("stavy")
    if not isinstance(items, list) or not items:
        raise AppError("states_empty", "Nech aspoň jeden stav.")
    clean = []
    for i, s in enumerate(items, 1):
        name = str(s.get("nazev") or "").strip()[:60]
        if not name:
            raise AppError("states_name", "Každý stav musí mít název.")
        rule = s.get("pravidlo") or {}
        rule = {"kdyz": [c for c in rule.get("kdyz") or [] if c in CONDITIONS], "vse": bool(rule.get("vse", True)),
                "ucet": str(rule.get("ucet") or "")}
        action = {k: bool((s.get("akce") or {}).get(k)) for k in ("tracking", "ntfy")}
        clean.append((s.get("id"), name, str(s.get("barva") or "#8a8f98")[:9], i, int(bool(s.get("koncovy"))),
                      json.dumps(rule), json.dumps(action)))
    with LOCK:
        con = db(db_path)
        try:
            keep = set()
            for sid, *vals in clean:
                if sid and con.execute("SELECT 1 FROM stavy_objednavek WHERE id=?", (sid,)).fetchone():
                    con.execute("UPDATE stavy_objednavek SET nazev=?, barva=?, poradi=?, koncovy=?, pravidlo=?, akce=? WHERE id=?",
                                (*vals, sid))
                    keep.add(sid)
                else:
                    keep.add(con.execute("INSERT INTO stavy_objednavek (nazev, barva, poradi, koncovy, pravidlo, akce) "
                                         "VALUES (?,?,?,?,?,?)", vals).lastrowid)
            gone = [r for (r,) in con.execute("SELECT id FROM stavy_objednavek") if r not in keep]
            for sid in gone:
                con.execute("DELETE FROM stavy_objednavek WHERE id=?", (sid,))
            set_last_ts(con, "", "stavy", int(time.time()))
            apply_rules(con, only_missing=True)  # objednávky ze smazaného stavu dostanou nový
            con.commit()
        finally:
            con.close()
    return {"ok": True}


def _shop_of(con, tokens, rid):
    row = con.execute("SELECT ucet_id FROM obj_polozky WHERE objednavka_id=? AND ucet_id LIKE 'etsy:%' LIMIT 1", (rid,)).fetchone()
    if row:
        return row[0].split(":", 1)[1]
    name = (con.execute("SELECT shop FROM objednavky WHERE receipt_id=?", (rid,)).fetchone() or [""])[0]
    return next((k for k, v in tokens.items() if v.get("shop_name") == name), None)


def push_tracking(cfg, rid, db_path=None):
    """Pošle dopravce a číslo zásilky na Etsy. Etsy objednávku označí jako odeslanou a napíše zákazníkovi."""
    rid = int(rid)
    con = db(db_path)
    try:
        ship = con.execute("SELECT dopravce, cislo FROM doprava WHERE receipt_id=?", (rid,)).fetchone()
        tokens = load_tokens()
        ref = order_ref(con, rid)  # objednávka z e-shopu přes Vlastní API
        shop_id = None if ref else _shop_of(con, tokens, rid)
    finally:
        con.close()
    if not ship or not (ship[1] or "").strip():
        raise AppError("track_missing", "Objednávka nemá vyplněné číslo zásilky.")
    if ref:
        return _record_tracking(cfg, rid, db_path, lambda: _api_ship(ref, ship, db_path), "api")
    if not shop_id or shop_id not in tokens:
        raise AppError("listing_no_shop", "Tahle shopa není připojená přes Etsy API.")
    if not can_ship(tokens[shop_id]):
        raise AppError("track_relogin", "Shopa je připojená bez oprávnění odesílat zásilky. Přihlas ji v Nastavení znovu.")
    data = {"tracking_code": ship[1].strip(), "send_bcc": False}
    if (ship[0] or "").strip():
        data["carrier_name"] = ship[0].strip()
    return _record_tracking(cfg, rid, db_path, lambda: api_send(cfg, tokens, shop_id, "POST", f"/shops/{shop_id}/receipts/{rid}/tracking", data))


def _api_ship(ref, ship, db_path):
    con = db(db_path)
    try:
        api_ship(con, ref[0], ref[1], (ship[0] or "").strip(), ship[1].strip())
    finally:
        con.close()


def _record_tracking(cfg, rid, db_path, send, kanal="etsy"):
    """Odešle tracking do kanálu a zapíše výsledek: úspěch označí objednávku jako odeslanou, chyba zůstane u objednávky."""
    err = None
    try:
        send()
    except Exception as e:
        err = str(e)
    now = int(time.time())
    with LOCK:
        con = db(db_path)
        try:
            con.execute("INSERT OR IGNORE INTO obj_stav (receipt_id) VALUES (?)", (rid,))
            con.execute("UPDATE obj_stav SET tracking_ts=?, tracking_chyba=? WHERE receipt_id=?",
                        (None if err else now, err, rid))
            if not err:
                con.execute("UPDATE objednavky SET odeslano=1 WHERE receipt_id=?", (rid,))
                con.execute("UPDATE obj_stav SET rucne=0 WHERE receipt_id=?", (rid,))  # odeslání je nový fakt pro pravidla
                apply_rules(con, cfg, [rid])
            con.commit()
        finally:
            con.close()
    if err:
        if kanal == "api":
            raise AppError("track_failed_api", f"E-shop tracking nepřijal: {err}", e=err)
        raise AppError("track_failed", f"Etsy tracking nepřijala: {err}", e=err)
    return {"ok": True}


def set_state(cfg, body, db_path=None, demo=False):
    """Ruční změna stavu jedné nebo více objednávek. Má-li cílový stav akci „poslat tracking“, pošle ho
    u objednávek, které mají číslo zásilky a v kanálu ještě odeslané nejsou."""
    rids = [int(r) for r in body.get("receipt_ids") or []]
    sid = body.get("stav_id")
    with LOCK:
        con = db(db_path)
        try:
            target = next((s for s in states(con) if s["id"] == sid), None)
            if not target or not rids:
                raise AppError("state_missing", "Zvolený stav už neexistuje.")
            now = int(time.time())
            for rid in rids:
                con.execute("INSERT INTO obj_stav (receipt_id, stav_id, zmeneno_ts, rucne) VALUES (?,?,?,1) ON CONFLICT(receipt_id) "
                            "DO UPDATE SET stav_id=excluded.stav_id, zmeneno_ts=excluded.zmeneno_ts, rucne=1", (rid, sid, now))
            con.commit()
            facts = _facts(con, rids) if target["akce"].get("tracking") and not demo else {}
        finally:
            con.close()
    sent, errors = 0, []
    for rid, f in facts.items():
        if f["tracking"] and not f["odeslano"]:
            try:
                push_tracking(cfg, rid, db_path)
                sent += 1
            except Exception as e:
                errors.append(f"#{rid}: {e}")
    return {"ok": True, "odeslano": sent, "chyby": errors}


def apply_stock(con):
    """Režim „ze skladu“: po prodeji sníží sklad varianty (jednou za řádek objednávky). Prodeje stažené dřív,
    než sklad začal platit, se jen označí. Do kanálů se nic neposílá, nabídka ukáže změnu skladu."""
    init = last_ts(con, "", "sklad") is None
    now = int(time.time())
    done = {(r[0], r[1]) for r in con.execute("SELECT ucet_id, externi_radek_id FROM sklad_pohyby")}
    offer = {(r[0], r[1]): r[2] for r in con.execute("SELECT ucet_id, externi_id, produkt_id FROM nabidky")}
    mode = {r[0]: r[1] for r in con.execute("SELECT id, vyroba FROM produkty")}
    variants = {}
    for vid, pid, sku, props, stock in con.execute("SELECT id, produkt_id, sku, vlastnosti, sklad FROM produkt_varianty WHERE aktivni=1"):
        variants.setdefault(pid, []).append((vid, sku or "", _jl(props, {}), stock))
    sku_pid = {v[1]: pid for pid, vs in variants.items() for v in vs if v[1]}
    for ucet, line, lid, sku, qty, var in con.execute(
            "SELECT ucet_id, externi_radek_id, externi_nabidka_id, sku, mnozstvi, varianta FROM obj_polozky").fetchall():
        if (ucet, line) in done:
            continue
        vid = None
        pid = offer.get((ucet, lid)) or sku_pid.get(sku)
        if not init and pid and mode.get(pid) == "sklad":
            vs = variants.get(pid) or []
            hit = [v for v in vs if sku and v[1] == sku] or \
                  [v for v in vs if v[2] and all(f"{k}: {val}" in (var or "") for k, val in v[2].items())] or \
                  (vs if len(vs) == 1 else [])
            if hit and hit[0][3] is not None:
                vid = hit[0][0]
                con.execute("UPDATE produkt_varianty SET sklad=MAX(0, sklad-?) WHERE id=?", (qty or 1, vid))
        con.execute("INSERT INTO sklad_pohyby VALUES (?,?,?,?,?)", (ucet, line, vid, -(qty or 1) if vid else 0, now))
    if init:
        set_last_ts(con, "", "sklad", now)


def after_sync(cfg, db_path=None):
    """Po stažení dat: sklad a stavy podle pravidel. Vrací upozornění."""
    con = db(db_path)
    try:
        ensure_states(con, cfg.get("jazyk") or "cs")
        apply_stock(con)
        news = apply_rules(con, cfg)
        con.commit()
    finally:
        con.close()
    return news


def orders_data(con, lang="cs"):
    """Stavy, lokální stav objednávek a řádky objednávek s produktem z katalogu (pro stránku Objednávky)."""
    ensure_states(con, lang)
    apply_rules(con, only_missing=True)
    con.commit()
    offer = {(r[0], r[1]): r[2] for r in con.execute("SELECT ucet_id, externi_id, produkt_id FROM nabidky")}
    sku_pid = {r[0]: r[1] for r in con.execute("SELECT sku, produkt_id FROM produkt_varianty WHERE sku<>''")}
    sku_pid.update({r[0]: r[1] for r in con.execute("SELECT sku, id FROM produkty WHERE sku<>''")})
    items = []
    for ucet, rid, lid, sku, name, qty, var, perso in con.execute(
            "SELECT ucet_id, objednavka_id, externi_nabidka_id, sku, nazev, mnozstvi, varianta, personalizace FROM obj_polozky"):
        items.append({"receipt_id": rid, "nazev": name, "sku": sku, "mnozstvi": qty, "varianta": var or "",
                      "personalizace": perso or "", "produkt_id": offer.get((ucet, lid)) or sku_pid.get(sku)})
    return {"stavy": states(con),
            "obj_stav": {r[0]: {"stav_id": r[1], "zmeneno_ts": r[2], "rucne": bool(r[3]), "tracking_ts": r[4], "tracking_chyba": r[5]}
                         for r in con.execute("SELECT receipt_id, stav_id, zmeneno_ts, rucne, tracking_ts, tracking_chyba FROM obj_stav")},
            "polozky": items, "cisla": order_numbers(con)}
