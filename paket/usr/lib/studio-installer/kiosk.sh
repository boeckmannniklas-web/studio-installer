#!/bin/sh
# Gemeinsamer Teil von /usr/bin/studio und /usr/bin/studio-einrichtung:
#   kiosk_oeffnen <url>
# Öffnet die Adresse als App-Fenster: maximiert, ohne Adressleiste und Tabs, aber mit
# Titelleiste – so lässt es sich minimieren und verkleinern (etwa um einen Lizenzschlüssel
# oder TSE-Zugangsdaten aus einer Mail zu kopieren). F11 schaltet auf Vollbild. Alt+F4 oder
# das X schließt das Fenster; bei der Kasse folgt die Verwaltung dieses PCs
# (kiosk-schleife.sh), danach der normale Schreibtisch – das Studio-Icon öffnet die Kasse wieder.
#
# Ein eigenes Firefox-Profil hält das App-Fenster von einem normal geöffneten Firefox getrennt:
# sonst landet die Adresse als Tab im vorhandenen Fenster. --name gibt dem Fenster die
# App-Kennung „studio“: Das Dock zeigt es unter dem Studio-Icon, und ein Klick darauf holt das
# minimierte Fenster zurück, statt ein zweites zu öffnen.
# Nicht über xdg-open oder gtk-launch: die kennen keinen Kiosk, und xdg-open lädt eine
# http-Adresse unter GNOME per gvfs womöglich in den Texteditor.

KIOSK_PROFIL=studio-kiosk
APP_NAME=studio

firefox_profil_anlegen() {
  # Der Snap legt Profile unter ~/snap/firefox/common/.mozilla ab; --CreateProfile weiß das selbst.
  # Liste der Profile nach dem Namen durchsuchen – zweimal anlegen ergäbe ein zweites Profil.
  for ini in "$HOME/snap/firefox/common/.mozilla/firefox/profiles.ini" "$HOME/.mozilla/firefox/profiles.ini"; do
    [ -f "$ini" ] && grep -qx "Name=$KIOSK_PROFIL" "$ini" && return 0
  done
  firefox --headless --CreateProfile "$KIOSK_PROFIL" >/dev/null 2>&1 || true
}

# Ordner des Kiosk-Profils aus profiles.ini (relativ oder absolut eingetragen).
firefox_profil_ordner() {
  for basis in "$HOME/snap/firefox/common/.mozilla/firefox" "$HOME/.mozilla/firefox"; do
    [ -f "$basis/profiles.ini" ] || continue
    ORDNER=$(awk -F= -v n="$KIOSK_PROFIL" -v basis="$basis" '
      function aus() { if (name == n && p != "") { print (rel == "0" ? p : basis "/" p); fertig = 1; exit } }
      /^\[/ { aus(); name = ""; p = ""; rel = "1"; next }
      $1 == "Name" { name = $2 } $1 == "Path" { p = $2 } $1 == "IsRelative" { rel = $2 }
      END { if (!fertig) aus() }' "$basis/profiles.ini")
    [ -n "$ORDNER" ] && [ -d "$ORDNER" ] && { echo "$ORDNER"; return 0; }
  done
  return 1
}

# App-Fenster statt Kiosk: Adressleiste und Tabs per userChrome.css ausblenden, Titelleiste
# des Systems (Minimieren, Maximieren, Schließen) einschalten, maximiert starten.
firefox_profil_einrichten() {
  firefox_profil_anlegen
  ORDNER=$(firefox_profil_ordner) || return 0
  mkdir -p "$ORDNER/chrome"
  cat >"$ORDNER/chrome/userChrome.css" <<'CSS'
/* Studio-Installer: App-Fenster ohne Adressleiste, Lesezeichen und Tabs */
#TabsToolbar, #nav-bar, #PersonalToolbar, #titlebar, #sidebar-main, #sidebar-box { visibility: collapse !important; }
CSS
  cat >"$ORDNER/user.js" <<'JS'
// Studio-Installer: App-Fenster (siehe /usr/lib/studio-installer/kiosk.sh)
user_pref("toolkit.legacyUserProfileCustomizations.stylesheets", true);
user_pref("browser.tabs.inTitlebar", 0);
user_pref("browser.toolbars.bookmarks.visibility", "never");
// Ohne Tab-Leiste wäre ein neuer Tab unsichtbar: Links mit Ziel _blank öffnen als eigenes Fenster.
user_pref("browser.link.open_newwindow", 2);
// Das Studio-Icon bei offenem Fenster lädt die Adresse in das vorhandene Fenster.
user_pref("browser.link.open_newwindow.override.external", 1);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("sidebar.revamp", false);
JS
  # Jeder Start maximiert – auch wenn das Fenster zuletzt verkleinert geschlossen wurde.
  python3 - "$ORDNER/xulstore.json" <<'PY' >/dev/null 2>&1 || true
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
try:
    d = json.loads(p.read_text())
except Exception:
    d = {}
d.setdefault("chrome://browser/content/browser.xhtml", {}).setdefault("main-window", {})["sizemode"] = "maximized"
p.write_text(json.dumps(d))
PY
}

starten() {
  # Als eigene, vorübergehende Benutzer-Unit: so überlebt der Browser das Ende des Aufrufers.
  # Der Autostart (studio-kiosk.service) ist oneshot – systemd beendet beim Abschluss alles,
  # was in seiner Gruppe noch läuft, und nähme ein einfach im Hintergrund gestartetes Firefox mit.
  if command -v systemd-run >/dev/null 2>&1 && [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    # Die Anzeige der Sitzung mitgeben – nicht jede Sitzung trägt sie in die Umgebung des
    # Benutzer-Managers ein.
    UMGEBUNG=""
    for v in DISPLAY WAYLAND_DISPLAY XAUTHORITY XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS; do
      eval "w=\${$v:-}"
      [ -n "$w" ] && UMGEBUNG="$UMGEBUNG --setenv=$v"
    done
    # shellcheck disable=SC2086 # UMGEBUNG ist eine Liste von Schaltern
    if systemd-run --user --quiet --collect $UMGEBUNG -- "$@" >/dev/null 2>&1; then
      return 0
    fi
  fi
  "$@" >/dev/null 2>&1 &
}

kiosk_oeffnen() {
  URL="$1"
  if command -v firefox >/dev/null 2>&1; then
    # Läuft das Kiosk-Profil schon, bekommt diese Instanz die Adresse (Firefox sucht die
    # laufende Instanz je Profil) – es entsteht kein zweites Fenster.
    kiosk_laeuft || firefox_profil_einrichten
    starten firefox --name "$APP_NAME" -P "$KIOSK_PROFIL" "$URL"
    return 0
  fi
  for chrom in chromium chromium-browser google-chrome; do
    if command -v "$chrom" >/dev/null 2>&1; then
      starten "$chrom" --start-maximized --no-first-run --noerrdialogs --disable-session-crashed-bubble \
        --class="$APP_NAME" --user-data-dir="$HOME/.config/studio-kiosk" "--app=$URL"
      return 0
    fi
  done
  # Kein bekannter Browser: wenigstens irgendwie öffnen.
  xdg-open "$URL"
}

# Läuft schon ein Kiosk-Fenster dieses Benutzers? Dann übernimmt es die Adresse (Firefox sucht
# die laufende Instanz je Profil), und der neue Aufruf ist sofort wieder fertig.
kiosk_laeuft() {
  pgrep -u "$(id -u)" -f -- "$KIOSK_PROFIL" >/dev/null 2>&1
}

# Wie kiosk_oeffnen, aber im Vordergrund: kehrt erst zurück, wenn das Fenster zu ist.
kiosk_vordergrund() {
  URL="$1"
  if command -v firefox >/dev/null 2>&1; then
    kiosk_laeuft || firefox_profil_einrichten
    firefox --name "$APP_NAME" -P "$KIOSK_PROFIL" "$URL" >/dev/null 2>&1
    return $?
  fi
  for chrom in chromium chromium-browser google-chrome; do
    if command -v "$chrom" >/dev/null 2>&1; then
      "$chrom" --start-maximized --no-first-run --noerrdialogs --disable-session-crashed-bubble \
        --class="$APP_NAME" --user-data-dir="$HOME/.config/$KIOSK_PROFIL" "--app=$URL" >/dev/null 2>&1
      return $?
    fi
  done
  return 2  # kein bekannter Browser
}

# ── Dienst studio-setup (Assistent bzw. Verwaltung) ───────────────────────────
# Das Token ist nur für root und die Gruppe sudo lesbar. Ohne Token (kein Administrator)
# gibt es kein Verwaltungsfenster – Alt+F4 führt dann wie früher auf den Schreibtisch.
SETUP=http://127.0.0.1:8099

setup_token() {
  cat "${STUDIO_SETUP_TOKEN:-/run/studio-setup/token}" 2>/dev/null
}

# setup_api <pfad> [json] – das Token geht per Kopfzeilen-Datei, nicht über die Befehlszeile.
setup_api() {
  T=$(setup_token)
  [ -n "$T" ] || return 1
  KOPF=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/studio-kopf.XXXXXX") || return 1
  printf 'X-Studio-Setup: 1\nCookie: studio_setup=%s\nContent-Type: application/json\n' "$T" >"$KOPF"
  if [ -n "${2:-}" ]; then
    curl -fs --max-time 5 -H @"$KOPF" -X POST -d "$2" "$SETUP$1"
  else
    curl -fs --max-time 5 -H @"$KOPF" "$SETUP$1"
  fi
  RC=$?
  rm -f "$KOPF"
  return $RC
}

ansicht_setzen() {
  setup_api /api/kiosk/ansicht "{\"ansicht\":\"$1\"}" >/dev/null 2>&1 || true
}

ansicht_lesen() {
  setup_api /api/kiosk/ansicht 2>/dev/null | sed -n 's/.*"ansicht": *"\([a-z]*\)".*/\1/p'
}

meldung() {
  notify-send "Studio" "$1" 2>/dev/null || echo "$1" >&2
}
