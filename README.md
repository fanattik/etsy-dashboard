# Etsy Dashboard

A small local dashboard for Etsy sellers: orders, revenue, Etsy fees, payouts and what still needs to ship, for one or more shops in one place. It runs on your own computer and opens in the browser; your data and keys never leave your machine (except for requests to Etsy).

![Dashboard](docs/screenshot.png)

The interface is available in English, Czech and German (switch in *Settings*, which also has currency and light or dark theme; it follows your browser language by default). A step-by-step guide in Czech is in [docs/NAVOD.md](docs/NAVOD.md).

## Features

- Revenue, order count, average order, Etsy fees (incl. VAT), net to account, payouts, balance and orders waiting to ship
- Monthly revenue chart (12 months), best sellers, fee breakdown
- Orders and payment-account tables with filters, sorting and CSV export (Excel-friendly)
- Per-order shipping notes: carrier, tracking number and your shipping cost (kept locally, never overwritten by imports)
- Listings page: price, stock, views, favorites, and units sold and revenue per listing in the selected period
- Create new listings through the Etsy API: load a folder of products (each with an `etsy-listing.md`, images and download files), check and edit them, then create drafts or publish them in one go. Each listing can be digital (download files) or physical (shipping and processing profiles), with category attributes, up to two variations with their own price, quantity and SKU, and custom options for the buyer (personalization questions: text, dropdown or file upload)
- Edit existing listings (text, price, images, files, attributes, variations), activate, deactivate or delete them, one at a time or several at once
- Scheduled sales from a start to an end date. The Etsy API has no Sales & Discounts endpoints, so the dashboard lowers the prices on the start day and restores them after the end (only while it runs)
- Stats page: listing views, orders, conversion and revenue charts, new favorites, shop followers, reviews, repeat buyers, cities and countries, and listings ranked by views. The Etsy API only reports lifetime view counts, so the dashboard stores a daily snapshot and the history starts on the first day it runs (visits and traffic sources are not available through the API)
- Several shops side by side, filter by shop and period
- Show all amounts in one currency of your choice (converted at the daily ECB rate)
- Two ways to get data:
  - **CSV import** (works right away): upload the files from *Shop Manager → Settings → Options → Download Data* (Payment Account statements, Orders, Order Items, Payments, Currently for Sale Listings). Re-uploading the same file never duplicates anything.
  - **Etsy Open API v3** (automatic every 15 minutes) once your Etsy developer app is approved
- New-order notifications in the browser, optionally on your phone via [ntfy](https://ntfy.sh)
- Updates itself from this repository once a day

## Install on macOS

1. Download [Etsy-Dashboard-mac.zip](https://github.com/fanattik/etsy-dashboard/raw/main/dist/Etsy-Dashboard-mac.zip), unzip it and move **Etsy Dashboard** to *Applications*.
2. Open it. The app is not signed with an Apple Developer ID, so macOS blocks the first launch: click *Done*, then go to *System Settings → Privacy & Security* and click *Open Anyway*. This is needed only once.
3. If Python 3 is missing, the app offers to download it from python.org.

The app installs a background service (LaunchAgent) that starts at login, so afterwards the dashboard is always at <http://127.0.0.1:8765>. Uninstall from the dashboard: *Settings → Uninstall*.

## Run anywhere else

Only the Python 3.9+ standard library is needed:

```sh
python3 app/etsy_dashboard.py          # start and open the browser
python3 app/etsy_dashboard.py demo     # demo data, port 8766
python3 app/etsy_dashboard.py jednou   # one API sync without the UI (cron)
```

## Connecting the Etsy API

1. Register an app at <https://www.etsy.com/developers/register>.
2. Add the callback URL `https://localhost:3003/etsy`.
3. After Etsy approves it, paste the *Keystring* and *Shared secret* into *Settings* and sign in each shop. The browser then shows a "can't connect" page; copy the full address from the address bar back into the dashboard.

## Project layout

| Path | What |
| --- | --- |
| `app/etsy_dashboard.py` | entry point: local web server, background sync loop, self-update, `VERSION` |
| `app/zaklad.py` | paths, constants, settings, tokens, HTTP helper |
| `app/databaze.py` | SQLite schema and helpers |
| `app/etsy_api.py` | Etsy Open API v3: OAuth sign-in and API calls |
| `app/synchronizace.py` | sync of orders, payment account, listings and stats from Etsy, ECB rates |
| `app/etsy_listingy.py` | creating and editing listings, personalization, scheduled sales |
| `app/prehled.py` | data for the dashboard and stats pages, CSV export, shipping notes, demo data |
| `app/csv_import.py` | import of CSV files downloaded from Etsy |
| `app/dashboard.html` | the dashboard UI (no build step, no external libraries) |
| `app/version.json` | version used by the self-updater |
| `mac/launcher.sh`, `mac/build_mac_app.py` | macOS app bundle and its build script (`python3 mac/build_mac_app.py`, needs Pillow for the icon) |
| `dist/Etsy-Dashboard-mac.zip` | built macOS app |

## Releasing an update

Bump `VERSION` in `app/etsy_dashboard.py`, run `python3 mac/build_mac_app.py` (it also rewrites `app/version.json` and the zip) and push to `main`. Installed apps pick it up within a day.

## License

[MIT](LICENSE)
