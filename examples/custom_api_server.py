#!/usr/bin/env python3
"""Reference implementation of the dashboard's Custom API (docs/custom-api.md).

A tiny web shop backend with no shop front: products, orders and images are kept in a JSON file
and a folder next to it. Use it to try the Custom API channel, or as a model when adding the API
to a real shop. Python 3.9+, standard library only.

    python3 custom_api_server.py --key secret                 # serve on http://127.0.0.1:8790/api/dashboard
    python3 custom_api_server.py add-order MUG-01 2           # simulate a customer order (product SKU, quantity)
    python3 custom_api_server.py edit MUG-01 price=420        # simulate an edit in the shop's admin
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PREFIX = "/api/dashboard"
LOCK = threading.Lock()


class Store:
    """Everything in one JSON file; uploaded images and files in <data>-media/<sha256>."""

    def __init__(self, path):
        self.path = path
        self.media = os.path.splitext(path)[0] + "-media"
        os.makedirs(self.media, exist_ok=True)

    def load(self):
        if not os.path.exists(self.path):
            return {"info": {"name": "Example shop", "currency": "CZK", "language": "cs"}, "products": {}, "orders": {}, "next_order": 1001}
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def save(self, data):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def has_blob(self, digest):
        return os.path.exists(os.path.join(self.media, digest))

    def put_blob(self, data):
        digest = hashlib.sha256(data).hexdigest()
        with open(os.path.join(self.media, digest), "wb") as f:
            f.write(data)
        return digest


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def public(p):
    """A product as the API returns it (no file contents)."""
    return {**p, "images": [{k: v for k, v in i.items() if k != "data"} for i in p.get("images", [])],
            "files": [{k: v for k, v in i.items() if k != "data"} for i in p.get("files", [])]}


def summary(p):
    return {k: p.get(k) for k in ("sku", "title", "status", "price", "currency", "quantity", "url", "updated_at")}


def store_media(store, items):
    """Keep known files by sha256, store new ones from base64 `data`, in the order given."""
    out = []
    for it in items or []:
        digest = it.get("sha256") or ""
        if it.get("data"):
            raw = base64.b64decode(it["data"])
            digest = store.put_blob(raw)
            if it.get("sha256") and it["sha256"] != digest:
                raise ApiError(400, f"sha256 doesn't match the data of {it.get('filename')}")
        elif not store.has_blob(digest):
            raise ApiError(409, f"Unknown file {it.get('filename')} ({digest[:12]}…), send it with data")
        out.append({"filename": it.get("filename") or digest, "content_type": it.get("content_type") or "", "sha256": digest})
    return out


def handle(store, base_url, method, path, query, body):
    data = store.load()
    now = int(time.time())
    parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/")]
    page = int((query.get("page") or ["1"])[0] or 1)

    if method == "GET" and parts == ["info"]:
        return 200, {**data["info"], "api_version": 1}

    if parts and parts[0] == "products":
        if method == "GET" and len(parts) == 1:
            items = sorted(data["products"].values(), key=lambda p: p["sku"])
            return 200, {"products": [summary(p) for p in items], "next_page": None}
        sku = parts[1] if len(parts) > 1 else ""
        p = data["products"].get(sku)
        if method == "GET" and len(parts) == 2:
            if not p:
                raise ApiError(404, f"No product {sku}")
            return 200, public(p)
        if method == "PUT" and len(parts) == 2:
            for k in ("title", "price", "currency"):
                if body.get(k) in (None, ""):
                    raise ApiError(422, f"Missing {k}")
            new = {k: body.get(k) for k in ("title", "description", "price", "currency", "quantity", "type", "tags", "category",
                                           "weight_g", "dimensions_mm", "variants")}
            new.update(sku=sku, images=store_media(store, body.get("images")), files=store_media(store, body.get("files")),
                       status=(p or {}).get("status") or body.get("status") or "draft",
                       url=f"{base_url}/shop/{urllib.parse.quote(sku)}", updated_at=now)
            data["products"][sku] = new
            store.save(data)
            return (200 if p else 201), public(new)
        if method == "PATCH" and parts[2:] == ["stock"]:
            if not p:
                raise ApiError(404, f"No product {sku}")
            if "quantity" in body:
                p["quantity"] = body["quantity"]
            by_sku = {v.get("sku"): v for v in p.get("variants") or []}
            for v in body.get("variants") or []:
                if v.get("sku") in by_sku:
                    by_sku[v["sku"]]["quantity"] = v.get("quantity")
            p["updated_at"] = now
            store.save(data)
            return 200, public(p)

    if parts and parts[0] == "orders":
        if method == "GET" and len(parts) == 1:
            since = int((query.get("since") or ["0"])[0] or 0)
            orders = sorted((o for o in data["orders"].values() if o["updated_at"] >= since), key=lambda o: o["updated_at"])
            size = 50
            chunk = orders[(page - 1) * size: page * size]
            return 200, {"orders": chunk, "next_page": page + 1 if len(orders) > page * size else None}
        if method == "POST" and len(parts) == 3 and parts[2] == "shipment":
            o = data["orders"].get(parts[1])
            if not o:
                raise ApiError(404, f"No order {parts[1]}")
            if not str(body.get("tracking_number") or "").strip():
                raise ApiError(422, "Missing tracking_number")
            o["shipment"] = {"carrier": body.get("carrier") or "", "tracking_number": body["tracking_number"].strip()}
            o.update(shipped=True, status="shipped", updated_at=now)
            store.save(data)
            return 200, o

    raise ApiError(404, "Not found")


def make_handler(store, key):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

        def reply(self, status, obj):
            out = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def run(self):
            url = urllib.parse.urlparse(self.path)
            if not url.path.startswith(PREFIX + "/"):
                return self.reply(404, {"error": "Not found"})
            if self.headers.get("Authorization") != f"Bearer {key}":
                return self.reply(401, {"error": "Wrong API key"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                base = f"http://{self.headers.get('Host')}"
                with LOCK:
                    status, obj = handle(store, base, self.command, url.path[len(PREFIX):], urllib.parse.parse_qs(url.query), body)
                self.reply(status, obj)
            except ApiError as e:
                self.reply(e.status, {"error": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                self.reply(400, {"error": f"Bad request: {e}"})

        do_GET = do_PUT = do_PATCH = do_POST = run

    return H


def add_order(store, sku, qty):
    with LOCK:
        data = store.load()
        p = data["products"].get(sku)
        if not p:
            sys.exit(f"No product {sku}")
        now, oid = int(time.time()), str(data["next_order"])
        data["next_order"] += 1
        v = (p.get("variants") or [None])[0]
        price = (v or {}).get("price") or p["price"]
        data["orders"][oid] = {
            "id": oid, "number": f"{time.strftime('%Y')}-{oid}", "created_at": now, "updated_at": now,
            "status": "paid", "paid": True, "shipped": False,
            "customer": {"name": "Jana Nováková", "email": "jana@example.com", "city": "Brno", "country": "CZ"},
            "total": round(price * qty, 2), "currency": p["currency"], "shipping_price": 0,
            "items": [{"id": f"{oid}-1", "sku": (v or {}).get("sku") or sku, "product_sku": sku, "title": p["title"], "quantity": qty,
                       "price": price, "variant": ", ".join(f"{k}: {x}" for k, x in ((v or {}).get("options") or {}).items()),
                       "personalization": ""}],
            "shipment": None}
        if p.get("quantity") is not None:  # a sale changes stock only, so updated_at stays
            p["quantity"] = max(0, p["quantity"] - qty)
        store.save(data)
    print(f"Order {oid} created")


def edit(store, sku, changes):
    with LOCK:
        data = store.load()
        p = data["products"].get(sku)
        if not p:
            sys.exit(f"No product {sku}")
        for c in changes:
            k, _, v = c.partition("=")
            p[k] = float(v) if k in ("price",) else int(v) if k == "quantity" else v
        p["updated_at"] = int(time.time())
        store.save(data)
    print(f"{sku} updated")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="*", help="add-order SKU QTY | edit SKU key=value…")
    ap.add_argument("--key", default="secret", help="API key the dashboard must send")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--data", default=os.path.join(HERE, "custom_api_data.json"), help="JSON file with the shop's data")
    a = ap.parse_args()
    store = Store(a.data)
    if a.command[:1] == ["add-order"] and len(a.command) == 3:
        return add_order(store, a.command[1], int(a.command[2]))
    if a.command[:1] == ["edit"] and len(a.command) >= 3:
        return edit(store, a.command[1], a.command[2:])
    if a.command:
        sys.exit(ap.format_usage())
    print(f"Custom API on http://127.0.0.1:{a.port}{PREFIX} (key: {a.key})")
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(store, a.key)).serve_forever()


if __name__ == "__main__":
    main()
