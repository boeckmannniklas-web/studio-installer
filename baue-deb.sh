#!/usr/bin/env bash
# .deb bauen:  ./baue-deb.sh [version]
#   → dist/studio-installer_<version>_all.deb und dist/studio-installer.deb (fester Name für „latest“)
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${1:-$(sed -n 's/^Version: //p' paket/DEBIAN/control)}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Version wie 0.1.0 erwartet, nicht '$VERSION'" >&2; exit 1; }

BAU=$(mktemp -d)
trap 'rm -rf "$BAU"' EXIT
WURZEL="$BAU/studio-installer"
cp -a paket "$WURZEL"
find "$WURZEL" -name __pycache__ -prune -exec rm -rf {} +
sed -i "s/^Version: .*/Version: $VERSION/" "$WURZEL/DEBIAN/control"
echo "$VERSION" > "$WURZEL/opt/studio-installer/VERSION"

# Rechte fest setzen, unabhängig von der umask des Bau-Rechners
find "$WURZEL" -type d -exec chmod 755 {} +
find "$WURZEL" -type f -exec chmod 644 {} +
chmod 755 "$WURZEL"/DEBIAN/{postinst,prerm,postrm} "$WURZEL"/usr/bin/* \
  "$WURZEL"/usr/lib/studio-installer/*.sh "$WURZEL"/opt/studio-installer/setup.py

mkdir -p dist
dpkg-deb --root-owner-group -Zxz --build "$WURZEL" "dist/studio-installer_${VERSION}_all.deb"
cp "dist/studio-installer_${VERSION}_all.deb" dist/studio-installer.deb
echo "gebaut: dist/studio-installer_${VERSION}_all.deb"
