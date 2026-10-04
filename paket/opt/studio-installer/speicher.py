"""Wo Studio seine Daten ablegt und wohin es sichert. Läuft als root (studio-setup, studio-betrieb).

Zwei getrennte Fragen, mit Absicht:

  Datenspeicher  Hier liegt die laufende Datenbank. Nur lokal: die Systemplatte oder eine
                 eigene Platte (zweite interne oder per USB). Eine Datenbank auf einer
                 Netzwerkfreigabe steht bei jedem Netzaussetzer – und kann dabei kaputtgehen.
  Backup-Ziel    Hierhin gehen die nächtlichen Sicherungen: eine USB-Platte oder eine
                 Netzwerkfestplatte (SMB oder NFS). Fällt das Ziel aus, läuft die Kasse weiter.

Umgesetzt wird beides ohne Änderung am Studio-Paket: der Installer legt neben compose.yml
eine eigene compose.lokal.yml, die die Daten-Volumes durch Ordner auf dem gewählten Speicher
ersetzt (Compose führt Volumes über den Pfad im Container zusammen). Dazu kommt immer der
Austauschordner, über den Studio und die Dienste auf dem PC (Sicherung, Updates) reden.

Eine bestehende Installation mit Docker-Volumes wird nie umgehängt: dort zeigte ein
leerer Ordner sonst plötzlich ein leeres Studio. Sie behält ihre Volumes (Art „bestand“).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import schritte
from schritte import Fehler, _ausfuehren, _sicher_schreiben

ETC = Path(os.environ.get("STUDIO_ETC", "/etc/studio"))
FSTAB = Path(os.environ.get("STUDIO_FSTAB", "/etc/fstab"))
DOCKER_DROPIN = Path(os.environ.get("STUDIO_DOCKER_DROPIN", "/etc/systemd/system/docker.service.d/studio-daten.conf"))
AUSTAUSCH = Path(os.environ.get("STUDIO_AUSTAUSCH", "/var/lib/studio-austausch"))

SYSTEM_DATEN = Path("/var/lib/studio")
PLATTE_DATEN = Path("/srv/studio-daten")
BACKUP_PFAD = Path(os.environ.get("STUDIO_BACKUP_PFAD", "/srv/studio-backup"))
LABEL_DATEN = "studio-daten"
LABEL_BACKUP = "studio-backup"
UNTERORDNER = {"db": "/var/lib/postgresql/data", "api": "/data", "mqtt": "/mosquitto/data",
               "tailscale": "/var/lib/tailscale"}
DIENSTE = {"db": "db", "api": "api", "mqtt": "mqtt", "tailscale": "tailscale"}


def _json_lesen(pfad: Path) -> dict:
    try:
        return json.loads(pfad.read_text())
    except (OSError, ValueError):
        return {}


def _json_schreiben(pfad: Path, daten: dict, modus: int = 0o600) -> None:
    _sicher_schreiben(pfad, json.dumps(daten, indent=2, ensure_ascii=False) + "\n", modus)


def speicher_config() -> dict:
    return _json_lesen(ETC / "speicher.json")


def sicherung_config() -> dict:
    return _json_lesen(ETC / "sicherung.json")


def sicherung_config_schreiben(daten: dict) -> None:
    _json_schreiben(ETC / "sicherung.json", daten)


# ── Platten ───────────────────────────────────────────────────────────────────
LSBLK = ["lsblk", "-J", "-b", "-o",
         "NAME,PATH,SIZE,TYPE,TRAN,MODEL,SERIAL,FSTYPE,LABEL,UUID,MOUNTPOINTS,RM,HOTPLUG"]


def _lsblk() -> list[dict]:
    aus = subprocess.run(LSBLK, capture_output=True, text=True).stdout
    try:
        return json.loads(aus or "{}").get("blockdevices", [])
    except ValueError:
        return []


def _alle(geraet: dict):
    yield geraet
    for kind in geraet.get("children") or []:
        yield from _alle(kind)


def _einhaengepunkte(geraet: dict) -> set[str]:
    return {m for g in _alle(geraet) for m in (g.get("mountpoints") or []) if m}


def platten_auswerten(geraete: list[dict]) -> list[dict]:
    """Ganze Platten mit Einordnung. Die Systemplatte und der Installations-Stick sind dabei,
    aber gesperrt – damit niemand sie versehentlich formatiert."""
    ergebnis = []
    for g in geraete:
        if g.get("type") != "disk" or (g.get("name") or "").startswith(("loop", "zram", "sr", "ram")):
            continue
        punkte = _einhaengepunkte(g)
        teile = [t for t in _alle(g) if t is not g]
        labels = {t.get("label") for t in teile if t.get("label")}
        system = bool(punkte & {"/", "/boot", "/boot/efi", "/usr", "/var"}) or any(
            p.startswith("/var/lib/docker") for p in punkte)
        stick = any(t.get("fstype") == "iso9660" for t in _alle(g)) or "/cdrom" in punkte
        daten_teil = next((t for t in teile if t.get("label") == LABEL_DATEN and t.get("fstype") == "ext4"), None)
        backup_teil = next((t for t in teile if t.get("label") == LABEL_BACKUP), None)
        ergebnis.append({
            "name": g.get("name"), "pfad": g.get("path") or f"/dev/{g.get('name')}",
            "groesse": int(g.get("size") or 0), "anschluss": (g.get("tran") or "").lower(),
            "modell": (g.get("model") or "").strip(), "seriennummer": (g.get("serial") or "").strip(),
            "wechselbar": bool(g.get("rm")) or bool(g.get("hotplug")) or (g.get("tran") or "") == "usb",
            "system": system, "installationsmedium": stick,
            "gesperrt": system or stick,
            "labels": sorted(labels), "eingehaengt": sorted(punkte),
            "studio_daten": daten_teil and {"pfad": daten_teil.get("path"), "uuid": daten_teil.get("uuid")},
            "studio_backup": backup_teil and {"pfad": backup_teil.get("path"), "uuid": backup_teil.get("uuid"),
                                              "fstype": backup_teil.get("fstype")},
        })
    return ergebnis


def platten() -> list[dict]:
    return platten_auswerten(_lsblk())


def _platte(name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9]{2,16}", name or ""):
        raise Fehler("Unbekannte Platte.")
    for p in platten():
        if p["name"] == name:
            if p["gesperrt"]:
                raise Fehler("Diese Platte trägt das System oder ist der Installations-Stick – sie bleibt unangetastet.")
            return p
    raise Fehler(f"Die Platte {name} ist nicht (mehr) da. Steckt sie noch?")


def _partition_von(platte: str) -> dict:
    """Erste Partition einer Platte nach dem Partitionieren."""
    _ausfuehren(["udevadm", "settle"], pruefen=False)
    for g in _lsblk():
        if g.get("name") == platte:
            kinder = g.get("children") or []
            if kinder:
                return kinder[0]
    raise Fehler("Nach dem Partitionieren ist keine Partition zu sehen.")


def formatieren(name: str, label: str, log) -> dict:
    """Platte komplett löschen und mit einer ext4-Partition neu anlegen."""
    p = _platte(name)
    if p["eingehaengt"]:
        raise Fehler(f"Die Platte ist noch eingehängt ({', '.join(p['eingehaengt'])}). Bitte erst auswerfen.")
    log(f"Lösche {p['pfad']} ({p['modell'] or 'ohne Namen'}) und lege eine Partition „{label}“ an …")
    _ausfuehren(["wipefs", "-a", p["pfad"]], log)
    _ausfuehren(["sfdisk", "--quiet", p["pfad"]], log, eingabe="label: gpt\n,,L\n")
    teil = _partition_von(name)
    _ausfuehren(["wipefs", "-a", teil["path"]], log, pruefen=False)
    _ausfuehren(["mkfs.ext4", "-F", "-q", "-L", label, teil["path"]], log)
    _ausfuehren(["udevadm", "settle"], pruefen=False)
    neu = _partition_von(name)
    uuid = neu.get("uuid") or _ausfuehren(["blkid", "-s", "UUID", "-o", "value", neu["path"]]).strip()
    if not uuid:
        raise Fehler("Die neue Partition hat keine UUID bekommen.")
    log(f"Partition {neu['path']} angelegt (UUID {uuid}).")
    return {"pfad": neu["path"], "uuid": uuid}


# ── fstab ─────────────────────────────────────────────────────────────────────
def fstab_setzen(kennung: str, zeile: str | None) -> None:
    """Die Zeile des Installers für `kennung` setzen oder entfernen. Fremde Zeilen bleiben."""
    marke = f"# studio:{kennung}"
    alt = FSTAB.read_text().splitlines() if FSTAB.exists() else []
    neu = [z for z in alt if not z.rstrip().endswith(marke)]
    if zeile:
        neu.append(f"{zeile}  {marke}")
    _sicher_schreiben(FSTAB, "\n".join(neu) + "\n", 0o644)


def fstab_daten(uuid: str) -> str:
    # nofail: fehlt die Platte, startet der PC trotzdem – Docker aber nicht (siehe Drop-in).
    return f"UUID={uuid} {PLATTE_DATEN} ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2"


def _cifs_optionen() -> str:
    return (f"credentials={ETC / 'smb-zugang'},uid=0,gid=0,file_mode=0600,dir_mode=0700,iocharset=utf8,"
            "_netdev,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.mount-timeout=30")


def fstab_backup(ziel: dict) -> str | None:
    """fstab-Zeile für das Backup-Ziel. automount: ein fehlendes Ziel hält nie den Start auf."""
    art = ziel.get("art")
    if art == "usb":
        return (f"UUID={ziel['uuid']} {BACKUP_PFAD} {ziel.get('fstype') or 'ext4'} "
                "defaults,nofail,x-systemd.automount,x-systemd.idle-timeout=600,x-systemd.device-timeout=10s 0 2")
    if art == "smb":
        return f"//{ziel['server']}/{ziel['freigabe']} {BACKUP_PFAD} cifs {_cifs_optionen()} 0 0"
    if art == "nfs":
        return (f"{ziel['server']}:{ziel['pfad']} {BACKUP_PFAD} nfs "
                "_netdev,nofail,soft,timeo=150,retrans=3,x-systemd.automount,x-systemd.idle-timeout=600,"
                "x-systemd.mount-timeout=30 0 0")
    return None


# ── Compose-Ergänzung ─────────────────────────────────────────────────────────
LOKAL_DATEI = "compose.lokal.yml"


def compose_lokal_text(daten: Path | None) -> str:
    """compose.lokal.yml: Daten-Ordner (außer im Bestand) und immer der Austauschordner."""
    zeilen = ["# Vom Studio-Installer erzeugt (Schritt „Speicher“) – nicht von Hand ändern.",
              "# Ersetzt die Docker-Volumes durch Ordner auf dem gewählten Datenspeicher und",
              "# bindet den Austauschordner für Sicherung und Updates ein.",
              "services:"]
    for dienst, ziel in UNTERORDNER.items():
        mounts = []
        if daten is not None:
            mounts.append(f"{daten / dienst}:{ziel}")
        if dienst == "api":
            mounts.append(f"{AUSTAUSCH}:/data/host")
        if mounts:
            zeilen.append(f"  {DIENSTE[dienst]}:")
            zeilen.append("    volumes:")
            zeilen += [f'      - "{m}"' for m in mounts]
    return "\n".join(zeilen) + "\n"


def compose_lokal_schreiben() -> None:
    s = speicher_config()
    daten = Path(s["daten"]) if s.get("art") in ("system", "platte") else None
    _sicher_schreiben(schritte.ZIEL / LOKAL_DATEI, compose_lokal_text(daten), 0o644)


def austausch_anlegen() -> None:
    (AUSTAUSCH / "anforderung").mkdir(parents=True, exist_ok=True)
    os.chmod(AUSTAUSCH, 0o750)


# ── Datenspeicher ─────────────────────────────────────────────────────────────
def bestand_vorhanden() -> bool:
    """Gibt es schon Studio-Daten in Docker-Volumes (frühere Installation ohne Speicher-Wahl)?"""
    if speicher_config().get("art") in ("system", "platte"):
        return False
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "volume", "inspect", f"{schritte.PROJEKT}_db_data"],
                          capture_output=True).returncode == 0


def speicher_stand() -> dict:
    s = speicher_config()
    if not s and bestand_vorhanden():
        s = {"art": "bestand"}
    frei = None
    pfad = s.get("daten") or ("/var/lib/docker" if s.get("art") == "bestand" else None)
    if pfad and Path(pfad).exists():
        st = os.statvfs(pfad)
        frei = st.f_bavail * st.f_frsize
    return {**s, "frei": frei, "festgelegt": bool(s)}


def _ordner_anlegen(daten: Path) -> None:
    for unter in UNTERORDNER:
        (daten / unter).mkdir(parents=True, exist_ok=True)
    os.chmod(daten, 0o755)


def _docker_braucht(pfad: Path | None, log) -> None:
    """Docker startet nur, wenn die Datenplatte eingehängt ist – sonst schriebe die Datenbank
    ins leere Verzeichnis auf der Systemplatte, und Studio sähe aus wie frisch installiert."""
    if pfad is None:
        DOCKER_DROPIN.unlink(missing_ok=True)
    else:
        _sicher_schreiben(DOCKER_DROPIN, "# Studio-Installer: Docker erst mit eingehängter Datenplatte\n"
                                         f"[Unit]\nRequiresMountsFor={pfad}\n", 0o644)
    _ausfuehren(["systemctl", "daemon-reload"], log, pruefen=False)


def datenspeicher_waehlen(eingaben: dict, log) -> dict:
    """Systemplatte oder eigene Platte. Läuft als Auftrag (Formatieren dauert)."""
    if schritte.installiert() and speicher_config():
        raise Fehler("Der Datenspeicher ist schon eingerichtet. Ein Umzug der Daten geht nur mit dem Support.")
    if bestand_vorhanden():
        _json_schreiben(ETC / "speicher.json", {"art": "bestand"}, 0o644)
        log("Bestehende Installation: Die Daten bleiben in den Docker-Volumes.")
        return {"art": "bestand"}
    art = eingaben.get("art")
    if art == "system":
        _ordner_anlegen(SYSTEM_DATEN)
        _docker_braucht(None, log)
        konfig = {"art": "system", "daten": str(SYSTEM_DATEN)}
        log(f"Daten auf der Systemplatte unter {SYSTEM_DATEN}.")
    elif art == "platte":
        name = str(eingaben.get("platte", ""))
        p = _platte(name)
        if p["studio_daten"] and not eingaben.get("neu_formatieren"):
            teil = p["studio_daten"]
            log(f"Vorhandene Studio-Datenplatte {teil['pfad']} wird übernommen.")
        else:
            if (eingaben.get("bestaetigung") or "").strip() != name:
                raise Fehler(f"Zum Formatieren bitte den Namen der Platte ({name}) eintippen.")
            teil = formatieren(name, LABEL_DATEN, log)
        PLATTE_DATEN.mkdir(parents=True, exist_ok=True)
        if not os.path.ismount(PLATTE_DATEN):
            # Leerer Mountpunkt unveränderlich: fehlt die Platte, kann niemand hineinschreiben.
            _ausfuehren(["chattr", "+i", str(PLATTE_DATEN)], log, pruefen=False)
        fstab_setzen("daten", fstab_daten(teil["uuid"]))
        _ausfuehren(["systemctl", "daemon-reload"], log, pruefen=False)
        if not os.path.ismount(PLATTE_DATEN):
            _ausfuehren(["mount", str(PLATTE_DATEN)], log)
        _ordner_anlegen(PLATTE_DATEN)
        _docker_braucht(PLATTE_DATEN, log)
        konfig = {"art": "platte", "daten": str(PLATTE_DATEN), "uuid": teil["uuid"],
                  "modell": p["modell"], "anschluss": p["anschluss"], "groesse": p["groesse"]}
        log(f"Daten auf {p['modell'] or name} unter {PLATTE_DATEN}.")
    else:
        raise Fehler("Bitte Systemplatte oder eigene Platte wählen.")
    _json_schreiben(ETC / "speicher.json", konfig, 0o644)
    return konfig


# ── Backup-Ziel ───────────────────────────────────────────────────────────────
SERVER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
FREIGABE = re.compile(r"^[^/\\\x00-\x1f]{1,80}(/[^/\\\x00-\x1f]{1,80})*$")
NFS_PFAD = re.compile(r"^/[A-Za-z0-9._/-]{0,250}$")


def _ziel_aus_eingaben(e: dict) -> dict:
    art = e.get("art")
    if art in (None, "", "spaeter"):
        return {"art": "spaeter"}
    if art == "usb":
        p = _platte(str(e.get("platte", "")))
        if speicher_config().get("uuid") and p["studio_daten"] and \
                p["studio_daten"]["uuid"] == speicher_config().get("uuid"):
            raise Fehler("Das ist die Datenplatte. Die Sicherung gehört auf eine andere Platte.")
        return {"art": "usb", "platte": p["name"], "modell": p["modell"],
                "uuid": (p["studio_backup"] or {}).get("uuid"), "fstype": (p["studio_backup"] or {}).get("fstype")}
    if art in ("smb", "nfs"):
        server = (e.get("server") or "").strip().lstrip("/").rstrip("/")
        if not SERVER.match(server):
            raise Fehler("Server: bitte Name oder IP-Adresse der Netzwerkfestplatte, z. B. 192.168.178.5 oder nas.")
        if art == "smb":
            freigabe = (e.get("freigabe") or "").strip().strip("/")
            if not FREIGABE.match(freigabe):
                raise Fehler("Freigabe: Name des freigegebenen Ordners, z. B. studio-backup.")
            benutzer = (e.get("benutzer") or "").strip()
            if not benutzer or any(c in benutzer for c in "\n\r="):
                raise Fehler("Benutzername der Netzwerkfestplatte fehlt.")
            return {"art": "smb", "server": server, "freigabe": freigabe, "benutzer": benutzer,
                    "passwort": e.get("passwort") or "", "domaene": (e.get("domaene") or "").strip()}
        pfad = (e.get("pfad") or "").strip()
        if not NFS_PFAD.match(pfad):
            raise Fehler("NFS-Pfad: der exportierte Ordner, z. B. /volume1/studio-backup.")
        return {"art": "nfs", "server": server, "pfad": pfad}
    raise Fehler("Unbekanntes Backup-Ziel.")


def _smb_zugang_text(ziel: dict) -> str:
    text = f"username={ziel['benutzer']}\npassword={ziel['passwort']}\n"
    if ziel.get("domaene"):
        text += f"domain={ziel['domaene']}\n"
    return text


def _einhaengen_probe(ziel: dict, ort: Path, log) -> None:
    ort.mkdir(parents=True, exist_ok=True)
    art = ziel["art"]
    if art == "usb":
        geraet = f"UUID={ziel['uuid']}"
        _ausfuehren(["mount", geraet, str(ort)], log)
    elif art == "smb":
        if not shutil.which("mount.cifs"):
            _pakete(["cifs-utils"], log)
        with tempfile.NamedTemporaryFile("w", dir="/run", prefix="studio-smb-", delete=False) as fh:
            os.chmod(fh.name, 0o600)
            fh.write(_smb_zugang_text(ziel))
        try:
            _ausfuehren(["mount", "-t", "cifs", f"//{ziel['server']}/{ziel['freigabe']}", str(ort),
                         "-o", f"credentials={fh.name},uid=0,gid=0,file_mode=0600,dir_mode=0700,iocharset=utf8"], log)
        finally:
            os.unlink(fh.name)
    elif art == "nfs":
        if not shutil.which("mount.nfs"):
            _pakete(["nfs-common"], log)
        _ausfuehren(["mount", "-t", "nfs", "-o", "soft,timeo=100,retrans=2",
                     f"{ziel['server']}:{ziel['pfad']}", str(ort)], log)


def _pakete(namen: list[str], log) -> None:
    log(f"Installiere {' '.join(namen)} …")
    _ausfuehren(["apt-get", "install", "-y", *namen], log)


def ziel_pruefen(ort: Path) -> dict:
    """Schreiben, lesen, löschen – und wie viel Platz frei ist."""
    probe = ort / f".studio-probe-{os.getpid()}"
    inhalt = os.urandom(64).hex()
    probe.write_text(inhalt)
    try:
        if probe.read_text() != inhalt:
            raise Fehler("Die Testdatei kam verändert zurück.")
    finally:
        probe.unlink(missing_ok=True)
    st = os.statvfs(ort)
    return {"frei": st.f_bavail * st.f_frsize, "gesamt": st.f_blocks * st.f_frsize}


def _klartext(exc: Exception, ziel: dict) -> str:
    text = str(exc)
    if ziel["art"] == "smb":
        return ("Die Netzwerkfestplatte hat abgelehnt. Bitte Server, Freigabe, Benutzer und Passwort prüfen "
                "(Details im Protokoll).")
    if ziel["art"] == "nfs":
        return ("Die NFS-Freigabe ließ sich nicht einhängen. Ist der Ordner für diesen PC freigegeben "
                "(Details im Protokoll)?")
    return text


def backup_ziel_testen(eingaben: dict, log) -> dict:
    ziel = _ziel_aus_eingaben(eingaben)
    if ziel["art"] == "spaeter":
        return {"art": "spaeter"}
    if ziel["art"] == "usb" and not ziel.get("uuid"):
        return {"art": "usb", "formatieren_noetig": True}
    ort = Path(tempfile.mkdtemp(prefix="studio-ziel-test-", dir="/run"))
    try:
        try:
            _einhaengen_probe(ziel, ort, log)
        except Fehler as exc:
            raise Fehler(_klartext(exc, ziel)) from exc
        try:
            ergebnis = ziel_pruefen(ort)
        except OSError as exc:
            raise Fehler(f"Das Ziel ist eingehängt, aber nicht beschreibbar ({exc.strerror}).") from exc
        log(f"Backup-Ziel beschreibbar, {ergebnis['frei'] // 1024**3} GB frei.")
        return {"art": ziel["art"], **ergebnis}
    finally:
        _ausfuehren(["umount", str(ort)], pruefen=False)
        try:
            ort.rmdir()
        except OSError:
            pass


def backup_ziel_speichern(eingaben: dict, log) -> dict:
    ziel = _ziel_aus_eingaben(eingaben)
    if ziel["art"] == "usb" and (not ziel.get("uuid") or eingaben.get("neu_formatieren")):
        if (eingaben.get("bestaetigung") or "").strip() != ziel["platte"]:
            raise Fehler(f"Zum Formatieren bitte den Namen der Platte ({ziel['platte']}) eintippen.")
        teil = formatieren(ziel["platte"], LABEL_BACKUP, log)
        ziel.update(uuid=teil["uuid"], fstype="ext4")
    ergebnis = backup_ziel_testen({**eingaben, "art": ziel["art"]}, log) if ziel["art"] != "usb" else \
        _usb_testen(ziel, log)
    # Altes Ziel aushängen, bevor die neue Zeile gilt.
    if os.path.ismount(BACKUP_PFAD):
        _ausfuehren(["umount", "-l", str(BACKUP_PFAD)], log, pruefen=False)
    if ziel["art"] == "smb":
        _sicher_schreiben(ETC / "smb-zugang", _smb_zugang_text(ziel), 0o600)
    else:
        (ETC / "smb-zugang").unlink(missing_ok=True)
    BACKUP_PFAD.mkdir(parents=True, exist_ok=True)
    fstab_setzen("backup", fstab_backup(ziel))
    _ausfuehren(["systemctl", "daemon-reload"], log, pruefen=False)
    if ziel["art"] != "spaeter":
        _ausfuehren(["systemctl", "restart", "srv-studio\\x2dbackup.automount"], log, pruefen=False)
    oeffentlich = {k: v for k, v in ziel.items() if k != "passwort"}
    konfig = sicherung_config()
    konfig["ziel"] = oeffentlich
    sicherung_config_schreiben(konfig)
    return {**oeffentlich, **{k: v for k, v in ergebnis.items() if k in ("frei", "gesamt")}}


def _usb_testen(ziel: dict, log) -> dict:
    ort = Path(tempfile.mkdtemp(prefix="studio-ziel-test-", dir="/run"))
    try:
        _einhaengen_probe(ziel, ort, log)
        return ziel_pruefen(ort)
    finally:
        _ausfuehren(["umount", str(ort)], pruefen=False)
        try:
            ort.rmdir()
        except OSError:
            pass


def backup_ziel_stand() -> dict:
    ziel = sicherung_config().get("ziel") or {"art": "spaeter"}
    return {k: v for k, v in ziel.items() if k != "passwort"}
