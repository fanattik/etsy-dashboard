#!/usr/bin/env python3
"""Sestaví „Etsy Dashboard.app“ pro Mac a zabalí ji do dist/Etsy-Dashboard-mac.zip.

Potřebuje Pillow (jen kvůli ikoně): python3 -m pip install pillow
"""
import io
import json
import os
import time
import zipfile

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_ZIP = os.path.join(ROOT, "dist", "Etsy-Dashboard-mac.zip")
APP = "Etsy Dashboard.app"

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Etsy Dashboard</string>
  <key>CFBundleDisplayName</key><string>Etsy Dashboard</string>
  <key>CFBundleIdentifier</key><string>io.github.fanattik.etsy-dashboard</string>
  <key>CFBundleVersion</key><string>%(version)s</string>
  <key>CFBundleShortVersionString</key><string>%(version)s</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>etsy-dashboard</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
"""


def icon_png(size=1024):
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = int(s * 0.09)  # okraj jako u macOS ikon
    d.rounded_rectangle([m, m, s - m, s - m], radius=int(s * 0.2), fill=(232, 116, 59, 255))
    # tři rostoucí sloupce + základna
    base = int(s * 0.70)
    bw = int(s * 0.12)
    gap = int(s * 0.055)
    x0 = (s - (3 * bw + 2 * gap)) // 2
    for i, h in enumerate((0.20, 0.31, 0.44)):
        x = x0 + i * (bw + gap)
        d.rounded_rectangle([x, base - int(s * h), x + bw, base], radius=int(bw * 0.28), fill="white")
    d.rounded_rectangle([x0 - int(s * 0.03), base + int(s * 0.04), s - x0 + int(s * 0.03), base + int(s * 0.075)],
                        radius=int(s * 0.02), fill=(255, 255, 255, 215))
    return img


def icns_bytes():
    buf = io.BytesIO()
    icon_png().save(buf, format="ICNS", sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)])
    return buf.getvalue()


def add(z, name, data, mode=0o644, when=None):
    info = zipfile.ZipInfo(name, date_time=when)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100000 | mode) << 16
    z.writestr(info, data)


def add_dir(z, name, when):
    info = zipfile.ZipInfo(name.rstrip("/") + "/", date_time=when)
    info.create_system = 3
    info.external_attr = ((0o040000 | 0o755) << 16) | 0x10
    z.writestr(info, b"")


def read(name):
    with open(os.path.join(ROOT, name), "rb") as f:
        return f.read()


def app_version():
    for line in read("app/etsy_dashboard.py").decode().splitlines():
        if line.startswith("VERSION = "):
            return line.split('"')[1]
    raise SystemExit("VERSION nenalezena v app/etsy_dashboard.py")


def app_files():
    """Soubory aplikace: všechny moduly v app/ a dashboard.html (stejný seznam stahuje automatická aktualizace)."""
    return sorted(f for f in os.listdir(os.path.join(ROOT, "app")) if f.endswith(".py")) + ["dashboard.html"]


def main():
    os.makedirs(os.path.dirname(OUT_ZIP), exist_ok=True)
    with open(os.path.join(ROOT, "app", "version.json"), "w", encoding="utf-8") as f:  # pro automatické aktualizace
        json.dump({"verze": app_version(), "soubory": app_files()}, f)
        f.write("\n")
    when = time.localtime()[:6]
    root = "Etsy Dashboard/"
    app = root + APP + "/Contents/"
    with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for d in (root, root + APP, app, app + "MacOS", app + "Resources"):
            add_dir(z, d, when)
        add(z, app + "Info.plist", (INFO_PLIST % {"version": app_version()}).encode(), when=when)
        add(z, app + "PkgInfo", b"APPL????", when=when)
        add(z, app + "MacOS/etsy-dashboard", read("mac/launcher.sh"), 0o755, when)
        add(z, app + "Resources/AppIcon.icns", icns_bytes(), when=when)
        for name in app_files():
            add(z, app + "Resources/" + name, read("app/" + name), when=when)
        add(z, root + "NAVOD.md", read("docs/NAVOD.md"), when=when)
        add(z, root + "README.md", read("README.md"), when=when)
        add(z, root + "LICENSE", read("LICENSE"), when=when)
    icon_png(512).save(os.path.join(ROOT, "docs", "icon.png"))
    print("Hotovo:", OUT_ZIP)


if __name__ == "__main__":
    main()
