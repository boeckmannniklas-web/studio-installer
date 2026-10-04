# Studio-Installer

Richtet einen PC im Studio als **Studio-Edge-Server** ein. Das ist der lokale Server, auf dem Kasse,
Solarium-Steuerung und Geräte laufen und der sich mit der Studio-Cloud abgleicht.

Es gibt den Installer in zwei Formen:

| Form | Für wen | Was man braucht |
|---|---|---|
| **Studio-Image** (USB-Stick) | neuer PC, wird komplett neu aufgesetzt | USB-Stick ab 8 GB, PC mit x86-64 |
| **Installer einzeln** | PC mit Ubuntu ab 24.04 | Administrator-Konto (sudo) |

Beide Formen enden am selben Ort: Auf dem Schreibtisch liegt das Icon **Studio**. Der erste Klick
darauf öffnet den Einrichtungs-Assistenten. Beim Studio-Image öffnet er sich nach der Anmeldung von selbst.

## Kioskmodus

Assistent und Studio öffnen im **Kioskmodus**: Vollbild ohne Adressleiste und Tabs (Firefox `--kiosk`
mit eigenem Profil `studio-kiosk`, Richtlinien in `/etc/firefox/policies/policies.json`).

- Auf dem Studio-Image startet Studio nach der Anmeldung automatisch (Benutzer-Unit `studio-kiosk.service`,
  nur wenn `/etc/studio-os` existiert). Bei einem Einzeiler-PC öffnet erst das Icon.
- **Alt+F4** schließt das Vollbild, darunter liegt der normale Schreibtisch. Das Icon öffnet es wieder.
- Nach dem Hochfahren wartet der Starter, bis Studio antwortet. Nach einem Update lädt die Kasse im
  Leerlauf von selbst neu.

## Installer einzeln

```sh
curl -fsSL https://github.com/boeckmannniklas-web/studio-installer/releases/latest/download/install.sh | sudo bash
```

Alternativ das Paket `studio-installer.deb` aus den
[Releases](https://github.com/boeckmannniklas-web/studio-installer/releases/latest) laden und mit
`sudo apt install ./studio-installer.deb` installieren.

## Studio-Image

Das Image ist das offizielle Ubuntu 26.04 LTS Desktop mit Studio-Installer und automatischer
Installation. Download: <https://ki-sunlounge.de/downloads/studio-os-latest.iso>
(etwa 6,5 GB; Prüfsummen unter <https://ki-sunlounge.de/downloads/>).

1. Das Image auf einen USB-Stick schreiben, z. B. mit Ubuntu „Laufwerke“, balenaEtcher oder Rufus im DD-Modus.
2. Den PC vom Stick starten und den Eintrag **„Studio-PC installieren – LÖSCHT die gesamte Festplatte“**
   wählen. Die Installation läuft danach ohne Rückfragen. Internet wird dringend empfohlen, weil dabei
   gleich die Updates eingespielt werden.
3. Der PC startet neu und meldet sich automatisch als `studio` an.
   Das Anfangspasswort lautet `studio-start`. Der Assistent verlangt als ersten Schritt ein neues.

Das Image selbst baut `iso/baue-iso.sh`; `iso/veroeffentlichen.sh` legt es auf den Download-Server.

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
   Dazu **Updates**: automatisch im Wartungsfenster (Standard, 03:00–05:00) oder manuell. Der
   SystemOwner kann das später im Studio umstellen (Grundeinstellungen → Updates).
7. **Speicher & Sicherung**:
   - **Datenspeicher** – Systemplatte (Standard) oder eine eigene Platte (zweite interne oder USB).
     Eine eigene Platte wird nach Eintippen ihres Namens formatiert (ext4, Label `studio-daten`) und
     unter `/srv/studio-daten` eingehängt. Fehlt sie beim Start, startet Docker nicht, statt auf die
     Systemplatte zu schreiben. Eine Netzwerkfestplatte ist als Datenspeicher nicht vorgesehen.
   - **Backup-Ziel vor Ort** – später, USB-Platte, Netzwerkfestplatte per SMB oder NFS. Mit „Testen“
     (schreiben, lesen, freier Platz). Eingehängt per automount unter `/srv/studio-backup`.
   - **Sicherungsschlüssel** – ein age-Schlüssel je Studio. Der private Teil erscheint einmal als
     **Wiederherstellungsblatt** zum Drucken. Die Plattform bewahrt eine verschlüsselte Kopie auf und gibt
     sie nur nach eigener Freigabe heraus.
   - **Neu beginnen oder aus einer Sicherung wiederherstellen** – aus der Cloud oder vom Backup-Ziel.
8. **Installation** – lädt die freigegebene Studio-Version. Das Manifest ist von der Cloud signiert,
   die Images sind über ihren Digest festgelegt. Danach startet Studio, und der Assistent wartet auf
   Verbindung und Lizenz. Bei einer Wiederherstellung spielt er vorher Datenbank und Dateien ein. Studio
   startet dann gesperrt, bis der SystemOwner unter System → Backup den Abgleich mit Cloud und TSE
   abgeschlossen hat.
9. **Fertig** – zeigt die erste Anmeldung. Das Passwort muss danach geändert werden.

Danach öffnet das Icon direkt Studio. Den Assistenten holt `studio-einrichtung` zurück
(Administrator-Konto nötig). Nach der Installation lässt sich dort das Backup-Ziel ändern.

## Betrieb: Sicherung und Updates

`studio-betrieb` (als root, gesteuert von systemd) übernimmt den Betrieb:

| Unit | Wann | Was |
|---|---|---|
| `studio-sicherung.timer` | nachts 02:30 (verpasst: beim nächsten Start) | `studio-betrieb sichern --anlass nacht` |
| `studio-update.timer` | alle 30 min | nach Updates fragen, im Wartungsfenster einspielen |
| `studio-auftrag.path` | sobald Studio etwas anfordert | „Jetzt sichern“, „Nach Updates suchen“, „Jetzt aktualisieren“ … |

- **Sicherung:** `pg_dump` + Dateien aus `DATA_DIR` + Konfiguration, verschlüsselt mit age. Sie geht ans
  Backup-Ziel vor Ort (aufbewahrt 7 Tage, 4 Wochen, 12 Monate) und in die Cloud (dort 14 Tage). Außerdem
  sichert Studio nach jedem Kassenabschluss. Einmal pro Woche läuft eine Probe-Wiederherstellung in einem
  Wegwerf-Container.
- **Update:** Es kommt nur eine von der Cloud signierte, für den Ring des Studios freigegebene Version.
  Läuft ein Verkauf, startet kein Update. Vorher wird gesichert. Antwortet die neue Version nicht, geht es
  auf die alte zurück, notfalls samt Datenbank. Auch dieses Paket aktualisiert sich selbst (Prüfsumme aus
  `SHA256SUMS`).
- **Betriebssystem:** Sicherheitsupdates laufen über unattended-upgrades. Ein Neustart und Snap-Updates
  (Firefox im Kiosk) laufen nur im Wartungsfenster.
- Studio im Container und dieser Dienst reden über `/var/lib/studio-austausch` (im Container
  `/data/host`). Das bindet `/opt/studio/compose.lokal.yml` ein, die der Installer erzeugt.

Von Hand: `sudo studio-betrieb sichern`, `sudo studio-betrieb update-pruefen`, `sudo studio-betrieb status`.

### Sicherheit

- Der Assistent lauscht nur auf `127.0.0.1`. Er prüft den Host-Header und nimmt nur Anfragen mit dem
  Sitzungstoken aus `/run/studio-setup/token` an; lesen dürfen das root und die Gruppe `sudo`.
- Lizenz und Manifest werden gegen den öffentlichen Schlüssel der Cloud geprüft (`cloud-key.pub`).
- Die Zugangsdaten für den Software-Download landen nur in einem Wegwerf-Ordner, nie in `/root/.docker`.
  Den Tailscale-Anmeldeschlüssel entfernt der Assistent nach der Anmeldung aus der `.env`.

## Entwicklung

```sh
python3 -m unittest discover -s test     # Logik ohne root/Docker/Cloud
./baue-deb.sh 0.2.0                      # → dist/studio-installer.deb
iso/baue-iso.sh --deb dist/studio-installer.deb --test
ZWEITE_PLATTE=1 test/vm-test.sh start vm1 ~/studio-os/studio-os-…-test.iso
```

Ein Release entsteht mit einem Tag `v<version>` (`.github/workflows/release.yml`).
