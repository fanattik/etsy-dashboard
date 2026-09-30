#!/bin/bash
# Spouštěč aplikace „Etsy Dashboard.app“ na Macu.
# Při prvním otevření (nebo po aktualizaci) nainstaluje dashboard tak, aby běžel na pozadí
# po každém přihlášení do Macu. Pak jen otevře dashboard v prohlížeči.

RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
APP_DIR="$HOME/Library/Application Support/EtsyDashboard"
LABEL="io.github.fanattik.etsy-dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
URL="http://127.0.0.1:8765"

dialog() {  # $1 = text, $2 = tlačítka (AppleScript seznam), vrací stisknuté tlačítko
  osascript -e "button returned of (display dialog \"$1\" buttons {$2} default button 1 with title \"Etsy Dashboard\" with icon note)" 2>/dev/null
}

running() { curl -s -m 2 -o /dev/null "$URL/api/stav"; }

# Převod ze starší verze „Etsy hlídač“: vypnout starou službu a převzít její data
OLD_LABEL="cz.milumacreations.etsy-hlidac"
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
OLD_DIR="$HOME/Library/Application Support/EtsyHlidac"
if [ -f "$OLD_PLIST" ]; then
  launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || launchctl unload -w "$OLD_PLIST" 2>/dev/null
  rm -f "$OLD_PLIST" "$HOME/Desktop/Etsy hlídač.webloc"
fi
if [ -d "$OLD_DIR" ] && [ ! -d "$APP_DIR" ]; then
  mv "$OLD_DIR" "$APP_DIR"
  rm -f "$APP_DIR/etsy_hlidac.py"
fi

version_of() { grep -m1 '^VERSION = ' "$1" 2>/dev/null | cut -d'"' -f2; }
newer() {  # je verze $1 vyšší než $2?
  local IFS=.; local a=($1) b=($2) i
  for i in 0 1 2 3; do
    [ "${a[i]:-0}" -gt "${b[i]:-0}" ] && return 0
    [ "${a[i]:-0}" -lt "${b[i]:-0}" ] && return 1
  done
  return 1
}

# Instalace při prvním spuštění, po odinstalaci, nebo když je v aplikaci novější verze,
# než jakou si dashboard mezitím sám stáhl.
needs_install=0
[ -f "$PLIST" ] || needs_install=1
[ -f "$APP_DIR/etsy_dashboard.py" ] || needs_install=1
newer "$(version_of "$RES/etsy_dashboard.py")" "$(version_of "$APP_DIR/etsy_dashboard.py")" && needs_install=1

if [ "$needs_install" = 1 ]; then
  # Python 3.9+ (bez vyvolání instalace vývojářských nástrojů, pokud chybí)
  PY=""
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    [ -x "$c" ] || continue
    if [ "$c" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
  if [ -z "$PY" ]; then
    b=$(dialog "Etsy Dashboard potřebuje Python 3. Stáhni z python.org „macOS installer“, nainstaluj ho a pak otevři Etsy Dashboard znovu." "\"Stáhnout Python\", \"Zrušit\"")
    [ "$b" = "Stáhnout Python" ] && open "https://www.python.org/downloads/macos/"
    exit 1
  fi

  mkdir -p "$APP_DIR" "$HOME/Library/LaunchAgents"
  cp "$RES/etsy_dashboard.py" "$RES/dashboard.html" "$APP_DIR/"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$APP_DIR/etsy_dashboard.py</string>
    <string>web</string>
    <string>--sluzba</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/log.txt</string>
  <key>StandardErrorPath</key><string>$APP_DIR/log.txt</string>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null
elif ! running; then
  # nainstalováno, ale neběží (např. po odinstalaci bez smazání dat): znovu zapnout
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null
fi

for _ in $(seq 1 30); do running && break; sleep 0.5; done
if ! running; then
  dialog "Etsy Dashboard se nepodařilo spustit. Podrobnosti jsou v souboru log.txt ve složce ~/Library/Application Support/EtsyDashboard." "\"OK\"" >/dev/null
  exit 1
fi
open "$URL/"
