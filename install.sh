#!/bin/sh
# Studio-Installer auf einem vorhandenen Ubuntu (ab 24.04, x86-64) einrichten:
#
#   curl -fsSL https://github.com/boeckmannniklas-web/studio-installer/releases/latest/download/install.sh | sudo bash
#
# Installiert das Paket studio-installer und legt das Studio-Icon auf den Schreibtisch.
# Alles Weitere (Docker, Lizenz, Konfiguration) macht der Assistent beim ersten Klick.

# Alles in einer Funktion, die erst am Ende läuft: bricht der Download ab, passiert nichts halb.
studio_installer() {
  set -eu
  QUELLE="${STUDIO_INSTALLER_QUELLE:-https://github.com/boeckmannniklas-web/studio-installer/releases/latest/download}"

  if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte mit sudo ausführen:  curl -fsSL …/install.sh | sudo bash" >&2
    exit 1
  fi
  . /etc/os-release
  if [ "${ID:-}" != "ubuntu" ]; then
    echo "Nur für Ubuntu (gefunden: ${PRETTY_NAME:-unbekannt})." >&2
    exit 1
  fi
  case "${VERSION_ID:-0}" in
    2[4-9].*|[3-9][0-9].*) ;;
    *) echo "Ubuntu 24.04 oder neuer nötig (gefunden: ${VERSION_ID:-?})." >&2; exit 1 ;;
  esac
  if [ "$(dpkg --print-architecture)" != "amd64" ]; then
    echo "Studio läuft nur auf x86-64-PCs (amd64)." >&2
    exit 1
  fi

  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  echo "Lade den Studio-Installer …"
  curl -fsSL "$QUELLE/studio-installer.deb" -o "$TMP/studio-installer.deb"
  chmod 644 "$TMP/studio-installer.deb"
  # </dev/null: apt darf nicht den Rest dieses Skripts von stdin lesen.
  apt-get update -qq </dev/null
  DEBIAN_FRONTEND=noninteractive apt-get install -y "$TMP/studio-installer.deb" </dev/null

  # Icon für den Benutzer, der sudo aufgerufen hat – in dessen Sitzung.
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    BENUTZER_ID=$(id -u "$SUDO_USER")
    HEIM=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    if [ -S "/run/user/$BENUTZER_ID/bus" ]; then
      runuser -u "$SUDO_USER" -- env HOME="$HEIM" XDG_RUNTIME_DIR="/run/user/$BENUTZER_ID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$BENUTZER_ID/bus" \
        /usr/lib/studio-installer/schreibtisch.sh </dev/null || true
    fi
  fi

  echo
  echo "Fertig. Auf dem Schreibtisch liegt jetzt das Studio-Icon – ein Klick startet die Einrichtung."
  echo "PC ohne Bildschirm? Von einem anderen Rechner:  ssh -L 8099:127.0.0.1:8099 <benutzer>@<dieser-pc>"
  echo "und dann im Browser dort:  http://127.0.0.1:8099/?t=\$(sudo cat /run/studio-setup/token)"
}

studio_installer "$@"
