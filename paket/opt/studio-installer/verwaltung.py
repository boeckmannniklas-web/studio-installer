"""Verwaltung eines eingerichteten Studio-PCs.

Das Fenster, das nach Alt+F4 auf der Kasse erscheint (siehe kiosk-schleife.sh):
  - Übersicht: welche Installer- und Studio-Version hier läuft und an welches Studio der PC
    gebunden ist,
  - „Für ein anderes Studio einrichten“: nach dem Passwort eines SystemOwners wird alles
    gelöscht, was zum bisherigen Studio gehört, und der Assistent beginnt von vorn.

Vor dem Löschen entsteht eine letzte Sicherung (Cloud und/oder Backup-Ziel) – die Kassendaten
des alten Studios müssen aufbewahrt werden. Danach löst sich der PC in der Cloud vom alten
Studio, damit dessen Lizenz auf einem anderen PC aktiviert werden kann.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import betrieb
import schritte
import speicher

Fehler = schritte.Fehler

#: Studio auf diesem PC (Web-Container auf Port 80, leitet /api an die API weiter).
STUDIO_LOKAL = os.environ.get("STUDIO_LOKAL_URL", "http://127.0.0.1")
REBOOT_DATEI = Path("/etc/apt/apt.conf.d/52studio-unattended-reboot")
TIMER = ("studio-sicherung.timer", "studio-update.timer", "studio-auftrag.path")
SYSTEM_OWNER = "system_owner"


# ── Übersicht ─────────────────────────────────────────────────────────────────
def uebersicht() -> dict:
    """Was auf diesem PC läuft und für wen. Keine Geheimnisse (Tokens bleiben in der .env)."""
    i = schritte.installiert() or {}
    env = betrieb._env()
    sich = betrieb.zustand().get("sicherung") or {}
    upstream = env.get("SYNC_UPSTREAM_URL", "")
    return {
        "installer": betrieb._installer_version(),
        "studio_version": env.get("STUDIO_VERSION") or i.get("version"),
        "studio": env.get("EDGE_TENANT_NAME") or i.get("studio"),
        "tenant_id": env.get("EDGE_TENANT_ID"),
        "cloud": urllib.parse.urlparse(upstream).hostname if upstream else None,
        "gekoppelt": bool(upstream and env.get("SYNC_TOKEN")),
        "installiert_at": i.get("installiert_at"),
        "rechner": socket.gethostname(),
        "fernwartung": i.get("fernwartung"),
        "tailscale": i.get("tailscale"),
        "speicher": {k: v for k, v in speicher.speicher_stand().items() if k in ("art", "daten", "modell", "frei")},
        "letzte_sicherung": sich.get("letzte_erfolgreich"),
        "updates": betrieb.updates_config(),
    }


# ── SystemOwner prüfen ────────────────────────────────────────────────────────
class Bremse:
    """Höchstens `anzahl` Fehlversuche je `fenster` Sekunden – gegen Durchprobieren."""

    def __init__(self, anzahl: int = 5, fenster: float = 300) -> None:
        self.anzahl, self.fenster, self.fehlversuche = anzahl, fenster, []
        self.lock = threading.Lock()

    def pruefen(self) -> None:
        with self.lock:
            grenze = time.monotonic() - self.fenster
            self.fehlversuche = [t for t in self.fehlversuche if t > grenze]
            if len(self.fehlversuche) >= self.anzahl:
                rest = int(self.fehlversuche[0] - grenze) + 1
                raise Fehler(f"Zu viele Fehlversuche. Bitte in {max(rest // 60, 1)} Minuten erneut versuchen.")

    def fehlschlag(self) -> None:
        with self.lock:
            self.fehlversuche.append(time.monotonic())


BREMSE = Bremse()


def _studio(methode: str, pfad: str, daten: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    kopf = {"Accept": "application/json", "User-Agent": "studio-verwaltung"}
    rumpf = None
    if daten is not None:
        rumpf, kopf["Content-Type"] = json.dumps(daten).encode(), "application/json"
    if token:
        kopf["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(STUDIO_LOKAL + pfad, data=rumpf, method=methode, headers=kopf)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise Fehler("Studio antwortet nicht, das Passwort lässt sich so nicht prüfen. Ohne laufendes Studio "
                     "geht das Zurücksetzen nur im Terminal: sudo studio-betrieb anderes-studio") from exc


def systemowner_pruefen(login: str, passwort: str) -> str:
    """Anmeldung am Studio dieses PCs; nur ein SystemOwner darf weiter. Gibt seinen Namen zurück."""
    BREMSE.pruefen()
    if not login.strip() or not passwort:
        raise Fehler("Bitte Benutzer und Passwort des SystemOwners eingeben.")
    code, antwort = _studio("POST", "/api/v1/auth/login", {"login": login.strip(), "password": passwort})
    if code == 401 or (code == 200 and not antwort.get("access_token")):
        BREMSE.fehlschlag()
        raise Fehler("Benutzer oder Passwort stimmt nicht.")
    if code != 200:
        raise Fehler(f"Studio meldet bei der Anmeldung Fehler {code}.")
    code, ich = _studio("GET", "/api/v1/auth/me", token=antwort["access_token"])
    if code != 200:
        raise Fehler(f"Studio meldet bei der Anmeldung Fehler {code}.")
    if SYSTEM_OWNER not in {r.get("key") for r in ich.get("roles") or []}:
        BREMSE.fehlschlag()
        raise Fehler("Dieses Konto ist kein SystemOwner. Nur der SystemOwner darf den PC für ein anderes "
                     "Studio freigeben.")
    return ich.get("display_name") or ich.get("username") or login.strip()


def bestaetigung_pruefen(eingabe: str) -> None:
    name = (uebersicht().get("studio") or "").strip()
    if name and " ".join((eingabe or "").split()).casefold() != " ".join(name.split()).casefold():
        raise Fehler(f"Zur Bestätigung bitte den Namen des bisherigen Studios eintippen: {name}")


# ── Zurücksetzen ──────────────────────────────────────────────────────────────
def _leeren(ordner: Path, log) -> None:
    """Inhalt eines Ordners löschen, den Ordner selbst behalten (Mountpunkt, Paket-Ordner)."""
    if str(ordner.resolve()) in ("/", "/etc", "/var", "/var/lib", "/opt", "/srv", "/home"):
        raise Fehler(f"Weigere mich, {ordner} zu leeren.")
    if not ordner.is_dir():
        return
    for eintrag in ordner.iterdir():
        if eintrag.is_dir() and not eintrag.is_symlink():
            shutil.rmtree(eintrag)
        else:
            eintrag.unlink()
    log(f"Geleert: {ordner}")


def _weg(pfad: Path, log) -> None:
    if pfad.is_dir() and not pfad.is_symlink():
        shutil.rmtree(pfad)
        log(f"Gelöscht: {pfad}")
    elif pfad.exists() or pfad.is_symlink():
        pfad.unlink()
        log(f"Gelöscht: {pfad}")


def _cloud_loesen(log) -> bool:
    try:
        betrieb._cloud("POST", "/api/v1/edge/freigeben", {"grund": "anderes_studio"})
        log("In der Cloud vom bisherigen Studio gelöst.")
        return True
    except Fehler as exc:
        log(f"WARNUNG: In der Cloud nicht gelöst ({exc}). Bitte in der Plattform beim bisherigen Studio "
            "unter „Edge & Installation“ auf „Gerät lösen“ klicken, sonst lässt sich dessen Lizenz auf keinem "
            "anderen PC aktivieren.")
        return False


def anderes_studio(log, *, ohne_sicherung: bool = False, wer: str = "") -> dict:
    """Alles löschen, was zum bisherigen Studio gehört. Docker, Images und das Installer-Paket
    bleiben – der Assistent beginnt danach von vorn (auch mit dem PC-Passwort)."""
    vorher = uebersicht()
    log(f"Für ein anderes Studio einrichten – bisher: {vorher.get('studio') or 'unbekannt'}"
        + (f", angefordert von {wer}" if wer else ""))
    ergebnis = {"studio_alt": vorher.get("studio"), "sicherung": None, "cloud_geloest": False}
    eingerichtet = (schritte.ZIEL / ".env").exists()

    # Kein Studio- oder Installer-Update dazwischen.
    with betrieb.sperre("update"), betrieb.sperre("installer"):
        if eingerichtet and not ohne_sicherung:
            log("Letzte Sicherung des bisherigen Studios …")
            try:
                eintrag = betrieb.sichern("manuell", log)
            except Fehler as exc:
                raise Fehler(f"Die letzte Sicherung ist fehlgeschlagen ({exc}). Es wurde nichts gelöscht.") from exc
            ergebnis["sicherung"] = eintrag.get("datei")
        elif ohne_sicherung:
            log("WARNUNG: ohne letzte Sicherung – so ausdrücklich gewählt.")

        schritte._ausfuehren(["systemctl", "disable", "--now", *TIMER], log, pruefen=False)

        if eingerichtet:
            ergebnis["cloud_geloest"] = _cloud_loesen(log)

        if (schritte.ZIEL / "compose.yml").exists() and (schritte.ZIEL / ".env").exists():
            log("Studio anhalten und seine Docker-Volumes löschen …")
            schritte._ausfuehren(schritte._compose("down", "-v", "--remove-orphans"), log, cwd=schritte.ZIEL)

        art = speicher.speicher_config().get("art")
        if art == "system":
            _weg(speicher.SYSTEM_DATEN, log)
        elif art == "platte":
            if os.path.ismount(speicher.PLATTE_DATEN):
                _leeren(speicher.PLATTE_DATEN, log)
                schritte._ausfuehren(["umount", str(speicher.PLATTE_DATEN)], log, pruefen=False)
            else:
                log("WARNUNG: Die Datenplatte ist nicht eingehängt. Bitte im Assistenten beim Speicher "
                    "„neu formatieren“ wählen, sonst übernimmt er die alten Daten.")
                ergebnis["platte_nicht_geleert"] = True
        # Backup-Ziel nur aushängen: dort liegen die Sicherungen des bisherigen Studios.
        if os.path.ismount(speicher.BACKUP_PFAD):
            schritte._ausfuehren(["umount", str(speicher.BACKUP_PFAD)], log, pruefen=False)
        speicher.fstab_setzen("daten", None)
        speicher.fstab_setzen("backup", None)
        speicher.DOCKER_DROPIN.unlink(missing_ok=True)
        schritte._ausfuehren(["systemctl", "daemon-reload"], log, pruefen=False)

        schritte.ZIEL.mkdir(parents=True, exist_ok=True)
        _leeren(schritte.ZIEL, log)
        for pfad in (speicher.ETC, speicher.AUSTAUSCH, betrieb.ARBEIT, REBOOT_DATEI):
            _weg(pfad, log)
        schritte.ZUSTAND.mkdir(mode=0o700, parents=True, exist_ok=True)
        _leeren(schritte.ZUSTAND, log)
        schritte.WIEDERHERSTELLUNGSBLATT.clear()

    log("Fertig. Der Assistent beginnt von vorn – jetzt den Lizenzschlüssel des neuen Studios bereithalten.")
    return ergebnis
