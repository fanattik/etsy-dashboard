# Custom API channel

The dashboard can sell through any web shop that implements the small HTTP API below. In the dashboard it is the **Custom API** channel: add the shop in *Settings → Custom API shops* with its base URL and an API key, then publish catalog products to it, download its orders and send tracking numbers back, the same way as with Etsy.

The dashboard always calls the shop, never the other way round, so it works while the dashboard runs on a laptop. Nothing is sent to the shop unless the user clicks a button; downloading products and orders happens on every regular check.

A complete reference implementation in Python (standard library only, data in a JSON file) is in [`examples/custom_api_server.py`](../examples/custom_api_server.py). Run it with `python3 examples/custom_api_server.py --key secret` and add `http://127.0.0.1:8790/api/dashboard` with key `secret` as a shop to try the whole flow.

## Basics

- **Base URL**: whatever the shop chooses, for example `https://shop.example.com/api/dashboard`. All paths below are relative to it.
- **HTTPS** is required. Plain `http://` is accepted only for `localhost` and `127.0.0.1`.
- **Authentication**: every request carries `Authorization: Bearer <api key>`. Answer `401` when the key is missing or wrong.
- **Format**: JSON in UTF-8 both ways (`Content-Type: application/json`).
- **Times** are Unix timestamps in seconds. **Prices** are decimal numbers in the stated currency (`12.9`, not cents).
- **Errors**: any non-2xx status with `{"error": "Human readable message"}`. The dashboard shows the message to the user. `404` on a product means "this SKU doesn't exist yet".
- **Paging**: list endpoints take `?page=1,2,…` and answer `"next_page": <number or null>`. Page size is up to the shop.
- Unknown fields must be ignored by both sides, so either side can add fields later.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/info` | Shop name, currency and language; used by *Test connection* |
| GET | `/products` | Summary of all products the dashboard manages (state, price, stock, last change) |
| GET | `/products/{sku}` | One product in full |
| PUT | `/products/{sku}` | Create or replace a product |
| POST | `/media` | Optional: where to upload a new image or file before `PUT /products/{sku}` |
| PATCH | `/products/{sku}/stock` | Change stock only |
| GET | `/orders?since={ts}` | Orders created or changed since a time |
| POST | `/orders/{id}/shipment` | Carrier and tracking number; marks the order shipped |

`{sku}` and `{id}` are URL-encoded.

### GET /info

```json
{"name": "My shop", "currency": "CZK", "language": "cs", "api_version": 1}
```

### GET /products

```json
{
  "products": [
    {"sku": "MUG-01", "title": "Ceramic mug", "status": "active", "price": 390, "currency": "CZK",
     "quantity": 12, "url": "https://shop.example.com/p/mug-01", "updated_at": 1790000000}
  ],
  "next_page": null
}
```

- `status`: `active`, `draft` or `inactive`.
- `quantity`: total stock, or `null` for unlimited (digital products, made to order without a limit).
- `updated_at` must change whenever the product's content is edited in the shop (title, description, price, tags, images, variants, status). The dashboard compares it with the time of its own last push and tells the user that the product was changed in the shop. Stock that goes down after a sale should not change it; the dashboard compares stock separately. Return the same `updated_at` the shop stored, since the dashboard keeps the value from the `PUT` answer.
- List every product with a SKU, or at least every product the dashboard created. A SKU missing from the list is shown as no longer in the shop.

### GET /products/{sku}

The same object the dashboard sends in `PUT` (below), plus `url`, `updated_at`, `status`, and `images` / `files` **without** `data`:

```json
{
  "sku": "MUG-01", "title": "Ceramic mug", "description": "…", "status": "active",
  "price": 390, "currency": "CZK", "quantity": 12, "type": "physical",
  "tags": ["mug", "ceramic"], "category": "Kitchen", "weight_g": 350,
  "dimensions_mm": {"length": 120, "width": 90, "height": 100},
  "variants": [{"sku": "MUG-01-W", "options": {"Color": "White"}, "price": 390, "quantity": 5, "active": true}],
  "images": [{"filename": "mug.jpg", "content_type": "image/jpeg", "sha256": "9f2c…", "url": "https://…"}],
  "files": [],
  "url": "https://shop.example.com/p/mug-01", "updated_at": 1790000000
}
```

`404` when the SKU doesn't exist.

### PUT /products/{sku}

Creates the product or replaces it completely. Body as above, without `url` and `updated_at`:

- `title`, `description` (plain text with line breaks, may be empty), `price`, `currency` are always present.
- `status` is sent only when the product is created (`active` or `draft`). On later updates it is left out and the shop keeps its current status.
- `quantity`: `null` means unlimited.
- `type`: `physical` or `digital`.
- `tags`: list of strings, may be empty. `category`: free text from the catalog, or `null`.
- `weight_g`, `dimensions_mm`: may be `null`.
- `variants`: may be empty. Each variant has its own `sku` (may be empty), `options` (name → value, at most a few), `price` (full price, not a surcharge), `quantity` (`null` = unlimited) and `active`.
- `images`: the full ordered list; the first one is the main image. Each item has `filename`, `content_type`, `sha256` and, **only when the shop doesn't have that image yet**, `data` with the file in base64. The dashboard first calls `GET /products/{sku}` and sends `data` only for hashes the shop didn't list. The shop must keep images it already has by `sha256`, add the new ones, delete images that are no longer listed, and follow the list order.
- `files`: downloadable files of a digital product, same rules as `images`.

If the shop implements `POST /media` (below), the dashboard uploads new images and files there first and the `PUT` carries only their `sha256`, never `data`. This keeps the request small; hosting platforms such as Vercel reject request bodies over 4.5 MB.

Answer `200` (or `201`) with the stored product in the same shape as `GET /products/{sku}` (without `data`), including `url` and `updated_at`. If an image arrives without `data` and the shop doesn't know its hash, answer `409` with an error, and the dashboard uploads (or sends with `data`) all images again.

### POST /media

Optional, but recommended for shops behind a request size limit. Before `PUT /products/{sku}`, the dashboard calls it for every image or file the shop doesn't have yet:

```json
{"sha256": "9f2c…", "filename": "mug.jpg", "content_type": "image/jpeg", "size": 2483021}
```

Answer `{"exists": true}` when the shop already holds a file with that hash. Otherwise answer where the dashboard should send the raw file:

```json
{"exists": false, "upload_url": "https://storage.example.com/upload/9f2c…?token=…", "method": "PUT",
 "headers": {"content-type": "image/jpeg"}}
```

The dashboard sends the file's bytes as the request body to `upload_url` with that method and those headers, and without its API key, so the URL must carry its own authorization, for example a pre-signed storage URL. The following `PUT /products/{sku}` then lists the file by `sha256` only, and the shop takes it from where it was uploaded. Answer `404` if the shop doesn't implement this endpoint; the dashboard then sends `data` inside the `PUT` as described above.

### PATCH /products/{sku}/stock

```json
{"quantity": 10, "variants": [{"sku": "MUG-01-W", "quantity": 4}]}
```

Answer like `GET /products/{sku}`.

### GET /orders?since={ts}&page={n}

Orders **created or changed** at or after `since`, oldest first. On the first sync `since` is about a year back; afterwards it is the last check minus two days, so the same order may arrive more than once; the dashboard updates it in place.

```json
{
  "orders": [
    {
      "id": "1042", "number": "2026-1042",
      "created_at": 1790000000, "updated_at": 1790003600,
      "status": "paid", "paid": true, "shipped": false,
      "customer": {"name": "Jana Nováková", "email": "jana@example.com", "city": "Brno", "country": "CZ"},
      "total": 829, "currency": "CZK", "shipping_price": 89,
      "items": [
        {"id": "5501", "sku": "MUG-01-W", "product_sku": "MUG-01", "title": "Ceramic mug",
         "quantity": 2, "price": 370, "variant": "Color: White", "personalization": "For Jana"}
      ],
      "shipment": {"carrier": "Zásilkovna", "tracking_number": "Z123456789"}
    }
  ],
  "next_page": null
}
```

- `id` is the shop's own order id (string or number); it is used in `/orders/{id}/shipment`. `number` is what the customer sees; the dashboard shows it.
- `status` is free text shown in the dashboard. `paid` and `shipped` drive the dashboard's order-state rules.
- `items[].id` must be unique within the shop. `product_sku` is the SKU the dashboard used in `PUT /products/{sku}`; `sku` is the variant SKU (or the product SKU when there are no variants). `price` is the unit price.
- `shipment` is `null` until the order has a tracking number.

### POST /orders/{id}/shipment

```json
{"carrier": "Zásilkovna", "tracking_number": "Z123456789"}
```

Store the shipment, mark the order shipped (and email the customer if the shop does that). Answer with the updated order object as in `/orders`. Sending it again for the same order replaces the tracking number.
