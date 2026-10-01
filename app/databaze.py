"""Lokální databáze SQLite: schéma a pomocné funkce."""

import os
import shutil
import sqlite3
import threading
import time

from zaklad import DATA_DIR, DB_PATH

SCHEMA = 1  # PRAGMA user_version: 1 = katalog produktů (verze 1.24)
_MIGRACE = threading.Lock()
_ZKONTROLOVANO = set()


# --------------------------------------------------------------- databáze

def db(path=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = path or DB_PATH
    with _MIGRACE:
        if path not in _ZKONTROLOVANO and os.path.exists(path) and os.path.getsize(path):
            con = sqlite3.connect(path)
            old = con.execute("PRAGMA user_version").fetchone()[0]
            con.close()
            backup = path[:-3] + f"-zaloha-schema{old}.db"
            if old < SCHEMA and not os.path.exists(backup):  # před první změnou schématu kopie databáze
                shutil.copy2(path, backup)
        _ZKONTROLOVANO.add(path)
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS objednavky (
        shop TEXT, receipt_id INTEGER PRIMARY KEY, vytvoreno_ts INTEGER, zakaznik TEXT,
        polozky TEXT, celkem REAL, mena TEXT, zaplaceno INTEGER, odeslano INTEGER,
        stav TEXT, zmeneno_ts INTEGER, pridano_ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS vypis (
        shop TEXT, entry_id INTEGER PRIMARY KEY, datum_ts INTEGER, typ TEXT, popis TEXT,
        castka REAL, mena TEXT, zustatek REAL, reference TEXT, pridano_ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS listingy (
        shop TEXT, listing_id INTEGER PRIMARY KEY, nazev TEXT, stav TEXT, cena REAL, mena TEXT,
        mnozstvi INTEGER, zobrazeni INTEGER, oblibene INTEGER, stitky TEXT, obrazek TEXT, url TEXT,
        sku TEXT, vytvoreno_ts INTEGER, zmeneno_ts INTEGER, pridano_ts INTEGER, popis TEXT)""")
    if "popis" not in {r[1] for r in con.execute("PRAGMA table_info(listingy)")}:  # tabulka z verze 1.10
        con.execute("ALTER TABLE listingy ADD COLUMN popis TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS doprava (
        receipt_id INTEGER PRIMARY KEY, dopravce TEXT, cislo TEXT, cena REAL, mena TEXT,
        zmeneno_ts INTEGER)""")  # ruční údaje, import ani synchronizace je nepřepíšou
    con.execute("""CREATE TABLE IF NOT EXISTS slevy (
        id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT, listing_id INTEGER, procento REAL, od_ts INTEGER,
        do_ts INTEGER, stav TEXT, puvodni TEXT, nove TEXT, chyba TEXT, vytvoreno_ts INTEGER)""")
    # statistiky: Etsy API dává jen celkové počty zobrazení a oblíbených, proto se ukládají denní stavy
    con.execute("""CREATE TABLE IF NOT EXISTS stat_listingy (
        datum TEXT, listing_id INTEGER, shop TEXT, zobrazeni INTEGER, oblibene INTEGER, PRIMARY KEY (datum, listing_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS stat_shop (
        datum TEXT, shop TEXT, sledujici INTEGER, PRIMARY KEY (datum, shop))""")
    con.execute("""CREATE TABLE IF NOT EXISTS recenze (
        id TEXT PRIMARY KEY, shop TEXT, listing_id INTEGER, hodnoceni INTEGER, text TEXT, ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS obj_info (
        receipt_id INTEGER PRIMARY KEY, kupujici TEXT, mesto TEXT, zeme TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS csv_polozky (
        receipt_id INTEGER PRIMARY KEY, polozky TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS stav (
        shop_id TEXT, co TEXT, posledni_ts INTEGER, PRIMARY KEY (shop_id, co))""")
    # katalog: produkt je jednou, nad ním vrstvy obsahu pro kanály a nabídky v jednotlivých účtech
    con.execute("""CREATE TABLE IF NOT EXISTS kanal_ucty (
        id TEXT PRIMARY KEY, kanal TEXT, nazev TEXT, mena TEXT, jazyk TEXT, pravidla TEXT DEFAULT '{}', aktivni INTEGER DEFAULT 1)""")
    con.execute("""CREATE TABLE IF NOT EXISTS produkty (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT UNIQUE, typ TEXT, nazev TEXT, popis TEXT, zakladni_cena REAL,
        hmotnost_g REAL, delka_mm REAL, sirka_mm REAL, vyska_mm REAL, vyroba TEXT DEFAULT 'objednavka', kategorie TEXT,
        slozka TEXT, jazyk TEXT, vytvoreno_ts INTEGER, zmeneno_ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS produkt_varianty (
        id INTEGER PRIMARY KEY AUTOINCREMENT, produkt_id INTEGER, sku TEXT, vlastnosti TEXT, cena_rozdil REAL,
        sklad INTEGER, aktivni INTEGER DEFAULT 1, poradi INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS produkt_media (
        id INTEGER PRIMARY KEY AUTOINCREMENT, produkt_id INTEGER, druh TEXT, nazev TEXT, cesta TEXT, velikost INTEGER,
        hash TEXT, poradi INTEGER, zdroj TEXT, alt TEXT, varianta_sku TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS kanal_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, produkt_id INTEGER, rozsah TEXT, jazyk TEXT, nazev TEXT, popis TEXT,
        stitky TEXT, atributy TEXT, kategorie_id INTEGER, cena REAL, mena TEXT, extra TEXT, zmeneno_ts INTEGER,
        UNIQUE (produkt_id, rozsah, jazyk))""")
    con.execute("""CREATE TABLE IF NOT EXISTS nabidky (
        id INTEGER PRIMARY KEY AUTOINCREMENT, produkt_id INTEGER, ucet_id TEXT, externi_id TEXT, stav TEXT,
        prepisy TEXT DEFAULT '{}', odeslano TEXT, hash TEXT, zmeneno_v_kanalu_ts INTEGER, posledni_chyba TEXT,
        vytvoreno_ts INTEGER, UNIQUE (ucet_id, externi_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS obj_polozky (
        ucet_id TEXT, objednavka_id INTEGER, externi_radek_id TEXT, externi_nabidka_id TEXT, sku TEXT, nazev TEXT,
        mnozstvi INTEGER, cena REAL, mena TEXT, varianta TEXT, personalizace TEXT, PRIMARY KEY (ucet_id, externi_radek_id))""")
    if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA:
        con.execute(f"PRAGMA user_version = {SCHEMA}")
    return con


def last_ts(con, shop_id, what):
    row = con.execute("SELECT posledni_ts FROM stav WHERE shop_id=? AND co=?", (shop_id, what)).fetchone()
    return row[0] if row else None


def set_last_ts(con, shop_id, what, ts):
    con.execute("INSERT OR REPLACE INTO stav VALUES (?,?,?)", (shop_id, what, ts))


def save_account(con, ucet_id, kanal, nazev, mena=None, jazyk=None):
    """Prodejní účet (Etsy shop, později e-shop…). Pravidla a ručně změněné údaje se nepřepisují."""
    con.execute("INSERT OR IGNORE INTO kanal_ucty (id, kanal, nazev, mena, jazyk) VALUES (?,?,?,?,?)",
                (ucet_id, kanal, nazev, mena or "", jazyk or ""))
    con.execute("UPDATE kanal_ucty SET nazev=? WHERE id=?", (nazev, ucet_id))
    if mena:
        con.execute("UPDATE kanal_ucty SET mena=? WHERE id=?", (mena, ucet_id))


def today():
    return time.strftime("%Y-%m-%d")


def rows_as_dicts(con, sql):
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
