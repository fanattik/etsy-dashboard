"""Vytváření a úpravy listingů přes Etsy API, personalizace a plánované slevy."""

import base64
import html
import json
import os
import threading
import time
from datetime import datetime, timedelta

from zaklad import AppError, config_ready, DATA_DIR, DISCOUNT_LOCK, load_tokens, LOCK, TAXONOMY_PATH
from databaze import db
from etsy_api import api_get, api_send, can_delete, can_write
from synchronizace import sync_listings


def taxonomy(cfg, tokens, shop_id):
    """Kategorie Etsy (jen koncové, s celou cestou). Mění se zřídka, drží se 30 dní v data/kategorie.json."""
    try:
        with open(TAXONOMY_PATH, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - cached["stazeno"] < 30 * 24 * 3600:
            return cached["kategorie"]
    except (OSError, ValueError, KeyError):
        pass
    out = []

    def walk(nodes, path):
        for n in nodes:
            p = path + [n.get("name", "")]
            if n.get("children"):
                walk(n["children"], p)
            else:
                out.append([n["id"], " › ".join(p)])
    walk(api_get(cfg, tokens, shop_id, "/seller-taxonomy/nodes").get("results", []), [])
    out.sort(key=lambda x: x[1].lower())
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(TAXONOMY_PATH, "w", encoding="utf-8") as f:
        json.dump({"stazeno": int(time.time()), "kategorie": out}, f, ensure_ascii=False)
    return out


DEMO_TAXONOMY = [[1, "Paper & Party Supplies › Paper › Calendars & Planners"], [2, "Paper & Party Supplies › Paper › Stationery › Worksheets"],
                 [3, "Books, Movies & Music › Books › Coloring Books"], [4, "Home & Living › Kitchen & Dining › Drink & Barware › Drinkware › Mugs"],
                 [5, "Home & Living › Home Decor › Vases"]]


def listing_options(cfg, shop_id, demo=False):
    """Co formulář pro nový listing potřebuje vědět o shopě: měnu, kategorie, profily dopravy a zpracování."""
    if demo:
        return {"zapis": True, "mena": "USD", "kategorie": DEMO_TAXONOMY,
                "doprava": [{"id": 11, "nazev": "Standard (CZ → svět)"}], "zpracovani": [{"id": 21, "nazev": "Made to order, 3–5 days"}]}
    tokens = load_tokens()
    if shop_id not in tokens:
        raise AppError("listing_no_shop", "Tahle shopa není přihlášená přes Etsy API.")
    out = {"zapis": can_write(tokens[shop_id]), "mena": "", "kategorie": [], "doprava": [], "zpracovani": [], "chyby": []}
    out["mena"] = api_get(cfg, tokens, shop_id, f"/shops/{shop_id}").get("currency_code", "")
    out["kategorie"] = taxonomy(cfg, tokens, shop_id)
    try:
        out["doprava"] = [{"id": p["shipping_profile_id"], "nazev": p.get("title") or str(p["shipping_profile_id"])}
                          for p in api_get(cfg, tokens, shop_id, f"/shops/{shop_id}/shipping-profiles").get("results", [])]
    except Exception as e:
        out["chyby"].append(str(e))
    try:
        for r in api_get(cfg, tokens, shop_id, f"/shops/{shop_id}/readiness-state-definitions").get("results", []):
            rid = r.get("readiness_state_id") or r.get("readiness_state_definition_id")
            lo = r.get("min_processing_days", r.get("min_processing_time"))
            hi = r.get("max_processing_days", r.get("max_processing_time"))
            unit = r.get("processing_time_unit") or "days"
            label = r.get("processing_days_display_label") or ("" if lo is None and hi is None else
                                                              f"{lo if lo is not None else hi}–{hi if hi is not None else lo} {unit}")
            if lo is not None and lo == hi and not r.get("processing_days_display_label"):
                label = f"{lo} {unit[:-1] if lo == 1 and unit.endswith('s') else unit}"
            out["zpracovani"].append({"id": rid, "stav": r.get("readiness_state") or "", "nazev": label})
    except Exception as e:
        out["chyby"].append(str(e))
    return out


DEMO_PROPERTIES = [
    {"property_id": 200, "name": "Primary color", "display_name": "Primary color", "is_required": False, "supports_attributes": True,
     "supports_variations": True, "is_multivalued": False, "max_values_allowed": None, "scales": [],
     "possible_values": [{"value_id": 1, "name": "Black"}, {"value_id": 2, "name": "White"}, {"value_id": 3, "name": "Green"}]},
    {"property_id": 100, "name": "Size", "display_name": "Size", "is_required": False, "supports_attributes": False,
     "supports_variations": True, "is_multivalued": False, "max_values_allowed": None,
     "scales": [{"scale_id": 1, "display_name": "Inches"}, {"scale_id": 2, "display_name": "Centimeters"}], "possible_values": []},
    {"property_id": 46803063641, "name": "Holiday", "display_name": "Holiday", "is_required": False, "supports_attributes": True,
     "supports_variations": False, "is_multivalued": True, "max_values_allowed": 5, "scales": [],
     "possible_values": [{"value_id": 35, "name": "Christmas"}, {"value_id": 36, "name": "Halloween"}, {"value_id": 37, "name": "Thanksgiving"}]},
]


def listing_properties(cfg, shop_id, taxonomy_id, demo=False):
    """Vlastnosti kategorie: co jde nastavit jako atribut a co jako variantu (barva, velikost…)."""
    if demo:
        props = DEMO_PROPERTIES
    else:
        tokens = load_tokens()
        if shop_id not in tokens:
            raise AppError("listing_no_shop", "Tahle shopa není přihlášená přes Etsy API.")
        props = api_get(cfg, tokens, shop_id, f"/seller-taxonomy/nodes/{int(taxonomy_id)}/properties").get("results", [])
    out = []
    for p in props:
        if not (p.get("supports_attributes") or p.get("supports_variations")):
            continue
        out.append({"id": p["property_id"], "nazev": p.get("display_name") or p.get("name") or str(p["property_id"]),
                    "povinne": bool(p.get("is_required")), "atribut": bool(p.get("supports_attributes")),
                    "varianta": bool(p.get("supports_variations")), "vice": bool(p.get("is_multivalued")),
                    "max": p.get("max_values_allowed"),
                    "skaly": [{"id": x["scale_id"], "nazev": x.get("display_name") or str(x["scale_id"])} for x in p.get("scales") or []],
                    "hodnoty": [{"id": v.get("value_id"), "nazev": v.get("name", ""), "skala": v.get("scale_id")}
                                for v in p.get("possible_values") or []]})
    return out


CUSTOM_PROPERTIES = (513, 514)  # vlastní varianty s vlastním názvem


def build_inventory(body, price, qty, readiness):
    """Etsy inventory z variant: kombinace hodnot → produkt s cenou, množstvím a SKU."""
    var = body.get("varianty") or {}
    props = var.get("vlastnosti") or []
    if not props:
        return None
    if len(props) > 2:
        raise AppError("listing_variants", "Listing může mít nejvýš 2 varianty.")
    custom = iter(CUSTOM_PROPERTIES)
    ids = []
    for p in props:
        pid = p.get("property_id")
        ids.append(next(custom) if pid in (None, "", "custom") else int(pid))
        if not p.get("hodnoty"):
            raise AppError("listing_variants", "Každá varianta potřebuje aspoň jednu hodnotu.")

    def num(v, default, cast):
        try:
            return cast(str(v).replace(",", ".")) if v not in (None, "") else default
        except ValueError:
            raise AppError("listing_variants", "Cena a množství u variant musí být čísla.")
    products = []
    for combo in var.get("kombinace") or []:
        values = []
        for i, p in enumerate(props):
            h = p["hodnoty"][int(combo["hodnoty"][i])]
            pv = {"property_id": ids[i], "property_name": str(p.get("nazev") or "").strip(),
                  "value_ids": [int(h["id"])] if h.get("id") not in (None, "") else [], "values": [str(h.get("nazev", "")).strip()]}
            if p.get("scale_id"):
                pv["scale_id"] = int(p["scale_id"])
            values.append(pv)
        offering = {"price": round(num(combo.get("cena"), price, float), 2), "quantity": num(combo.get("mnozstvi"), qty, int),
                    "is_enabled": combo.get("aktivni", True) is not False}
        if readiness:
            offering["readiness_state_id"] = readiness
        products.append({"sku": str(combo.get("sku") or "").strip(), "property_values": values, "offerings": [offering]})
    if not products or not any(p["offerings"][0]["is_enabled"] for p in products):
        raise AppError("listing_variants", "U variant musí být zapnutá aspoň jedna kombinace.")
    on = lambda k: [ids[int(i)] for i in var.get(k) or []]
    inv = {"products": products, "price_on_property": on("cena_dle"), "quantity_on_property": on("mnozstvi_dle"),
           "sku_on_property": on("sku_dle")}
    if readiness:
        inv["readiness_state_on_property"] = []
    return inv


MAX_FILE = 20 * 1024 * 1024


def _decode_files(items, what):
    out = []
    for it in items or []:
        try:
            data = base64.b64decode(it.get("data") or "", validate=False)
        except (ValueError, TypeError):
            raise AppError("listing_bad_file", f"Soubor {it.get('nazev')} se nepodařilo přečíst.", soubor=it.get("nazev", ""))
        if what == "file" and len(data) > MAX_FILE:
            raise AppError("listing_file_big", f"Soubor {it.get('nazev')} má víc než 20 MB, Etsy ho nepřijme.", soubor=it.get("nazev", ""))
        out.append((os.path.basename(it.get("nazev") or what), data))
    return out


def _shop_tokens(shop_id, need="w"):
    tokens = load_tokens()
    if shop_id not in tokens:
        raise AppError("listing_no_shop", "Tahle shopa není přihlášená přes Etsy API.")
    if need == "w" and not can_write(tokens[shop_id]):
        raise AppError("listing_relogin", "Shopa je přihlášená bez práva vytvářet listingy. V Nastavení ji přihlas znovu.")
    if need == "d" and not can_delete(tokens[shop_id]):
        raise AppError("listing_relogin_d", "Shopa je přihlášená bez práva mazat listingy. V Nastavení ji přihlas znovu.")
    return tokens


def refresh_listings(cfg, shop_id):
    """Po změně přes API stáhne listingy shopy znovu, aby stránka Listingy hned ukazovala novinky."""
    def run():
        with LOCK:
            try:
                tokens = load_tokens()
                con = db()
                sync_listings(cfg, tokens, con, shop_id, tokens[shop_id].get("shop_name", shop_id), int(time.time()))
                con.commit()
                con.close()
            except Exception as e:
                print(f"⚠️  listingy: {e}")
    threading.Thread(target=run, daemon=True).start()


def _image_items(items):
    """Obrázky v novém pořadí: {"id": …} je už nahraný na Etsy, {"nazev", "data"} je nový."""
    out = []
    for it in items or []:
        if it.get("id"):
            out.append(("id", int(it["id"])))
        else:
            out.append(("new", _decode_files([it], "image")[0]))
    return out


def save_listing(cfg, body):
    """Založí nový listing (bez listing_id), nebo upraví existující. Nahraje obrázky, soubory ke stažení,
    atributy a varianty, případně změní stav. Když selže až některý krok, listing zůstane a vrátí se seznam chyb."""
    shop_id = str(body.get("shop_id") or "")
    tokens = _shop_tokens(shop_id)
    lid = int(body["listing_id"]) if body.get("listing_id") else None
    digital = body.get("typ") != "physical"
    title = " ".join(str(body.get("nazev") or "").split())
    tags = [" ".join(str(t).split()) for t in body.get("stitky") or [] if str(t).strip()]
    try:
        price = round(float(str(body.get("cena")).replace(",", ".")), 2)
        qty = int(body.get("mnozstvi") or (999 if digital else 1))
        tax = int(body.get("kategorie"))
    except (TypeError, ValueError):
        raise AppError("listing_fields", "Vyplň název, cenu, množství a kategorii.")
    if not title or len(title) > 140 or price <= 0 or qty < 1:
        raise AppError("listing_fields", "Vyplň název (max 140 znaků), cenu a množství.")
    if len(tags) > 13 or any(len(t) > 20 for t in tags):
        raise AppError("listing_tags", "Etsy povoluje nejvýš 13 štítků, každý do 20 znaků.")
    images = _image_items(body.get("obrazky"))
    files = _image_items(body.get("soubory")) if digital else []  # stejný tvar: {"id"} nebo nový soubor
    for kind, val in files:
        if kind == "new" and len(val[1]) > MAX_FILE:
            raise AppError("listing_file_big", f"Soubor {val[0]} má víc než 20 MB, Etsy ho nepřijme.", soubor=val[0])
    if len(files) > 5:
        raise AppError("listing_files_many", "Digitální listing může mít nejvýš 5 souborů.")
    data = {"title": title, "description": str(body.get("popis") or "").strip() or title,
            "who_made": body.get("who_made") or "i_did", "when_made": body.get("when_made") or "made_to_order",
            "taxonomy_id": tax, "is_supply": False, "tags": tags, "type": "download" if digital else "physical"}
    readiness = None
    if not digital:
        if not body.get("doprava_id") or not body.get("zpracovani_id"):
            raise AppError("listing_physical", "Fyzický listing potřebuje profil dopravy a zpracování.")
        data["shipping_profile_id"] = int(body["doprava_id"])
        readiness = int(body["zpracovani_id"])
    inventory = build_inventory(body, price, qty, readiness)
    base = f"/shops/{shop_id}/listings"
    errors = []
    if lid is None:
        create = dict(data, price=price, quantity=qty)
        if readiness:
            create["readiness_state_id"] = readiness
        listing = api_send(cfg, tokens, shop_id, "POST", base, create)
        lid = listing["listing_id"]
        old_images, old_files, old_props = [], [], []
        if not inventory:
            inventory = None  # cena a množství jsou už v konceptu
    else:
        listing = api_send(cfg, tokens, shop_id, "PATCH", f"{base}/{lid}", data)
        old_images = [i["listing_image_id"] for i in sorted(api_get(cfg, tokens, shop_id, f"/listings/{lid}/images").get("results", []),
                                                            key=lambda i: i.get("rank", 0))]
        old_files = [f["listing_file_id"] for f in api_get(cfg, tokens, shop_id, f"{base}/{lid}/files").get("results", [])] \
            if digital or listing.get("type") == "download" else []
        old_props = [p["property_id"] for p in api_get(cfg, tokens, shop_id, f"{base}/{lid}/properties").get("results", [])]
        if not inventory:  # bez variant: jeden produkt s cenou a množstvím
            offering = {"price": price, "quantity": qty, "is_enabled": True}
            if readiness:
                offering["readiness_state_id"] = readiness
            inventory = {"products": [{"sku": str(body.get("sku") or "").strip(), "property_values": [], "offerings": [offering]}],
                         "price_on_property": [], "quantity_on_property": [], "sku_on_property": []}
    # obrázky: smazat odebrané, nahrát nové a seřadit
    keep = {v for k, v in images if k == "id"}
    for iid in old_images:
        if iid not in keep:
            try:
                api_send(cfg, tokens, shop_id, "DELETE", f"{base}/{lid}/images/{iid}")
            except Exception as e:
                errors.append(f"Obrázek {iid}: {e}")
    for rank, (kind, val) in enumerate(images, 1):
        try:
            if kind == "id":
                if val not in old_images or old_images.index(val) + 1 != rank:
                    api_send(cfg, tokens, shop_id, "POST", f"{base}/{lid}/images", {"listing_image_id": val, "rank": rank}, {})
            else:
                api_send(cfg, tokens, shop_id, "POST", f"{base}/{lid}/images", {"rank": rank}, {"image": val})
        except Exception as e:
            errors.append(f"{val[0] if kind == 'new' else val}: {e}")
    keep = {v for k, v in files if k == "id"}
    for fid in old_files:
        if fid not in keep:
            try:
                api_send(cfg, tokens, shop_id, "DELETE", f"{base}/{lid}/files/{fid}")
            except Exception as e:
                errors.append(f"Soubor {fid}: {e}")
    for rank, (kind, val) in enumerate(files, 1):
        if kind == "new":
            try:
                api_send(cfg, tokens, shop_id, "POST", f"{base}/{lid}/files", {"name": val[0], "rank": rank}, {"file": val})
            except Exception as e:
                errors.append(f"{val[0]}: {e}")
    # atributy (barva, materiál, svátek…): nastavit vyplněné, smazat vyprázdněné
    new_props = set()
    for a in body.get("atributy") or []:
        vals = {"value_ids": [int(x) for x in a.get("value_ids") or []], "values": [str(x) for x in a.get("values") or []]}
        if not vals["value_ids"] and not vals["values"]:
            continue
        if a.get("scale_id"):
            vals["scale_id"] = int(a["scale_id"])
        new_props.add(int(a["property_id"]))
        try:
            api_send(cfg, tokens, shop_id, "PUT", f"{base}/{lid}/properties/{int(a['property_id'])}", vals)
        except Exception as e:
            errors.append(f"{a.get('nazev') or a.get('property_id')}: {e}")
    var_props = {pv["property_id"] for p in (inventory or {}).get("products", []) for pv in p["property_values"]}
    for pid in old_props:
        if pid not in new_props and pid not in var_props:
            try:
                api_send(cfg, tokens, shop_id, "DELETE", f"{base}/{lid}/properties/{pid}")
            except Exception as e:
                errors.append(f"Atribut {pid}: {e}")
    if inventory:
        try:
            api_send(cfg, tokens, shop_id, "PUT", f"/listings/{lid}/inventory?legacy=false", inventory)
        except Exception as e:
            errors.append(f"Varianty: {e}")
    if isinstance(body.get("personalizace"), list):
        err = save_personalization(cfg, tokens, shop_id, lid, body["personalizace"])
        if err:
            errors.append(err)
    state = listing.get("state", "draft")
    want = body.get("stav") or ("active" if body.get("zverejnit") else None)
    if want and want != state and not errors:
        if want == "active" and (not images or (digital and not files)):
            errors.append("Ke zveřejnění chybí obrázek nebo soubor ke stažení, stav se nezměnil.")
        else:
            try:
                state = api_send(cfg, tokens, shop_id, "PATCH", f"{base}/{lid}", {"state": want}).get("state", want)
            except Exception as e:
                errors.append(str(e))
    refresh_listings(cfg, shop_id)
    return {"listing_id": lid, "stav": state, "chyby": errors,
            "url": listing.get("url") or f"https://www.etsy.com/listing/{lid}",
            "upravit": f"https://www.etsy.com/your/shops/me/listing-editor/edit/{lid}"}


PERSO_TYPES = ("text_input", "dropdown", "unlabeled_upload", "labeled_upload")


def _perso_out(q):
    return {"question_id": q.get("question_id"), "typ": q.get("question_type") or "text_input", "text": q.get("question_text") or "",
            "instr": q.get("instructions") or "", "req": bool(q.get("required")),
            "max": q.get("max_allowed_characters") or q.get("max_allowed_files") or None,
            "opts": [o.get("label", "") for o in q.get("options") or []]}


def get_personalization(cfg, tokens, shop_id, lid, listing):
    """Vlastní volby kupujícího (personalizace). Nové API umí až 5 otázek; když není dostupné, vezmou se starší pole listingu."""
    try:
        r = api_get(cfg, tokens, shop_id, f"/listings/{lid}/personalization")
        return [_perso_out(q) for q in r.get("personalization_questions") or []], True
    except Exception:
        pass
    if listing.get("is_personalizable"):
        return [{"question_id": None, "typ": "text_input", "text": "Personalization", "instr": listing.get("personalization_instructions") or "",
                 "req": bool(listing.get("personalization_is_required")), "max": listing.get("personalization_char_count_max") or 256,
                 "opts": []}], False
    return [], False


def save_personalization(cfg, tokens, shop_id, lid, items):
    qs = []
    for q in items[:5]:
        typ = q.get("typ") if q.get("typ") in PERSO_TYPES else "text_input"
        text = " ".join(str(q.get("text") or "").split())[:45]
        if not text:
            continue
        o = {"question_type": typ, "question_text": text, "required": bool(q.get("req"))}
        if q.get("question_id"):
            o["question_id"] = int(q["question_id"])
        opts = [{"label": str(x).strip()[:20 if typ == "dropdown" else 45]} for x in q.get("opts") or [] if str(x).strip()]
        if typ in ("text_input", "unlabeled_upload") and str(q.get("instr") or "").strip():
            o["instructions"] = str(q["instr"]).strip()[:120]
        if typ == "text_input":
            o["max_allowed_characters"] = max(1, min(1024, int(q.get("max") or 256)))
        if typ == "unlabeled_upload":
            o["max_allowed_files"] = max(1, min(10, int(q.get("max") or 1)))
        if typ == "dropdown":
            o["options"] = opts[:30]
        if typ == "labeled_upload":
            o["options"] = opts[:10]
            o["max_allowed_files"] = len(o["options"])
        if typ in ("dropdown", "labeled_upload") and not o["options"]:
            return f"Vlastní volby: otázka „{text}“ nemá žádné možnosti."
        qs.append(o)
    path = f"/shops/{shop_id}/listings/{lid}/personalization?supports_multiple_personalization_questions=true"
    try:
        if qs:
            api_send(cfg, tokens, shop_id, "POST", path, {"personalization_questions": qs})
        else:
            api_send(cfg, tokens, shop_id, "DELETE", path)
        return None
    except Exception as e:
        new_err = str(e)
    # starší způsob: jen jedno textové pole
    if len(qs) > 1 or (qs and qs[0]["question_type"] != "text_input"):
        return f"Vlastní volby: {new_err}"
    legacy = {"is_personalizable": bool(qs)}
    if qs:
        legacy.update({"personalization_is_required": qs[0]["required"], "personalization_char_count_max": qs[0]["max_allowed_characters"],
                       "personalization_instructions": qs[0].get("instructions") or qs[0]["question_text"]})
    try:
        api_send(cfg, tokens, shop_id, "PATCH", f"/shops/{shop_id}/listings/{lid}", legacy)
        return None
    except Exception as e:
        return f"Vlastní volby: {new_err}; {e}"


def _money(m):
    if isinstance(m, dict):
        return round(m.get("amount", 0) / (m.get("divisor") or 100), 2)
    return float(m or 0)


def listing_detail(cfg, shop_id, lid, demo=False):
    """Všechno, co editor potřebuje k existujícímu listingu: texty, obrázky, soubory, atributy a varianty."""
    if demo:
        raise AppError("demo", "V ukázkovém režimu nejde nic měnit.")
    tokens = _shop_tokens(shop_id, need="r")
    lid = int(lid)
    l = api_get(cfg, tokens, shop_id, f"/listings/{lid}", {"includes": "Images"})
    inv = api_get(cfg, tokens, shop_id, f"/listings/{lid}/inventory", {"legacy": "false"})
    files = []
    if l.get("type") == "download":
        files = [{"id": f["listing_file_id"], "nazev": f.get("filename", ""), "velikost": f.get("filesize")}
                 for f in api_get(cfg, tokens, shop_id, f"/shops/{shop_id}/listings/{lid}/files").get("results", [])]
    props = api_get(cfg, tokens, shop_id, f"/shops/{shop_id}/listings/{lid}/properties").get("results", [])
    perso, perso_new = get_personalization(cfg, tokens, shop_id, lid, l)
    products = []
    for p in inv.get("products", []):
        if p.get("is_deleted"):
            continue
        o = (p.get("offerings") or [{}])[0]
        products.append({"sku": p.get("sku") or "", "hodnoty": [{"property_id": v["property_id"], "nazev": v.get("property_name", ""),
                                                                 "scale_id": v.get("scale_id"), "value_id": (v.get("value_ids") or [None])[0],
                                                                 "hodnota": (v.get("values") or [""])[0]} for v in p.get("property_values") or []],
                         "cena": _money(o.get("price")), "mnozstvi": o.get("quantity"), "aktivni": o.get("is_enabled", True),
                         "zpracovani": o.get("readiness_state_id")})
    first = products[0] if products else {}
    return {"listing_id": lid, "shop_id": shop_id, "stav": l.get("state"), "typ": "digital" if l.get("type") == "download" else "physical",
            "nazev": html.unescape(l.get("title") or ""), "popis": html.unescape(l.get("description") or ""),
            "stitky": [html.unescape(t) for t in l.get("tags") or []], "kategorie": l.get("taxonomy_id"),
            "cena": first.get("cena", _money(l.get("price"))), "mnozstvi": l.get("quantity"),
            "doprava": l.get("shipping_profile_id"), "zpracovani": first.get("zpracovani") or l.get("readiness_state_id"),
            "url": l.get("url"), "obrazky": [{"id": i["listing_image_id"], "url": i.get("url_570xN") or i.get("url_fullxfull", "")}
                                             for i in sorted(l.get("images") or [], key=lambda i: i.get("rank", 0))],
            "soubory": files, "atributy": [{"property_id": p["property_id"], "nazev": p.get("property_name", ""),
                                            "value_ids": p.get("value_ids") or [], "values": p.get("values") or [],
                                            "scale_id": p.get("scale_id")} for p in props],
            "produkty": products, "cena_dle": inv.get("price_on_property") or [], "mnozstvi_dle": inv.get("quantity_on_property") or [],
            "sku_dle": inv.get("sku_on_property") or [], "personalizace": perso, "personalizace_nove": perso_new}


def listings_state(cfg, body):
    """Hromadná změna stavu (active / inactive) nebo smazání (stav "smazat")."""
    shop_id, want = str(body.get("shop_id") or ""), body.get("stav")
    if want not in ("active", "inactive", "smazat"):
        raise AppError("listing_fields", "Neznámá akce.")
    tokens = _shop_tokens(shop_id, need="d" if want == "smazat" else "w")
    out = []
    for lid in body.get("ids") or []:
        try:
            if want == "smazat":
                api_send(cfg, tokens, shop_id, "DELETE", f"/listings/{int(lid)}")
                con = db()
                con.execute("DELETE FROM listingy WHERE listing_id=?", (int(lid),))
                con.commit()
                con.close()
            else:
                api_send(cfg, tokens, shop_id, "PATCH", f"/shops/{shop_id}/listings/{int(lid)}", {"state": want})
            out.append({"listing_id": lid, "ok": True})
        except Exception as e:
            out.append({"listing_id": lid, "ok": False, "chyba": str(e)})
    refresh_listings(cfg, shop_id)
    return {"vysledky": out}


# ------------------------------------------------------------------ slevy
# Etsy API neumí Sales & Discounts ani kupóny. Sleva se proto dělá změnou ceny: v den začátku
# dashboard sníží ceny všech variant o zadaná procenta, po konci je vrátí (jen ty, které mezitím nikdo nezměnil).

def _inv_payload(inv, price_fn):
    products = []
    for p in inv.get("products", []):
        if p.get("is_deleted"):
            continue
        offerings = []
        for o in p.get("offerings") or []:
            if o.get("is_deleted"):
                continue
            off = {"price": price_fn(p, _money(o.get("price"))), "quantity": o.get("quantity", 0), "is_enabled": o.get("is_enabled", True)}
            if o.get("readiness_state_id"):
                off["readiness_state_id"] = o["readiness_state_id"]
            offerings.append(off)
        products.append({"sku": p.get("sku") or "", "offerings": offerings,
                         "property_values": [{k: v[k] for k in ("property_id", "property_name", "scale_id", "value_ids", "values") if v.get(k) is not None}
                                             for v in p.get("property_values") or []]})
    out = {"products": products}
    for k in ("price_on_property", "quantity_on_property", "sku_on_property", "readiness_state_on_property"):
        if inv.get(k) is not None:
            out[k] = inv[k]
    return out


def _pkey(p):
    return json.dumps([[v.get("property_id"), v.get("values")] for v in p.get("property_values") or []])


def add_discount(cfg, body, db_path=None):
    shop_id = str(body.get("shop_id") or "")
    _shop_tokens(shop_id)
    try:
        pct = float(str(body.get("procento")).replace(",", "."))
        start = int(datetime.strptime(body["od"], "%Y-%m-%d").timestamp())
        end = int((datetime.strptime(body["do"], "%Y-%m-%d") + timedelta(days=1)).timestamp())  # včetně posledního dne
    except (KeyError, TypeError, ValueError):
        raise AppError("sale_fields", "Vyplň slevu v procentech a data od a do.")
    if not 0 < pct < 100 or end <= start or end <= time.time():
        raise AppError("sale_fields", "Sleva musí být mezi 0 a 100 % a konec nesmí být před začátkem ani v minulosti.")
    ids = [int(x) for x in body.get("ids") or []]
    con = db(db_path)
    try:
        busy = [lid for lid in ids if con.execute("SELECT 1 FROM slevy WHERE listing_id=? AND stav IN ('naplanovano','bezi') "
                                                  "AND od_ts < ? AND do_ts > ?", (lid, end, start)).fetchone()]
        if busy:
            raise AppError("sale_overlap", f"{len(busy)} z vybraných listingů už má v tomhle termínu slevu.", n=len(busy))
        for lid in ids:
            con.execute("INSERT INTO slevy (shop_id, listing_id, procento, od_ts, do_ts, stav, vytvoreno_ts) VALUES (?,?,?,?,?,?,?)",
                        (shop_id, lid, pct, start, end, "naplanovano", int(time.time())))
        con.commit()
    finally:
        con.close()
    threading.Thread(target=process_discounts, args=(cfg,), daemon=True).start()
    return {"ok": True, "pocet": len(ids)}


def cancel_discount(cfg, body, db_path=None):
    con = db(db_path)
    try:
        row = con.execute("SELECT stav FROM slevy WHERE id=?", (int(body.get("id")),)).fetchone()
        if row and row[0] == "naplanovano":
            con.execute("UPDATE slevy SET stav='zruseno' WHERE id=?", (int(body["id"]),))
        elif row and row[0] == "bezi":
            con.execute("UPDATE slevy SET do_ts=? WHERE id=?", (int(time.time()), int(body["id"])))  # ukončí se hned
        con.commit()
    finally:
        con.close()
    process_discounts(cfg)
    return {"ok": True}


def process_discounts(cfg):
    """Spustí naplánované slevy a ukončí ty, kterým vypršel termín. Volá se při každé kontrole."""
    if not config_ready(cfg):
        return
    with DISCOUNT_LOCK:
        con = db()
        tokens = load_tokens()
        now = int(time.time())
        touched = set()
        for sid, shop_id, lid, pct, stav, puvodni, nove in con.execute(
                "SELECT id, shop_id, listing_id, procento, stav, puvodni, nove FROM slevy WHERE "
                "(stav='naplanovano' AND od_ts<=?) OR (stav='bezi' AND do_ts<=?)", (now, now)).fetchall():
            if shop_id not in tokens:
                continue
            try:
                inv = api_get(cfg, tokens, shop_id, f"/listings/{lid}/inventory", {"legacy": "false"})
                if stav == "naplanovano":
                    if con.execute("SELECT do_ts FROM slevy WHERE id=?", (sid,)).fetchone()[0] <= now:
                        con.execute("UPDATE slevy SET stav='hotovo' WHERE id=?", (sid,))  # termín propásnutý (Mac byl vypnutý)
                        continue
                    old, new = {}, {}

                    def cut(p, price):
                        old[_pkey(p)] = price
                        new[_pkey(p)] = max(0.2, round(price * (1 - pct / 100), 2))
                        return new[_pkey(p)]
                    api_send(cfg, tokens, shop_id, "PUT", f"/listings/{lid}/inventory?legacy=false", _inv_payload(inv, cut))
                    con.execute("UPDATE slevy SET stav='bezi', puvodni=?, nove=?, chyba=NULL WHERE id=?",
                                (json.dumps(old), json.dumps(new), sid))
                else:
                    old, new = json.loads(puvodni or "{}"), json.loads(nove or "{}")
                    back = lambda p, price: old[_pkey(p)] if _pkey(p) in old and abs(new.get(_pkey(p), -1) - price) < 0.005 else price
                    api_send(cfg, tokens, shop_id, "PUT", f"/listings/{lid}/inventory?legacy=false", _inv_payload(inv, back))
                    con.execute("UPDATE slevy SET stav='hotovo', chyba=NULL WHERE id=?", (sid,))
                touched.add(shop_id)
            except Exception as e:
                con.execute("UPDATE slevy SET chyba=? WHERE id=?", (str(e)[:300], sid))
            con.commit()
        con.commit()
        con.close()
    for shop_id in touched:
        refresh_listings(cfg, shop_id)
