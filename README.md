# Studio-Installer

Richtet einen PC im Studio als **Studio-Edge-Server** ein. Das ist der lokale Server, auf dem Kasse,
Solarium-Steuerung und Geräte laufen und der sich mit der Studio-Cloud abgleicht.

Es gibt den Installer in zwei Formen:

| Form | Für wen | Was man braucht |
|---|---|---|
| **Studio-Image** (USB-Stick) | neuer PC, wird komplett neu aufgesetzt | USB-Stick ab 8 GB, PC mit x86-64 |
| **Installer einzeln** | PC mit Ubuntu ab 24.04 | Administrator-Konto (sudo) |

Beide Formen enden am selben Ort: Auf dem Schreibtisch liegt das Icon **Studio**. Der erste Klick
darauf öffnet den Einrichtungs-Assistenten.

## Installer einzeln

```sh
curl -fsSL https://github.com/boeckmannniklas-web/studio-installer/releases/latest/download/install.sh | sudo bash
```

Alternativ das Paket `studio-installer.deb` aus den
[Releases](https://github.com/boeckmannniklas-web/studio-installer/releases/latest) laden und mit
`sudo apt install ./studio-installer.deb` installieren.

## Studio-Image

Das Image ist das offizielle Ubuntu 26.04 LTS Desktop mit Studio-Installer und automatischer
Installation.

1. Das Image auf einen USB-Stick schreiben, z. B. mit Ubuntu „Laufwerke“, balenaEtcher oder Rufus im DD-Modus.
2. Den PC vom Stick starten und den Eintrag **„Studio-PC installieren – LÖSCHT die gesamte Festplatte“**
   wählen. Die Installation läuft danach ohne Rückfragen. Internet wird dringend empfohlen, weil dabei
   gleich die Updates eingespielt werden.
3. Der PC startet neu und meldet sich automatisch als `studio` an.
   Das Anfangspasswort lautet `studio-start`. Der Assistent verlangt als ersten Schritt ein neues.

Das Image selbst baut `iso/baue-iso.sh`.

## Der Assistent

Er läuft lokal unter `http://127.0.0.1:8099` und führt durch diese Schritte:

1. **PC-Passwort** – nur beim Studio-Image.
2. **Internet** – prüft, ob Studio-Cloud und Software-Download erreichbar sind.
3. **Docker** – installiert Docker Engine und Compose.
4. **Lizenz** – den Studio-Lizenzschlüssel einfügen (er beginnt mit `STUDIO-1.`) oder aus einer
   Textdatei laden. Die erste Aktivierung bindet die Lizenz an diesen PC. Soll sie auf einen anderen PC
   umziehen, muss der Plattform-Betreiber das alte Gerät lösen.
5. **Fernwartung** – drei Wege:
   - Tailscale der Plattform,
   - ein eigener Tailscale-Schlüssel,
   - keine Fernwartung. Dann gibt es nur eingeschränkten Service.
6. **Konfiguration** – hat der Plattform-Betreiber eine Konfiguration hinterlegt, wird sie übernommen.
   Sonst fragt der Assistent Netzwerkkarte, feste LAN-Adresse und Marke ab. Passwörter und Schlüssel
   erzeugt er selbst. Die fertige `.env` liegt in `/opt/studio` und geht verschlüsselt an die Cloud.
7. **Installation** – lädt die freigegebene Studio-Version. Das Manifest ist von der Cloud signiert,
   die Images sind über ihren Digest festgelegt. Danach startet Studio, und der Assistent wartet auf
   Verbindung und Lizenz.
8. **Fertig** – zeigt die erste Anmeldung. Das Passwort muss danach geändert werden.

Danach öffnet das Icon direkt Studio. Den Assistenten holt `studio-einrichtung` zurück
(Administrator-Konto nötig).

### Sicherheit

- Der Assistent lauscht nur auf `127.0.0.1`. Er prüft den Host-Header und nimmt nur Anfragen mit dem
  Sitzungstoken aus `/run/studio-setup/token` an; lesen dürfen das root und die Gruppe `sudo`.
- Lizenz und Manifest werden gegen den öffentlichen Schlüssel der Cloud geprüft (`cloud-key.pub`).
- Die Zugangsdaten für den Software-Download landen nur in einem Wegwerf-Ordner, nie in `/root/.docker`.
  Den Tailscale-Anmeldeschlüssel entfernt der Assistent nach der Anmeldung aus der `.env`.

## Entwicklung

```sh
python3 -m unittest discover -s test     # Logik ohne root/Docker/Cloud
./baue-deb.sh 0.1.0                      # → dist/studio-installer.deb
iso/baue-iso.sh --deb dist/studio-installer.deb --test
test/vm-test.sh start vm1 ~/studio-os/studio-os-…-test.iso
```

Ein Release entsteht mit einem Tag `v<version>` (`.github/workflows/release.yml`).
