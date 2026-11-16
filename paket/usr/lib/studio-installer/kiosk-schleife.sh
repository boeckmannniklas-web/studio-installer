#!/bin/sh
# Kasse im Vollbild mit Verwaltungsfenster. Läuft als eigene Benutzer-Unit (starten in kiosk.sh):
#
#   Studio  --Alt+F4-->  Verwaltung dieses PCs  --Alt+F4-->  Schreibtisch
#
# Die Verwaltung zeigt der Dienst studio-setup. Ihr Knopf „Zurück zu Studio“ meldet dort die
# Ansicht „studio“ – so weiß die Schleife nach dem nächsten Schließen, wohin.
. /usr/lib/studio-installer/kiosk.sh

sitzung_lebt() {
  if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    case "$WAYLAND_DISPLAY" in
      /*) [ -S "$WAYLAND_DISPLAY" ] ;;
      *) [ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/$WAYLAND_DISPLAY" ] ;;
    esac
    return
  fi
  [ -n "${DISPLAY:-}" ]
}

ansicht_setzen studio
URL=http://localhost/
while :; do
  START=$(date +%s)
  kiosk_vordergrund "$URL"
  RC=$?
  if [ $RC -eq 2 ]; then kiosk_oeffnen "$URL"; exit 0; fi
  [ $RC -eq 0 ] || exit 0
  # Sehr kurz offen: Start gescheitert oder an ein schon offenes Kiosk-Fenster übergeben.
  [ $(( $(date +%s) - START )) -ge 3 ] || exit 0
  # Abmelden oder Herunterfahren schließt das Fenster auch – dann nichts mehr öffnen.
  sitzung_lebt || exit 0
  [ "$(ansicht_lesen)" = "studio" ] || exit 0
  T=$(setup_token)
  [ -n "$T" ] || exit 0
  ansicht_setzen verwaltung
  URL="$SETUP/?t=$T"
done
