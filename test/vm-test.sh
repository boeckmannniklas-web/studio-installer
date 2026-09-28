#!/usr/bin/env bash
# Test-VM mit QEMU/KVM (UEFI). Braucht: sudo apt install qemu-system-x86 ovmf
#
#   test/vm-test.sh start  <name> <iso> [uuid]   neue VM, bootet vom ISO (installiert sich selbst)
#   test/vm-test.sh boot   <name>                vorhandene VM von der Platte starten
#   test/vm-test.sh stop   <name>                sauber herunterfahren (ACPI), sonst nach 60 s hart
#   test/vm-test.sh foto   <name> [datei.png]    Bildschirmfoto über QMP
#   test/vm-test.sh sichern <name> <stand>       Platte kopieren (VM muss aus sein)
#   test/vm-test.sh zurueck <name> <stand>       Platte zurückspielen
#   test/vm-test.sh ssh    <name> [befehl]       per SSH in die VM (Schlüssel aus dem --test-ISO)
#
# Jede VM bekommt eine feste SMBIOS-UUID – daraus bildet der Installer die Hardware-ID. Zwei
# VMs mit verschiedenen UUIDs sind für die Cloud zwei verschiedene PCs.
# Bildschirm: VNC auf 127.0.0.1:590<n> (in VS Code den Port weiterleiten), SSH auf 127.0.0.1:22<n>2.
set -euo pipefail
VMS="${VMS:-$HOME/studio-vms}"
befehl="${1:-}"; name="${2:-}"
[ -n "$befehl" ] && [ -n "$name" ] || { sed -n '2,15p' "$0"; exit 1; }
VM="$VMS/$name"
nummer() { case "$name" in *2) echo 2 ;; *3) echo 3 ;; *) echo 1 ;; esac; }
N=$(nummer)
SSH_PORT="22${N}2"
qmp() { python3 - "$VM/qmp.sock" "$@" <<'PY'
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); f = s.makefile("rw")
f.readline(); f.write(json.dumps({"execute": "qmp_capabilities"}) + "\n"); f.flush(); f.readline()
cmd = {"execute": sys.argv[2]}
if len(sys.argv) > 3: cmd["arguments"] = json.loads(sys.argv[3])
f.write(json.dumps(cmd) + "\n"); f.flush(); print(f.readline().strip())
PY
}
laeuft() { [ -f "$VM/qemu.pid" ] && kill -0 "$(cat "$VM/qemu.pid")" 2>/dev/null; }

starten() {
  local cdrom=("$@")
  OVMF_CODE=$(ls /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd 2>/dev/null | head -1)
  [ -n "$OVMF_CODE" ] || { echo "OVMF fehlt: sudo apt install ovmf" >&2; exit 1; }
  qemu-system-x86_64 -enable-kvm -machine q35 -cpu host -smp 4 -m 6144 \
    -drive if=pflash,format=raw,readonly=on,file="$OVMF_CODE" \
    -drive if=pflash,format=raw,file="$VM/OVMF_VARS.fd" \
    -drive file="$VM/platte.qcow2",if=virtio,format=qcow2 \
    "${cdrom[@]}" \
    -smbios type=1,uuid="$(cat "$VM/uuid")" \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 -device virtio-net-pci,netdev=n0 \
    -device qemu-xhci -device usb-tablet -vga virtio -display none -vnc 127.0.0.1:"$N" \
    -qmp unix:"$VM/qmp.sock",server,nowait -daemonize -pidfile "$VM/qemu.pid"
  echo "VM $name läuft: VNC 127.0.0.1:590$N, SSH -p $SSH_PORT studio@127.0.0.1"
}

case "$befehl" in
  start)
    iso="${3:?ISO fehlt}"; uuid="${4:-$(cat /proc/sys/kernel/random/uuid)}"
    laeuft && { echo "läuft schon" >&2; exit 1; }
    mkdir -p "$VM"
    echo "$uuid" > "$VM/uuid"
    OVMF_VARS=$(ls /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd 2>/dev/null | head -1)
    cp "$OVMF_VARS" "$VM/OVMF_VARS.fd"
    rm -f "$VM/platte.qcow2" && qemu-img create -q -f qcow2 "$VM/platte.qcow2" 40G
    starten -cdrom "$iso" -boot order=dc ;;
  boot)
    laeuft && { echo "läuft schon" >&2; exit 1; }
    starten ;;
  stop)
    laeuft || { echo "läuft nicht"; exit 0; }
    qmp system_powerdown >/dev/null
    for _ in $(seq 60); do laeuft || { echo "aus"; exit 0; }; sleep 1; done
    qmp quit >/dev/null || kill "$(cat "$VM/qemu.pid")"; echo "hart beendet" ;;
  foto)
    ziel="${3:-$VM/foto.png}"
    qmp screendump "{\"filename\": \"$VM/foto.ppm\"}" >/dev/null; sleep 1
    python3 -c "
import sys
from PIL import Image
Image.open('$VM/foto.ppm').save('$ziel')" 2>/dev/null || cp "$VM/foto.ppm" "${ziel%.png}.ppm"
    echo "$ziel" ;;
  sichern)
    laeuft && { echo "erst stoppen" >&2; exit 1; }
    cp --sparse=always "$VM/platte.qcow2" "$VM/platte-${3:?Stand fehlt}.qcow2"; cp "$VM/OVMF_VARS.fd" "$VM/OVMF_VARS-$3.fd"; echo "gesichert: $3" ;;
  zurueck)
    laeuft && { echo "erst stoppen" >&2; exit 1; }
    cp --sparse=always "$VM/platte-${3:?Stand fehlt}.qcow2" "$VM/platte.qcow2"; cp "$VM/OVMF_VARS-$3.fd" "$VM/OVMF_VARS.fd"; echo "zurückgespielt: $3" ;;
  ssh)
    shift 2
    exec ssh -i "${TEST_SCHLUESSEL:-$HOME/.ssh/studio_vm_test}" -p "$SSH_PORT" -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR studio@127.0.0.1 "$@" ;;
  *) sed -n '2,15p' "$0"; exit 1 ;;
esac
