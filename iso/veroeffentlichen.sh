#!/usr/bin/env bash
# Studio-Image auf den Download-Server legen (https://<cloud>/downloads/) und „latest“ darauf zeigen lassen.
#
#   STUDIO_DOWNLOAD_ZIEL=root@<cloud>:/root/studio-downloads \
#   STUDIO_DOWNLOAD_SSH="ssh -i ~/.ssh/<schluessel>" \
#   iso/veroeffentlichen.sh ~/studio-os/studio-os-<version>-ubuntu-<ubuntu>.iso
#
# Fortsetzbar (rsync --partial). Auf dem Server wird die Prüfsumme kontrolliert, bevor „latest“
# umgestellt wird. Ältere Images werden dort gelöscht (Platz) – STUDIO_DOWNLOAD_BEHALTEN=1 verhindert das.
set -euo pipefail
ISO=$(realpath "${1:?Pfad zum ISO fehlt}")
ZIEL="${STUDIO_DOWNLOAD_ZIEL:?STUDIO_DOWNLOAD_ZIEL fehlt, z. B. root@host:/root/studio-downloads}"
SSH="${STUDIO_DOWNLOAD_SSH:-ssh}"
HOST="${ZIEL%%:*}"
PFAD="${ZIEL#*:}"
NAME=$(basename "$ISO")
BEHALTEN="${STUDIO_DOWNLOAD_BEHALTEN:-}"
[[ "$NAME" =~ ^studio-os-[0-9A-Za-z.-]+\.iso$ ]] || { echo "Unerwarteter Dateiname: $NAME" >&2; exit 1; }

cd "$(dirname "$ISO")"
echo "$(sha256sum "$NAME" | cut -d' ' -f1)  $NAME" > "$NAME.sha256"
rsync -a --partial --inplace --chmod=F644 --info=progress2 -e "$SSH" "$NAME" "$NAME.sha256" "$ZIEL/"

# shellcheck disable=SC2029  # Variablen sollen hier lokal eingesetzt werden
$SSH "$HOST" "set -e; cd '$PFAD'
sha256sum -c '$NAME.sha256'
ln -sfn '$NAME' studio-os-latest.iso
ln -sfn '$NAME.sha256' studio-os-latest.iso.sha256
if [ -z '$BEHALTEN' ]; then
  for f in studio-os-*.iso; do
    case \"\$f\" in '$NAME'|studio-os-latest.iso) ;; *) rm -f \"\$f\" \"\$f.sha256\" ;; esac
  done
fi
ls -la"
echo "veröffentlicht: $NAME"
