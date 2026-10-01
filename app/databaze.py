"""Lokální databáze SQLite: schéma a pomocné funkce."""

import os
import sqlite3
import time

from zaklad import DATA_DIR, DB_PATH


# --------------------------------------------------------------- databáze

def db(path=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(path or DB_PATH)
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
    return con


def last_ts(con, shop_id, what):
    row = con.execute("SELECT posledni_ts FROM stav WHERE shop_id=? AND co=?", (shop_id, what)).fetchone()
    return row[0] if row else None


def set_last_ts(con, shop_id, what, ts):
    con.execute("INSERT OR REPLACE INTO stav VALUES (?,?,?)", (shop_id, what, ts))


def today():
    return time.strftime("%Y-%m-%d")


def rows_as_dicts(con, sql):
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
