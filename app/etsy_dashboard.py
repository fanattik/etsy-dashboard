#!/usr/bin/env python3
"""Etsy Dashboard: webová aplikace s dashboardem, která sleduje nové objednávky
a pohyby na platebním účtu (měsíční výpis) pro jednu nebo více Etsy shop
přes Etsy Open API v3. Běží lokálně u tebe v počítači.

Jen standardní knihovna Pythonu (3.9+), nic se nemusí instalovat.

Příkazy:
    python etsy_dashboard.py           # spustí aplikaci a otevře ji v prohlížeči
    python etsy_dashboard.py web --sluzba  # běh na pozadí bez otevírání prohlížeče (Mac: LaunchAgent)
    python etsy_dashboard.py demo      # ukázková data bez Etsy (na portu 8766)
    python etsy_dashboard.py jednou    # jedna kontrola bez prohlížeče (Plánovač úloh / cron)
"""

import json
import os
import shutil
import subprocess
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import zaklad
from zaklad import (
    BASE_DIR, CONFIG_PATH, config_ready, DASHBOARD_PATH, DATA_DIR, http_json, load_config, load_tokens, LOCK,
    save_config, save_tokens, SSL_CTX, STATUS, tr, UPDATE_BASE, UPDATE_EVERY)
from databaze import db
from etsy_api import auth_finish, auth_start, can_delete, can_ship, can_write
from synchronizace import check_shop, get_rates
from etsy_listingy import (
    add_discount, cancel_discount, listing_detail, listing_options, listing_properties, listings_state,
    process_discounts, save_listing)
from prehled import csv_export, dashboard_data, make_demo_db, save_shipping
from csv_import import fill_items_from_ledger, import_csv
from katalog import adopt_listing, adoption_proposal, catalog_data, import_folder, media_file
from objednavky import after_sync, push_tracking, save_states, set_state
from nabidky import link_offer, offer_data, readopt_offer, unlink_offer
from produkty import delete_product, media_action, save_layer, save_product, save_rules, save_variants


PORT = 8765
VERSION = "1.27"
zaklad.VERSION = VERSION  # User-Agent v HTTP požadavcích


def run_check(cfg):
    """Zkontroluje všechny shopy. Vrací seznam novinek."""
    if not config_ready(cfg):
        return []
    with LOCK:
        STATUS["bezi"] = True
        try:
            tokens = load_tokens()
            con = db()
            all_news = []
            for shop_id in list(tokens):
                name = tokens[shop_id].get("shop_name", shop_id)
                try:
                    all_news.extend(check_shop(cfg, tokens, con, shop_id))
                    STATUS["chyby"].pop(name, None)
                except Exception as e:
                    STATUS["chyby"][name] = str(e)
                    print(f"⚠️  {name}: {e}")
            con.close()
            try:
                all_news.extend(after_sync(cfg))
            except Exception as e:
                print(f"⚠️  stavy objednávek: {e}")
            STATUS["posledni_kontrola"] = int(time.time())
        finally:
            STATUS["bezi"] = False
    try:
        process_discounts(cfg)
    except Exception as e:
        print(f"⚠️  slevy: {e}")
    if all_news:
        notify(cfg, all_news)
    return all_news


def notify(cfg, lines):
    for line in lines:
        print(line)
    topic = cfg.get("ntfy_topic")
    if not topic or not lines:
        return
    body = "\n".join(lines[:20]) + ("\n" + tr(cfg, "more", n=len(lines) - 20) if len(lines) > 20 else "")
    req = urllib.request.Request(f"https://ntfy.sh/{urllib.parse.quote(topic)}", data=body.encode("utf-8"),
                                 method="POST", headers={"Title": tr(cfg, "title"), "Tags": "shopping_cart"})
    try:
        urllib.request.urlopen(req, timeout=30, context=SSL_CTX).close()
    except Exception as e:
        print(f"(Nepodařilo se poslat notifikaci na telefon: {e})")


def version_tuple(v):
    return tuple(int(x) for x in str(v).split(".") if x.isdigit())


def restart():
    """Na Macu (služba) stačí skončit, launchd aplikaci hned spustí znovu s novým kódem."""
    time.sleep(1)
    if "--sluzba" in sys.argv:
        os._exit(0)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def check_update(apply=True):
    """Podívá se na GitHub, jestli není novější verze; pokud ano, stáhne ji a restartuje se."""
    STATUS["posledni_aktualizace"] = int(time.time())
    info = http_json("GET", UPDATE_BASE + "version.json?t=" + str(int(time.time())))
    new = str(info.get("verze", ""))
    if version_tuple(new) <= version_tuple(VERSION):
        return {"nova": False, "verze": VERSION}
    if not apply:
        return {"nova": True, "verze": new}
    files = {}
    for name in info.get("soubory", ["etsy_dashboard.py", "dashboard.html"]):
        if "/" in name or name.startswith("."):
            continue
        with urllib.request.urlopen(UPDATE_BASE + name, timeout=60, context=SSL_CTX) as resp:
            files[name] = resp.read()
    if "etsy_dashboard.py" not in files:
        raise RuntimeError("Aktualizace neobsahuje etsy_dashboard.py.")
    for name, data in files.items():  # rozbitý soubor nenahrajeme
        if name.endswith(".py"):
            compile(data, name, "exec")
    for name, data in files.items():
        tmp = os.path.join(BASE_DIR, name + ".novy")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(BASE_DIR, name))
    print(f"Aktualizováno na verzi {new}, restartuji…")
    threading.Thread(target=restart, daemon=True).start()
    return {"nova": True, "verze": new, "nainstalovano": True}


def watcher_loop():
    while True:
        cfg = load_config()
        try:
            run_check(cfg)
        except Exception as e:
            print(f"⚠️  Chyba kontroly: {e}")
        if time.time() - (STATUS.get("posledni_aktualizace") or 0) > UPDATE_EVERY:
            try:
                check_update()
            except Exception as e:
                print(f"(Kontrola aktualizací se nepovedla: {e})")
        time.sleep(max(5, int(cfg.get("interval_minut", 15))) * 60)


# ------------------------------------------------------------------ web server

class Handler(BaseHTTPRequestHandler):
    demo = False
    db_path = None

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def state(self):
        cfg = load_config()
        tokens = load_tokens()
        if self.demo:
            shops = [{"id": "1", "name": "DemoPrintables", "zapis": True, "mazani": True, "expedice": True},
                     {"id": "2", "name": "DemoHandmade", "zapis": True, "mazani": True, "expedice": True}]
        else:
            shops = [{"id": k, "name": v.get("shop_name", k), "zapis": can_write(v), "mazani": can_delete(v),
                      "expedice": can_ship(v)} for k, v in tokens.items()]
            con = db(self.db_path)
            names = {r[0] for r in con.execute("SELECT shop FROM objednavky UNION SELECT shop FROM vypis UNION SELECT shop FROM listingy")}
            con.close()
            shops += [{"id": None, "name": n} for n in sorted(names - {s["name"] for s in shops})]
        settings = {k: cfg.get(k) for k in ("interval_minut", "ntfy_topic", "redirect_uri", "keystring", "jazyk",
                                            "zakladni_mena", "jazyk_katalogu")}
        settings["ma_secret"] = bool(cfg.get("shared_secret"))
        return {
            "demo": self.demo,
            "nastaveno": self.demo or config_ready(cfg),
            "shopy": shops,
            "posledni_kontrola": int(time.time()) if self.demo else STATUS["posledni_kontrola"],
            "bezi": STATUS["bezi"],
            "chyby": STATUS["chyby"],
            "nastaveni": settings,
            "verze": VERSION,
        }

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(DASHBOARD_PATH, encoding="utf-8") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/stav":
            return self.send(200, self.state())
        if path == "/api/data":
            return self.send(200, dashboard_data(self.db_path, load_config().get("jazyk") or "cs"))
        if path == "/api/kurzy":
            return self.send(200, get_rates())
        if path == "/api/katalog":
            cfg = load_config()
            return self.send(200, catalog_data(self.db_path, "USD" if self.demo else cfg.get("zakladni_mena", ""),
                                               cfg.get("jazyk_katalogu") or "en"))
        if path == "/api/katalog/navrh":
            return self.send(200, adoption_proposal(load_config(), self.db_path))
        if path == "/api/katalog/nabidka":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                cfg = dict(load_config(), zakladni_mena="USD") if self.demo else load_config()
                return self.send(200, offer_data(cfg, self.db_path, q.get("ucet", [""])[0], q.get("produkt", ["0"])[0]))
            except Exception as e:
                return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        if path.startswith("/media/"):
            found = media_file(path[len("/media/"):])
            if not found:
                return self.send(404, {"chyba": "nenalezeno"})
            with open(found[0], "rb") as f:
                data = f.read()
            extra = {} if found[1].startswith("image/") else {"Content-Disposition": "attachment"}
            return self.send(200, data, found[1], extra)
        if path == "/api/listing/detail":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self.send(200, listing_detail(load_config(), q.get("shop", [""])[0], q.get("id", ["0"])[0], self.demo))
            except Exception as e:
                return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        if path == "/api/listing/vlastnosti":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                return self.send(200, listing_properties(load_config(), q.get("shop", [""])[0], q.get("kategorie", ["0"])[0], self.demo))
            except Exception as e:
                return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        if path == "/api/listing/moznosti":
            shop = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("shop", [""])[0]
            try:
                return self.send(200, listing_options(load_config(), shop, self.demo))
            except Exception as e:
                return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        if path in ("/export/objednavky.csv", "/export/vypis.csv", "/export/listingy.csv"):
            kind = path.split("/")[-1].split(".")[0]
            lang = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("lang", ["cs"])[0]
            fname = {"en": {"objednavky": "orders", "vypis": "statement", "listingy": "listings"},
                     "de": {"objednavky": "bestellungen", "vypis": "kontoauszug", "listingy": "angebote"}}.get(lang, {}).get(kind, kind)
            return self.send(200, csv_export(kind, self.db_path, lang), "text/csv; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="{fname}.csv"'})
        self.send(404, {"chyba": "nenalezeno"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send(403, {"chyba": "zakázáno"})
        try:
            body = self.read_json()
            if self.demo and path not in ("/api/zkontrolovat", "/api/doprava", "/api/objednavka/stav", "/api/stavy"):
                return self.send(400, {"chyba": "V ukázkovém režimu nejde nic měnit.", "kod": "demo"})
            if path == "/api/nastaveni":
                cfg = load_config()
                for k in ("keystring", "redirect_uri", "ntfy_topic", "jazyk", "jazyk_katalogu"):
                    if k in body:
                        cfg[k] = str(body[k]).strip()
                if body.get("shared_secret"):
                    cfg["shared_secret"] = str(body["shared_secret"]).strip()
                if "zakladni_mena" in body:
                    cfg["zakladni_mena"] = str(body["zakladni_mena"]).strip().upper()[:3]
                if body.get("interval_minut"):
                    cfg["interval_minut"] = max(5, int(body["interval_minut"]))
                save_config(cfg)
                return self.send(200, {"ok": True})
            if path == "/api/prihlasit/start":
                cfg = load_config()
                if not config_ready(cfg):
                    return self.send(400, {"chyba": "Nejdřív vyplň Keystring a Shared secret v Nastavení.",
                                           "kod": "need_keys"})
                return self.send(200, {"url": auth_start(cfg)})
            if path == "/api/prihlasit/dokoncit":
                name = auth_finish(load_config(), body.get("url", ""))
                threading.Thread(target=run_check, args=(load_config(),), daemon=True).start()
                return self.send(200, {"ok": True, "shop": name})
            if path == "/api/odebrat":
                with LOCK:
                    tokens = load_tokens()
                    tokens.pop(str(body.get("id")), None)
                    save_tokens(tokens)
                return self.send(200, {"ok": True})
            if path == "/api/aktualizace":
                return self.send(200, check_update())
            if path == "/api/listing/vytvorit":
                result = save_listing(load_config(), body)
                link_offer(body, result, self.db_path)
                return self.send(200, result)
            if path == "/api/nabidka/prevzit":
                return self.send(200, readopt_offer(load_config(), body, self.db_path, self.demo))
            if path == "/api/nabidka/odpojit":
                return self.send(200, unlink_offer(body, self.db_path, self.demo))
            if path == "/api/listing/stav":
                return self.send(200, listings_state(load_config(), body))
            if path == "/api/sleva":
                return self.send(200, add_discount(load_config(), body, self.db_path))
            if path == "/api/sleva/zrusit":
                return self.send(200, cancel_discount(load_config(), body, self.db_path))
            if path == "/api/katalog/prevzit":
                return self.send(200, adopt_listing(load_config(), body, self.db_path, self.demo))
            katalog_post = {"/api/produkt/ulozit": save_product, "/api/produkt/smazat": delete_product,
                            "/api/produkt/varianty": save_variants, "/api/produkt/media": media_action,
                            "/api/produkt/vrstva": save_layer, "/api/ucet/pravidla": save_rules}
            if path in katalog_post:
                return self.send(200, katalog_post[path](body, self.db_path, self.demo))
            if path == "/api/katalog/slozka":
                return self.send(200, import_folder(body, self.db_path, self.demo))
            if path == "/api/objednavka/stav":
                return self.send(200, set_state(load_config(), body, self.db_path, self.demo))
            if path == "/api/objednavka/tracking":
                return self.send(200, push_tracking(load_config(), body.get("receipt_id"), self.db_path))
            if path == "/api/stavy":
                return self.send(200, save_states(body, self.db_path, self.demo))
            if path == "/api/doprava":
                return self.send(200, save_shipping(body, self.db_path))
            if path == "/api/import":
                return self.send(200, import_csv(body.get("shop"), body.get("soubor", ""),
                                                 body.get("obsah", ""), self.db_path))
            if path == "/api/odinstalovat":
                uninstall(bool(body.get("smazat_data")))
                return self.send(200, {"ok": True})
            if path == "/api/zkontrolovat":
                news = [] if self.demo else run_check(load_config())
                return self.send(200, {"ok": True, "novinky": news, "chyby": STATUS["chyby"]})
        except Exception as e:
            return self.send(400, {"chyba": str(e), "kod": getattr(e, "kod", None), "param": getattr(e, "param", {})})
        self.send(404, {"chyba": "nenalezeno"})


LAUNCH_LABEL = "io.github.fanattik.etsy-dashboard"


def uninstall(delete_data):
    """Vypne běh na pozadí (Mac), smaže zástupce a volitelně data, pak aplikaci ukončí."""
    home = os.path.expanduser("~")
    plist = os.path.join(home, "Library", "LaunchAgents", LAUNCH_LABEL + ".plist")
    for f in (plist, os.path.join(home, "Desktop", "Etsy Dashboard.webloc")):
        if os.path.exists(f):
            os.remove(f)
    if delete_data:
        installed = os.path.basename(BASE_DIR) == "EtsyDashboard" and "Application Support" in BASE_DIR
        if installed:
            shutil.rmtree(BASE_DIR, ignore_errors=True)
        else:
            shutil.rmtree(DATA_DIR, ignore_errors=True)
            if os.path.exists(CONFIG_PATH):
                os.remove(CONFIG_PATH)

    def stop():
        if sys.platform == "darwin":
            # samostatný proces, aby ho launchd neukončil spolu s aplikací
            subprocess.Popen(["/bin/sh", "-c", f"sleep 1; launchctl bootout gui/{os.getuid()}/{LAUNCH_LABEL} "
                                               f"|| launchctl remove {LAUNCH_LABEL}"],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        os._exit(0)

    threading.Thread(target=stop, daemon=True).start()


def serve(demo=False, open_browser=True):
    global PORT
    Handler.demo = demo
    if demo:
        PORT = 8766
        Handler.db_path = os.path.join(DATA_DIR, "demo.db")
        make_demo_db(Handler.db_path)
    url = f"http://127.0.0.1:{PORT}/"
    with socket.socket() as probe:
        busy = probe.connect_ex(("127.0.0.1", PORT)) == 0
    if busy:  # už běží (např. na pozadí), stačí otevřít prohlížeč
        print(f"Etsy Dashboard už běží na {url}")
        if open_browser:
            webbrowser.open(url)
        return
    if not demo:
        con = db()  # doplní názvy produktů i do dříve nahraných objednávek
        fill_items_from_ledger(con)
        con.commit()
        con.close()
        threading.Thread(target=watcher_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Etsy Dashboard běží na {url}  (ukončíš Ctrl+C nebo zavřením okna)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "web"
    if cmd == "web":
        serve(open_browser="--sluzba" not in sys.argv)
    elif cmd == "demo":
        serve(demo=True, open_browser="--bez-prohlizece" not in sys.argv)
    elif cmd == "jednou":
        cfg = load_config()
        if not config_ready(cfg):
            sys.exit("Nejdřív spusť aplikaci a vyplň Nastavení.")
        news = run_check(cfg)
        print("\n".join(news) if news else "Nic nového.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
