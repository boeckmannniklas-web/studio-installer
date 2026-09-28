#!/usr/bin/env bash
# Studio-Image bauen: offizielles Ubuntu 26.04 Desktop + Studio-Installer + Autoinstall.
#
#   iso/baue-iso.sh                  .deb aus dem neuesten GitHub-Release
#   iso/baue-iso.sh --deb dist/studio-installer.deb
#   iso/baue-iso.sh --test           zusätzlich eigener SSH-Schlüssel, GRUB startet nach 5 s von selbst
#   iso/baue-iso.sh --ohne-studio    reines Ubuntu mit Autoinstall (zum Testen des Installers einzeln)
#
# Ergebnis: $ARBEIT/studio-os-<version>.iso (ca. 6,5 GB). Braucht xorriso, gpg, curl, openssl, python3-yaml.
# ACHTUNG: Der erste GRUB-Eintrag löscht die ganze Festplatte des Ziel-PCs.
set -euo pipefail
HIER=$(cd "$(dirname "$0")" && pwd)
ARBEIT="${ARBEIT:-$HOME/studio-os}"
UBUNTU_VERSION="${UBUNTU_VERSION:-26.04.1}"
UBUNTU_REIHE="${UBUNTU_VERSION%.*}"                     # 26.04
ISO_NAME="ubuntu-${UBUNTU_VERSION}-desktop-amd64.iso"
QUELLE="https://releases.ubuntu.com/${UBUNTU_REIHE}"
# Das ISO selbst von einem schnellen Spiegel; die Prüfsummen kommen signiert von Ubuntu (oben).
ISO_QUELLE="${ISO_QUELLE:-https://ftp.fau.de/ubuntu-releases/${UBUNTU_REIHE}}"
# Ubuntu CD Image Automatic Signing Key (2012)
UBUNTU_CD_SCHLUESSEL="843938DF228D22F7B3742BC0D94AA3F0EFE21092"
ANFANGSPASSWORT="${STUDIO_ANFANGSPASSWORT:-studio-start}"
RELEASE_DEB="https://github.com/boeckmannniklas-web/studio-installer/releases/latest/download/studio-installer.deb"

DEB="" TEST=0 OHNE_STUDIO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --deb) DEB="$(realpath "$2")"; shift 2 ;;
    --test) TEST=1; shift ;;
    --ohne-studio) OHNE_STUDIO=1; shift ;;
    *) echo "Unbekannt: $1" >&2; exit 1 ;;
  esac
done
for p in xorriso gpg gpgv curl openssl python3 sha256sum; do
  command -v "$p" >/dev/null || { echo "Fehlt: $p" >&2; exit 1; }
done

mkdir -p "$ARBEIT"
cd "$ARBEIT"

echo "== Ubuntu ${UBUNTU_VERSION} laden und prüfen"
if [ ! -f "$ISO_NAME" ]; then
  curl -fL -C - --progress-bar -o "$ISO_NAME.teil" "$ISO_QUELLE/$ISO_NAME"
  mv "$ISO_NAME.teil" "$ISO_NAME"
fi
curl -fsSL -o SHA256SUMS "$QUELLE/SHA256SUMS"
curl -fsSL -o SHA256SUMS.gpg "$QUELLE/SHA256SUMS.gpg"
SCHLUESSELRING="$ARBEIT/ubuntu-cd.gpg"
if [ ! -s "$SCHLUESSELRING" ]; then
  GNUPGHOME=$(mktemp -d); export GNUPGHOME
  gpg --batch --keyserver hkps://keyserver.ubuntu.com --recv-keys "$UBUNTU_CD_SCHLUESSEL"
  gpg --batch --export "$UBUNTU_CD_SCHLUESSEL" > "$SCHLUESSELRING"
  unset GNUPGHOME
fi
gpgv --keyring "$SCHLUESSELRING" SHA256SUMS.gpg SHA256SUMS
grep " \*\?${ISO_NAME}\$" SHA256SUMS | sed 's/ \*/  /' | sha256sum -c -

BAU=$(mktemp -d)
trap 'rm -rf "$BAU"' EXIT

echo "== Studio-Installer"
VERSION="ubuntu-${UBUNTU_VERSION}"
if [ "$OHNE_STUDIO" -eq 0 ]; then
  mkdir -p "$BAU/studio"
  if [ -n "$DEB" ]; then cp "$DEB" "$BAU/studio/studio-installer.deb"
  else curl -fsSL -o "$BAU/studio/studio-installer.deb" "$RELEASE_DEB"; fi
  INSTALLER_VERSION=$(dpkg-deb -f "$BAU/studio/studio-installer.deb" Version)
  VERSION="${INSTALLER_VERSION}-ubuntu-${UBUNTU_VERSION}"
  echo "   studio-installer $INSTALLER_VERSION"
fi

echo "== autoinstall.yaml"
SSH_DATEIEN=()
[ -f "$HIER/support-schluessel.pub" ] && SSH_DATEIEN+=("$HIER/support-schluessel.pub")
if [ "$TEST" -eq 1 ]; then
  for k in "${TEST_SCHLUESSEL:-}" "$HOME"/.ssh/id_ed25519.pub "$HOME"/.ssh/id_rsa.pub; do
    [ -n "$k" ] && [ -f "$k" ] && SSH_DATEIEN+=("$k") && break
  done
fi
PASSWORT_HASH=$(openssl passwd -6 "$ANFANGSPASSWORT")
OHNE_STUDIO=$OHNE_STUDIO PASSWORT_HASH=$PASSWORT_HASH VERSION=$VERSION python3 - "$HIER/autoinstall.yaml" "$BAU/autoinstall.yaml" "${SSH_DATEIEN[@]}" <<'PY'
import json, os, sys, yaml
quelle, ziel, *schluesseldateien = sys.argv[1:]
text = open(quelle).read()
schluessel = [z.strip() for d in schluesseldateien for z in open(d) if z.strip() and not z.startswith("#")]
text = (text.replace("@PASSWORT_HASH@", os.environ["PASSWORT_HASH"])
            .replace("@SSH_SCHLUESSEL@", json.dumps(schluessel))
            .replace("@VERSION@", os.environ["VERSION"]))
daten = yaml.safe_load(text)                      # prüft die YAML-Syntax
if os.environ["OHNE_STUDIO"] == "1":
    ai = daten["autoinstall"]
    ai["late-commands"] = [c for c in ai["late-commands"] if "studio-installer" not in c and "/etc/studio-os" not in c]
    text = yaml.safe_dump(daten, allow_unicode=True, sort_keys=False, width=200)
open(ziel, "w").write(text)
print(f"   {len(schluessel)} SSH-Schlüssel")
PY

echo "== GRUB-Eintrag"
xorriso -osirrox on -indev "$ISO_NAME" -extract /boot/grub/grub.cfg "$BAU/grub.orig.cfg" 2>/dev/null
chmod u+w "$BAU/grub.orig.cfg"
WARTEN=-1; [ "$TEST" -eq 1 ] && WARTEN=5
TITEL="Studio-PC installieren – LÖSCHT die gesamte Festplatte"
[ "$OHNE_STUDIO" -eq 1 ] && TITEL="Ubuntu automatisch installieren (Test) – LÖSCHT die Festplatte"
python3 - "$BAU/grub.orig.cfg" "$BAU/grub.cfg" "$WARTEN" "$TITEL" <<'PY'
import re, sys
quelle, ziel, warten, titel = sys.argv[1:]
cfg = open(quelle).read()
linux = re.search(r"^\s*linux\s+(\S+)\s*(.*)$", cfg, re.M)
initrd = re.search(r"^\s*initrd\s+(\S+)", cfg, re.M)
if not linux or not initrd:
    sys.exit("grub.cfg: kein linux/initrd-Eintrag gefunden")
parameter = linux.group(2).replace("---", "").strip()
eintrag = (f'menuentry "{titel}" {{\n\tset gfxpayload=keep\n'
           f'\tlinux\t{linux.group(1)} autoinstall {parameter} ---\n\tinitrd\t{initrd.group(1)}\n}}\n')
cfg = re.sub(r"^set timeout=.*$", f"set timeout={warten}", cfg, flags=re.M)
if "set timeout=" not in cfg:
    cfg = f"set timeout={warten}\n" + cfg
cfg = re.sub(r"^set default=.*$", "", cfg, flags=re.M)
erster = cfg.find("menuentry")
cfg = cfg[:erster] + "set default=0\n" + eintrag + "\n" + cfg[erster:]
open(ziel, "w").write(cfg)
PY

echo "== ISO schreiben"
AUS="$ARBEIT/studio-os-${VERSION}.iso"
[ "$TEST" -eq 1 ] && AUS="$ARBEIT/studio-os-${VERSION}-test.iso"
[ "$OHNE_STUDIO" -eq 1 ] && AUS="$ARBEIT/ubuntu-autoinstall-${UBUNTU_VERSION}-test.iso"
rm -f "$AUS"
ABBILDUNGEN=(-map "$BAU/autoinstall.yaml" /autoinstall.yaml -map "$BAU/grub.cfg" /boot/grub/grub.cfg)
[ "$OHNE_STUDIO" -eq 0 ] && ABBILDUNGEN+=(-map "$BAU/studio" /studio)
xorriso -indev "$ISO_NAME" -outdev "$AUS" "${ABBILDUNGEN[@]}" -boot_image any replay 2>&1 | grep -Ev '^xorriso : (UPDATE|NOTE)' || true
sha256sum "$AUS" | tee "$AUS.sha256"
echo "fertig: $AUS"
