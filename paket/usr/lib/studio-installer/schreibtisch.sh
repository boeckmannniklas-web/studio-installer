#!/bin/sh
# Einmal je Benutzer: Studio-Icon auf den Schreibtisch (als vertrauenswürdig) und ins Dock.
MARKE="${XDG_CONFIG_HOME:-$HOME/.config}/studio-installer/schreibtisch-erledigt"
[ -f "$MARKE" ] && exit 0
SCHREIBTISCH=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")
mkdir -p "$SCHREIBTISCH"
cp /usr/share/applications/studio.desktop "$SCHREIBTISCH/studio.desktop"
chmod +x "$SCHREIBTISCH/studio.desktop"
gio set "$SCHREIBTISCH/studio.desktop" metadata::trusted true 2>/dev/null || true
if command -v gsettings >/dev/null 2>&1; then
  FAV=$(gsettings get org.gnome.shell favorite-apps 2>/dev/null || echo "")
  case "$FAV" in
    *studio.desktop*|"") ;;
    "@as []") gsettings set org.gnome.shell favorite-apps "['studio.desktop']" ;;
    *) gsettings set org.gnome.shell favorite-apps "$(echo "$FAV" | sed "s/^\[/['studio.desktop', /")" ;;
  esac
fi
mkdir -p "$(dirname "$MARKE")" && touch "$MARKE"
