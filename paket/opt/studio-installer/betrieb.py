"""Betrieb eines eingerichteten Studio-Edge: Sicherung, Updates und Aufträge aus dem Studio.

Läuft als root über /usr/bin/studio-betrieb, ausgelöst von systemd:

  studio-sicherung.timer  nachts            studio-betrieb sichern --anlass nacht
  studio-update.timer     alle 30 Minuten   studio-betrieb update-pruefen
  studio-auftrag.path     sobald Studio im Austauschordner eine Anforderung ablegt
                                            studio-betrieb auftraege

Studio selbst (im Container) hat keinen Docker-Zugriff und soll ihn auch nicht haben. Es
redet mit diesem Dienst über den Austauschordner /var/lib/studio-austausch (im Container
/data/host): Studio legt Anforderungen nach anforderung/, dieser Dienst schreibt seinen
Zustand nach status.json.

Sicherung (docs: Backup nach 3-2-1)
  Datenbank (pg_dump) + Dateien aus DATA_DIR + Konfiguration, als ein Archiv, mit age an
  den öffentlichen Schlüssel des Studios verschlüsselt. Den privaten Schlüssel hat nur der
  Betreiber (Wiederherstellungsblatt) und – verschlüsselt – die Plattform. Ziele: das
  Backup-Ziel vor Ort (USB oder Netzwerkfestplatte, Aufbewahrung 7 Tage/4 Wochen/12 Monate)
  und die Cloud (dort räumt die Cloud nach 14 Tagen selbst auf).

Update
  Die Cloud nennt die Versionen, die für dieses Studio freigegeben sind – signiert. Vor dem
  Wechsel: keine offene Kassen-Transaktion, Sicherung, Kopie der Datenbank für den Rückweg.
  Antwortet die neue Version nicht, geht es auf die alte zurück, notfalls samt Datenbank.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schritte  # noqa: E402
import speicher  # noqa: E402
from schritte import Fehler, _ausfuehren, _sicher_schreiben  # noqa: E402

ARBEIT = Path(os.environ.get("STUDIO_SICHERUNG_ARBEIT", "/var/lib/studio-sicherung"))
ZUSTAND_DATEI = "betrieb.json"
VERLAUF_MAX = 30
STANDARD_FENSTER = ("03:00", "05:00")
#: Aufbewahrung am Backup-Ziel vor Ort (Großvater-Vater-Sohn)
GFS = {"tage": 7, "wochen": 4, "monate": 12}
#: Wie lange die Kopie für den Rückweg nach einem erfolgreichen Update bleibt
VOR_UPDATE_TAGE = 3


def jetzt() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime | None = None) -> str:
    return (t or jetzt()).isoformat(timespec="seconds")


def log_stdout(zeile: str) -> None:
    print(zeile, flush=True)


# ── Zustand ───────────────────────────────────────────────────────────────────
def zustand() -> dict:
    return schritte._zustand_lesen(ZUSTAND_DATEI)


def zustand_aendern(**teile) -> dict:
    z = zustand()
    for k, v in teile.items():
        if isinstance(v, dict) and isinstance(z.get(k), dict):
            z[k] = {**z[k], **v}
        else:
            z[k] = v
    schritte._zustand_schreiben(ZUSTAND_DATEI, z)
    status_schreiben(z)
    return z


def verlauf_anhaengen(bereich: str, eintrag: dict) -> None:
    z = zustand()
    teil = z.get(bereich) or {}
    teil["verlauf"] = ([eintrag] + (teil.get("verlauf") or []))[:VERLAUF_MAX]
    z[bereich] = teil
    schritte._zustand_schreiben(ZUSTAND_DATEI, z)
    status_schreiben(z)


def updates_config() -> dict:
    c = speicher._json_lesen(speicher.ETC / "updates.json")
    return {"modus": c.get("modus") or "automatisch",
            "fenster_von": c.get("fenster_von") or STANDARD_FENSTER[0],
            "fenster_bis": c.get("fenster_bis") or STANDARD_FENSTER[1]}


UHRZEIT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def updates_einstellen(modus: str, fenster_von: str | None = None, fenster_bis: str | None = None) -> dict:
    if modus not in ("automatisch", "manuell"):
        raise Fehler("Updates: automatisch oder manuell.")
    alt = updates_config()
    von, bis = fenster_von or alt["fenster_von"], fenster_bis or alt["fenster_bis"]
    if not UHRZEIT.match(von) or not UHRZEIT.match(bis) or von == bis:
        raise Fehler("Wartungsfenster: Uhrzeiten wie 03:00 und 05:00.")
    neu = {"modus": modus, "fenster_von": von, "fenster_bis": bis}
    speicher._json_schreiben(speicher.ETC / "updates.json", neu, 0o644)
    snap_fenster(von, bis)
    status_schreiben()
    return neu


def snap_fenster(von: str, bis: str) -> None:
    """Snaps (Firefox!) nur im Wartungsfenster erneuern – sonst fragt der Kiosk tagsüber nach
    einem Neustart. unattended-upgrades startet im selben Fenster neu (52studio-unattended)."""
    if shutil.which("snap"):
        _ausfuehren(["snap", "set", "system", f"refresh.timer={von}-{bis}"], pruefen=False)
    try:
        Path("/etc/apt/apt.conf.d/52studio-unattended-reboot").write_text(
            "// Studio-Installer: nötiger Neustart nach Sicherheitsupdates nur im Wartungsfenster\n"
            'Unattended-Upgrade::Automatic-Reboot "true";\n'
            f'Unattended-Upgrade::Automatic-Reboot-Time "{von}";\n')
    except OSError:
        pass


def im_fenster(t: datetime, von: str, bis: str) -> bool:
    lokal = t.astimezone()
    minute = lokal.hour * 60 + lokal.minute
    a = int(von[:2]) * 60 + int(von[3:])
    b = int(bis[:2]) * 60 + int(bis[3:])
    return a <= minute < b if a < b else (minute >= a or minute < b)


def _naechster_nachtlauf() -> str | None:
    aus = _ausfuehren(["systemctl", "show", "studio-sicherung.timer", "-p", "NextElapseUSecRealtime",
                       "--value"], pruefen=False).strip()
    return aus or None


def status_schreiben(z: dict | None = None) -> None:
    """Was Studio im Container zu sehen bekommt. Keine Geheimnisse."""
    z = z if z is not None else zustand()
    s = speicher.sicherung_config()
    pub = s.get("oeffentlicher_schluessel") or ""
    sich = z.get("sicherung") or {}
    upd = z.get("update") or {}
    daten = {
        "aktualisiert": iso(),
        "host": {"name": socket.gethostname(), "installer": _installer_version()},
        "speicher": speicher.speicher_stand(),
        "sicherung": {
            "ziel": speicher.backup_ziel_stand(), "cloud": s.get("cloud", True),
            "schluessel": bool(pub), "fingerabdruck": _fingerabdruck(pub) if pub else None,
            "laeuft": bool(sich.get("laeuft")), "letzte": sich.get("letzte"),
            "letzte_erfolgreich": sich.get("letzte_erfolgreich"), "naechste": _naechster_nachtlauf(),
            "verlauf": (sich.get("verlauf") or [])[:VERLAUF_MAX], "probe": sich.get("probe"),
            "ausstehend": sorted(p.name for p in (ARBEIT / "ausstehend").glob("*.age"))
            if (ARBEIT / "ausstehend").exists() else [],
        },
        "update": {
            **updates_config(), "installiert": _installierte_version(),
            "verfuegbar": upd.get("verfuegbar") or [], "automatisch": upd.get("automatisch"),
            "letzte_pruefung": upd.get("letzte_pruefung"), "pruef_fehler": upd.get("pruef_fehler"),
            "laeuft": upd.get("laeuft"), "verlauf": (upd.get("verlauf") or [])[:VERLAUF_MAX],
        },
        "auftraege": z.get("auftraege") or {},
    }
    try:
        speicher.austausch_anlegen()
        _sicher_schreiben(speicher.AUSTAUSCH / "status.json", json.dumps(daten, indent=2, default=str), 0o644)
    except OSError:
        pass


def _fingerabdruck(pub: str) -> str:
    return hashlib.sha256(pub.encode()).hexdigest()[:16]


def _installer_version() -> str:
    try:
        return (schritte.HIER / "VERSION").read_text().strip()
    except OSError:
        return "?"


def _installierte_version() -> str | None:
    env = _env()
    return env.get("STUDIO_VERSION") or None


def _env() -> dict[str, str]:
    try:
        return schritte.env_lesen((schritte.ZIEL / ".env").read_text())
    except OSError:
        return {}


@contextmanager
def sperre(name: str):
    """Nur ein Lauf je Art – ein zweiter Timer-Lauf wartet nicht, er lässt es."""
    ordner = Path(os.environ.get("STUDIO_SPERREN", "/run/studio-betrieb"))
    ordner.mkdir(exist_ok=True)
    fh = open(ordner / f"{name}.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise Fehler(f"Es läuft schon: {name}.")
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ── Datenbank im Container ────────────────────────────────────────────────────
def _psql(sql: str) -> str:
    return _ausfuehren(schritte._compose("exec", "-T", "db", "psql", "-U", "studio", "-d", "studio",
                                         "-Atq", "-v", "ON_ERROR_STOP=1", "-c", sql),
                       cwd=schritte.ZIEL).strip()


def _schemas() -> list[str]:
    return [z for z in _psql("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant\\_%' ORDER BY 1")
            .splitlines() if re.fullmatch(r"tenant_[0-9a-f_]{36}", z)]


def _tabelle_da(schema: str, tabelle: str) -> bool:
    return _psql(f"SELECT to_regclass('\"{schema}\".{tabelle}') IS NOT NULL") == "t"


def kassen_stand() -> dict:
    """Wo die Kasse zum Zeitpunkt der Sicherung stand – für den Abgleich nach einem Restore."""
    stand = {}
    for schema in _schemas():
        if not _tabelle_da(schema, "bestellungen"):
            continue
        zeile = _psql(f'SELECT coalesce(max(bon_nr), 0), coalesce(max(fiscal_tx_number), 0), '
                      f'coalesce(max(z_nr), 0), coalesce(to_char(max(created_at) AT TIME ZONE \'UTC\', '
                      f'\'YYYY-MM-DD"T"HH24:MI:SS"Z"\'), \'\') FROM "{schema}".bestellungen')
        bon, tx, z, letzte = (zeile.split("|") + ["", "", "", ""])[:4]
        stand[schema] = {"max_bon_nr": int(bon or 0), "max_tse_transaktion": int(tx or 0),
                         "max_z_nr": int(z or 0), "letzte_bestellung": letzte or None}
    return stand


def offene_vorgaenge() -> int:
    """Kassenvorgänge, die gerade laufen. Ältere offene räumt Studio selbst auf (wartung)."""
    anzahl = 0
    for schema in _schemas():
        if _tabelle_da(schema, "bestellungen"):
            anzahl += int(_psql(f"SELECT count(*) FROM \"{schema}\".bestellungen WHERE status = 'open' "
                                f"AND created_at > now() - interval '6 hours'") or 0)
    return anzahl


# ── Sicherung ─────────────────────────────────────────────────────────────────
def _arbeitsordner() -> Path:
    """Auf der Datenplatte, wenn es eine gibt – dort ist der Platz für die Sicherung."""
    s = speicher.speicher_config()
    if s.get("art") == "platte":
        return Path(s["daten"]) / ".sicherung"
    return ARBEIT


def _sha256(pfad: Path) -> str:
    h = hashlib.sha256()
    with open(pfad, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _nach_datei(befehl: list[str], ziel: Path, log, *, eingabe: Path | None = None) -> None:
    log("$ " + " ".join(befehl) + f" > {ziel.name}")
    with open(ziel, "wb") as aus:
        ein = open(eingabe, "rb") if eingabe else subprocess.DEVNULL
        try:
            proc = subprocess.run(befehl, stdout=aus, stdin=ein, stderr=subprocess.PIPE, cwd=schritte.ZIEL)
        finally:
            if eingabe:
                ein.close()
    if proc.returncode != 0:
        raise Fehler(f"{befehl[0]} fehlgeschlagen: {proc.stderr.decode(errors='replace').strip()[-300:]}")


def _mandant_kurz() -> str:
    name = _env().get("EDGE_TENANT_NAME") or "studio"
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "studio"


def rohsicherung(ordner: Path, anlass: str, log) -> dict:
    """Datenbank, Dateien und Konfiguration unverschlüsselt in `ordner` – Grundlage für das
    verschlüsselte Archiv und für den Rückweg nach einem misslungenen Update."""
    ordner.mkdir(parents=True, exist_ok=True)
    os.chmod(ordner, 0o700)
    if not (schritte.ZIEL / "compose.yml").exists():
        raise Fehler("Studio ist auf diesem PC nicht installiert.")
    log("Datenbank sichern …")
    _nach_datei(schritte._compose("exec", "-T", "db", "pg_dump", "-U", "studio", "-d", "studio", "-Fc"),
                ordner / "db.dump", log)
    log("Datenbank-Sicherung prüfen …")
    liste = subprocess.run(schritte._compose("exec", "-T", "db", "pg_restore", "--list"),
                           stdin=open(ordner / "db.dump", "rb"), capture_output=True, cwd=schritte.ZIEL)
    eintraege = sum(1 for z in liste.stdout.decode(errors="replace").splitlines() if z and not z.startswith(";"))
    if liste.returncode != 0 or eintraege < 10:
        raise Fehler("Die Datenbank-Sicherung ist unvollständig (pg_restore --list).")
    log("Dateien sichern …")
    _nach_datei(schritte._compose("exec", "-T", "api", "tar", "-C", "/data", "--exclude=./host",
                                  "-cf", "-", "."), ordner / "daten.tar", log)
    konfig = ordner / "konfig"
    konfig.mkdir(exist_ok=True)
    for quelle in [schritte.ZIEL / n for n in (".env", "profile.yml", "compose.yml", speicher.LOKAL_DATEI,
                                               "VERSION", "installiert.json")] + \
                  [speicher.ETC / n for n in ("speicher.json", "updates.json", "sicherung.json")]:
        if quelle.exists():
            shutil.copy2(quelle, konfig / quelle.name)
    if (schritte.ZIEL / "mosquitto").is_dir():
        shutil.copytree(schritte.ZIEL / "mosquitto", konfig / "mosquitto", dirs_exist_ok=True)
    env = _env()
    manifest = {
        "format": 1, "erstellt": iso(), "anlass": anlass, "version": env.get("STUDIO_VERSION"),
        "installer": _installer_version(), "rechner": socket.gethostname(),
        "tenant_id": env.get("EDGE_TENANT_ID"), "tenant_name": env.get("EDGE_TENANT_NAME"),
        "kasse": kassen_stand(),
        "dateien": {p.name: {"sha256": _sha256(p), "groesse": p.stat().st_size}
                    for p in (ordner / "db.dump", ordner / "daten.tar")},
    }
    (ordner / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    return manifest


def probe_wiederherstellung(dump: Path, log) -> dict:
    """Die Sicherung in eine Wegwerf-Datenbank einspielen und zählen, was ankommt."""
    name = f"studio-probe-{uuid.uuid4().hex[:8]}"
    bild = _db_image()
    log(f"Probe-Wiederherstellung in einem Wegwerf-Container ({bild}) …")
    _ausfuehren(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=probe",
                 "-e", "POSTGRES_USER=studio", "-e", "POSTGRES_DB=studio", bild], log)
    try:
        bereit = schritte._warten(lambda: subprocess.run(
            ["docker", "exec", name, "pg_isready", "-U", "studio", "-d", "studio"],
            capture_output=True).returncode == 0, 90, pause=2)
        if not bereit:
            raise Fehler("Die Wegwerf-Datenbank startet nicht.")
        time.sleep(2)
        with open(dump, "rb") as ein:
            proc = subprocess.run(["docker", "exec", "-i", name, "pg_restore", "-U", "studio", "-d", "studio",
                                   "--no-owner", "--exit-on-error"], stdin=ein, capture_output=True)
        if proc.returncode != 0:
            raise Fehler("Die Probe-Wiederherstellung ist gescheitert: "
                         + proc.stderr.decode(errors="replace").strip()[-300:])
        zahl = subprocess.run(["docker", "exec", name, "psql", "-U", "studio", "-d", "studio", "-Atc",
                               "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"],
                              capture_output=True, text=True).stdout.strip()
        ergebnis = {"zeit": iso(), "ok": True, "tabellen": int(zahl or 0)}
        log(f"Probe-Wiederherstellung in Ordnung ({ergebnis['tabellen']} Tabellen).")
        return ergebnis
    finally:
        _ausfuehren(["docker", "rm", "-f", name], pruefen=False)


def _db_image() -> str:
    aus = _ausfuehren(schritte._compose("config", "--images"), cwd=schritte.ZIEL, pruefen=False)
    return next((z.strip() for z in aus.splitlines() if z.strip().startswith("postgres:")), "postgres:16-alpine")


def verschluesseln(ordner: Path, ziel: Path, pub: str, log) -> None:
    log("Archiv verschlüsseln (age) …")
    tar = subprocess.Popen(["tar", "-C", str(ordner), "-czf", "-", "manifest.json", "db.dump", "daten.tar", "konfig"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    age = subprocess.run(["age", "-r", pub, "-o", str(ziel)], stdin=tar.stdout, capture_output=True)
    tar.stdout.close()
    tar.wait()
    if tar.returncode != 0 or age.returncode != 0:
        ziel.unlink(missing_ok=True)
        raise Fehler("Verschlüsseln fehlgeschlagen: " + (age.stderr or tar.stderr.read()).decode(errors="replace")[-300:])
    os.chmod(ziel, 0o600)


def _ziel_ordner() -> Path | None:
    """Ordner am Backup-Ziel vor Ort, wenn eins eingerichtet und erreichbar ist."""
    if (speicher.backup_ziel_stand().get("art") or "spaeter") == "spaeter":
        return None
    ordner = speicher.BACKUP_PFAD / socket.gethostname()
    # Zugriff löst das automount aus; hängt die Freigabe, bricht timeout ab statt ewig zu warten.
    probe = subprocess.run(["timeout", "40", "mkdir", "-p", str(ordner)], capture_output=True)
    if probe.returncode != 0:
        raise Fehler("Das Backup-Ziel ist nicht erreichbar (Platte angeschlossen? Netzwerkfestplatte an?).")
    return ordner


ARCHIV = re.compile(r"^studio-.+-(\d{8})-(\d{6})-[a-z]+\.tar\.gz\.age$")


def gfs_behalten(namen: list[str]) -> set[str]:
    """Großvater-Vater-Sohn: die letzten 7 Tage je eine, 4 Wochen je eine, 12 Monate je eine.
    Behalten wird je Zeitraum die jüngste Sicherung."""
    datiert = []
    for n in namen:
        m = ARCHIV.match(n)
        if m:
            datiert.append((datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"), n))
    datiert.sort(reverse=True)
    behalten, tage, wochen, monate = set(), set(), set(), set()
    for t, n in datiert:
        tag, woche, monat = t.date(), t.isocalendar()[:2], (t.year, t.month)
        if len(tage) < GFS["tage"] and tag not in tage:
            tage.add(tag)
            behalten.add(n)
        if len(wochen) < GFS["wochen"] and woche not in wochen:
            wochen.add(woche)
            behalten.add(n)
        if len(monate) < GFS["monate"] and monat not in monate:
            monate.add(monat)
            behalten.add(n)
    return behalten


def ziel_aufraeumen(ordner: Path, log) -> int:
    namen = [p.name for p in ordner.iterdir() if ARCHIV.match(p.name)]
    weg = sorted(set(namen) - gfs_behalten(namen))
    for n in weg:
        (ordner / n).unlink(missing_ok=True)
    if weg:
        log(f"Am Backup-Ziel aufgeräumt: {len(weg)} ältere Sicherung(en).")
    return len(weg)


def hochladen(datei: Path, manifest: dict, log) -> dict:
    env = _env()
    url, token = env.get("SYNC_UPSTREAM_URL", "").rstrip("/"), env.get("SYNC_TOKEN", "")
    if not url or not token:
        raise Fehler("Keine Verbindung zur Cloud eingerichtet (SYNC_UPSTREAM_URL/SYNC_TOKEN).")
    groesse = datei.stat().st_size
    log(f"In die Cloud hochladen ({groesse // 1024**2} MB) …")
    kopf = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream",
            "Content-Length": str(groesse), "User-Agent": "studio-betrieb",
            "X-Sicherung-Name": datei.name, "X-Sicherung-Sha256": _sha256(datei),
            "X-Sicherung-Anlass": manifest.get("anlass", ""),
            "X-Sicherung-Version": manifest.get("version") or "",
            "X-Sicherung-Erstellt": manifest.get("erstellt", ""),
            "X-Sicherung-Kasse": json.dumps(manifest.get("kasse") or {}, separators=(",", ":"))[:4000]}
    with open(datei, "rb") as fh:
        req = urllib.request.Request(url + "/api/v1/sicherung/hochladen", data=fh, method="POST", headers=kopf)
        try:
            with urllib.request.urlopen(req, timeout=600) as antwort:
                return json.loads(antwort.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail")
            except Exception:
                detail = None
            raise Fehler(f"Die Cloud hat die Sicherung abgelehnt: {detail or exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Fehler(f"Die Cloud ist nicht erreichbar ({exc}).") from exc


def ausstehende_hochladen(log) -> None:
    """Was beim letzten Mal nicht in die Cloud kam, jetzt nachreichen."""
    ordner = ARBEIT / "ausstehend"
    if not ordner.exists():
        return
    for datei in sorted(ordner.glob("*.age")):
        meta = datei.with_suffix(".json")
        manifest = json.loads(meta.read_text()) if meta.exists() else {}
        try:
            hochladen(datei, manifest, log)
            datei.unlink()
            meta.unlink(missing_ok=True)
            log(f"Nachgereicht: {datei.name}")
        except Fehler as exc:
            log(f"Nachreichen gescheitert ({exc}) – nächstes Mal wieder.")
            break


def sichern(anlass: str = "manuell", log=log_stdout, *, roh_behalten: Path | None = None) -> dict:
    """Eine Sicherung erstellen und verteilen. `roh_behalten`: die unverschlüsselte Fassung
    dort liegen lassen (Rückweg beim Update)."""
    if anlass not in ("nacht", "manuell", "update", "kassenabschluss"):
        raise Fehler("Unbekannter Anlass.")
    with sperre("sicherung"):
        start = jetzt()
        zustand_aendern(sicherung={"laeuft": True})
        eintrag = {"start": iso(start), "anlass": anlass, "ok": False}
        arbeit = Path(tempfile.mkdtemp(prefix="lauf-", dir=_bereit(_arbeitsordner())))
        try:
            pub = speicher.sicherung_config().get("oeffentlicher_schluessel")
            manifest = rohsicherung(arbeit / "roh", anlass, log)
            eintrag["version"] = manifest.get("version")
            z = zustand().get("sicherung") or {}
            letzte_probe = (z.get("probe") or {}).get("zeit")
            if anlass == "nacht" and (not letzte_probe or
                                      jetzt() - datetime.fromisoformat(letzte_probe) > timedelta(days=7)):
                try:
                    zustand_aendern(sicherung={"probe": probe_wiederherstellung(arbeit / "roh" / "db.dump", log)})
                except Fehler as exc:
                    zustand_aendern(sicherung={"probe": {"zeit": iso(), "ok": False, "fehler": str(exc)}})
                    log(f"WARNUNG: {exc}")
            if roh_behalten is not None:
                if roh_behalten.exists():
                    shutil.rmtree(roh_behalten)
                roh_behalten.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(arbeit / "roh", roh_behalten)
                os.chmod(roh_behalten, 0o700)
            if not pub:
                raise Fehler("Kein Sicherungsschlüssel eingerichtet – bitte im Studio unter Backup "
                             "den Schlüssel einrichten (Wiederherstellungsblatt).")
            stempel = start.astimezone().strftime("%Y%m%d-%H%M%S")
            datei = arbeit / f"studio-{_mandant_kurz()}-{stempel}-{anlass}.tar.gz.age"
            verschluesseln(arbeit / "roh", datei, pub, log)
            eintrag.update(datei=datei.name, groesse=datei.stat().st_size, sha256=_sha256(datei))
            fehler = []
            try:
                ordner = _ziel_ordner()
                if ordner:
                    shutil.copy2(datei, ordner / datei.name)
                    eintrag["vor_ort"] = True
                    log(f"Am Backup-Ziel abgelegt: {ordner / datei.name}")
                    ziel_aufraeumen(ordner, log)
            except (Fehler, OSError) as exc:
                fehler.append(f"Backup-Ziel: {exc}")
                log(f"WARNUNG: {exc}")
            if speicher.sicherung_config().get("cloud", True):
                try:
                    ausstehende_hochladen(log)
                    hochladen(datei, manifest, log)
                    eintrag["cloud"] = True
                    log("In der Cloud abgelegt.")
                except Fehler as exc:
                    fehler.append(f"Cloud: {exc}")
                    log(f"WARNUNG: {exc} – die Sicherung wird beim nächsten Lauf nachgereicht.")
                    ausstehend = _bereit(ARBEIT / "ausstehend")
                    for alt in sorted(ausstehend.glob("*.age"))[:-2]:  # höchstens drei vorhalten
                        alt.unlink(missing_ok=True)
                        alt.with_suffix(".json").unlink(missing_ok=True)
                    shutil.move(str(datei), ausstehend / datei.name)
                    (ausstehend / datei.name).with_suffix(".json").write_text(json.dumps(manifest))
            if not eintrag.get("vor_ort") and not eintrag.get("cloud"):
                raise Fehler("Die Sicherung ist erstellt, aber an keinem Ziel angekommen: " + "; ".join(fehler))
            eintrag.update(ok=True, warnungen=fehler or None)
            log("Sicherung fertig." + (" Mit Warnungen." if fehler else ""))
            return eintrag
        except Fehler as exc:
            eintrag["fehler"] = str(exc)
            log(f"FEHLER: {exc}")
            raise
        except Exception as exc:
            eintrag["fehler"] = f"Unerwarteter Fehler: {exc}"
            log(traceback.format_exc())
            raise Fehler(eintrag["fehler"]) from exc
        finally:
            shutil.rmtree(arbeit, ignore_errors=True)
            eintrag["ende"] = iso()
            teile = {"laeuft": False, "letzte": eintrag}
            if eintrag["ok"]:
                teile["letzte_erfolgreich"] = eintrag["ende"]
            zustand_aendern(sicherung=teile)
            verlauf_anhaengen("sicherung", eintrag)


def _bereit(ordner: Path) -> Path:
    ordner.mkdir(parents=True, exist_ok=True)
    os.chmod(ordner, 0o700)
    return ordner


# ── Schlüssel ─────────────────────────────────────────────────────────────────
AGE_PUB = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")


def schluessel_erzeugen() -> tuple[str, str]:
    """(öffentlich, privat) – ein age-Schlüsselpaar."""
    aus = _ausfuehren(["age-keygen"])
    privat = next((z for z in aus.splitlines() if z.startswith("AGE-SECRET-KEY-1")), None)
    pub = next((z.split(":", 1)[1].strip() for z in aus.splitlines() if "public key:" in z), None)
    if not privat or not pub or not AGE_PUB.match(pub):
        raise Fehler("age-keygen hat kein Schlüsselpaar geliefert.")
    return pub, privat


def oeffentlichen_schluessel_setzen(pub: str) -> None:
    if not AGE_PUB.match(pub or ""):
        raise Fehler("Das ist kein öffentlicher age-Schlüssel.")
    konfig = speicher.sicherung_config()
    konfig["oeffentlicher_schluessel"] = pub
    speicher.sicherung_config_schreiben(konfig)
    status_schreiben()


# ── Updates ───────────────────────────────────────────────────────────────────
def _cloud(methode: str, pfad: str, daten: dict | None = None, timeout: float = 30) -> dict:
    env = _env()
    url, token = env.get("SYNC_UPSTREAM_URL", "").rstrip("/"), env.get("SYNC_TOKEN", "")
    if not url or not token:
        raise Fehler("Keine Verbindung zur Cloud eingerichtet.")
    kopf = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "studio-betrieb"}
    rumpf = None
    if daten is not None:
        rumpf, kopf["Content-Type"] = json.dumps(daten).encode(), "application/json"
    req = urllib.request.Request(url + pfad, data=rumpf, method=methode, headers=kopf)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail")
        except Exception:
            detail = None
        raise Fehler(detail or f"Die Cloud antwortet mit Fehler {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Fehler(f"Die Cloud ist nicht erreichbar ({exc}).") from exc


def _manifest_pruefen(eintrag: dict) -> dict:
    roh = schritte._b64(eintrag["manifest"])
    schritte._pruefen(schritte._b64(eintrag["signatur"]), roh, "Update-Manifest")
    manifest = json.loads(roh)
    for name in ("api", "web"):
        if not re.match(r"^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", manifest["images"].get(name, "")):
            raise Fehler(f"Das Manifest nennt kein festes Image für {name}.")
    return manifest


def _versionsschluessel(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in (v or "0").split("-", 1)[0].split("."))
    except ValueError:
        return (0,)


def update_pruefen(log=log_stdout, *, ausfuehren_erlaubt: bool = True) -> dict:
    """Bei der Cloud nachfragen; im Automatikmodus und Wartungsfenster gleich einspielen."""
    try:
        antwort = _cloud("GET", "/api/v1/edge/updates")
    except Fehler as exc:
        zustand_aendern(update={"letzte_pruefung": iso(), "pruef_fehler": str(exc)})
        raise
    installiert = _installierte_version()
    verfuegbar = []
    for eintrag in antwort.get("versionen") or []:
        try:
            m = _manifest_pruefen(eintrag)
        except (Fehler, KeyError, ValueError) as exc:
            log(f"Version verworfen: {exc}")
            continue
        verfuegbar.append({"version": m["version"], "notiz": eintrag.get("notiz"), "release_url": m.get("release_url"),
                           "freigegeben_at": eintrag.get("freigegeben_at"),
                           "neuer": _versionsschluessel(m["version"]) > _versionsschluessel(installiert)})
    verfuegbar.sort(key=lambda v: _versionsschluessel(v["version"]), reverse=True)
    automatisch = next((v["version"] for v in verfuegbar if v["neuer"]), None)
    zustand_aendern(update={"letzte_pruefung": iso(), "pruef_fehler": None, "verfuegbar": verfuegbar,
                            "automatisch": automatisch})
    cfg = updates_config()
    if (ausfuehren_erlaubt and automatisch and cfg["modus"] == "automatisch"
            and im_fenster(jetzt(), cfg["fenster_von"], cfg["fenster_bis"])):
        log(f"Automatisches Update auf {automatisch} im Wartungsfenster.")
        try:
            update_ausfuehren(automatisch, log, anlass="automatisch")
        except Fehler as exc:
            log(f"Automatisches Update nicht ausgeführt: {exc}")
    if ausfuehren_erlaubt and cfg["modus"] == "automatisch" and im_fenster(jetzt(), cfg["fenster_von"], cfg["fenster_bis"]):
        try:
            installer_aktualisieren(log)
        except Fehler as exc:
            log(f"Installer-Update nicht ausgeführt: {exc}")
    return {"verfuegbar": verfuegbar, "automatisch": automatisch}


def _vorherige_konfig(version: str) -> Path:
    return ARBEIT / "vor-update" / version


def update_ausfuehren(version: str | None = None, log=log_stdout, *, anlass: str = "manuell") -> dict:
    with sperre("update"):
        antwort = _cloud("GET", "/api/v1/edge/updates")
        kandidaten = []
        for e in antwort.get("versionen") or []:
            try:
                kandidaten.append(_manifest_pruefen(e))
            except (Fehler, KeyError, ValueError):
                continue
        if not kandidaten:
            raise Fehler("Die Cloud bietet keine Version an.")
        manifest = next((m for m in kandidaten if m["version"] == version), None) if version else \
            max(kandidaten, key=lambda m: _versionsschluessel(m["version"]))
        if manifest is None:
            raise Fehler(f"Version {version} ist für dieses Studio nicht freigegeben.")
        alt = _installierte_version() or "?"
        neu = manifest["version"]
        if neu == alt:
            raise Fehler(f"Version {neu} ist schon installiert.")
        offen = offene_vorgaenge()
        if offen:
            raise Fehler(f"An der Kasse läuft gerade ein Verkauf ({offen} offen). Bitte erst abschließen.")
        start = jetzt()
        eintrag = {"start": iso(start), "von": alt, "nach": neu, "anlass": anlass, "ok": False}
        zustand_aendern(update={"laeuft": {"nach": neu, "start": iso(start), "schritt": "Sicherung"}})
        sicherung_alt = _vorherige_konfig(alt)
        try:
            # 1. Sicherung – verschlüsselt an die Ziele und unverschlüsselt für den Rückweg.
            try:
                sichern("update", log, roh_behalten=sicherung_alt / "roh")
            except Fehler as exc:
                if not (sicherung_alt / "roh" / "db.dump").exists():
                    raise Fehler(f"Ohne Sicherung kein Update: {exc}") from exc
                log(f"WARNUNG: Sicherung nicht vollständig verteilt ({exc}) – die Kopie für den Rückweg liegt vor.")
            for n in ("compose.yml", "profile.yml", "VERSION", ".env"):
                if (schritte.ZIEL / n).exists():
                    shutil.copy2(schritte.ZIEL / n, sicherung_alt / n)
            if (schritte.ZIEL / "mosquitto").is_dir():
                shutil.copytree(schritte.ZIEL / "mosquitto", sicherung_alt / "mosquitto", dirs_exist_ok=True)
            # 2. Images laden
            zustand_aendern(update={"laeuft": {"nach": neu, "start": iso(start), "schritt": "Laden"}})
            images_laden(manifest, antwort["registry"], log)
            # 3. Edge-Paket der neuen Version auspacken und einsetzen
            edge_paket_einsetzen(neu, log)
            schritte.env_setzen({"STUDIO_VERSION": neu, "STUDIO_API_IMAGE": "studio-api",
                                 "STUDIO_WEB_IMAGE": "studio-web"})
            # 4. Starten und prüfen
            zustand_aendern(update={"laeuft": {"nach": neu, "start": iso(start), "schritt": "Start"}})
            _ausfuehren(schritte._compose("up", "-d", "--no-build", "--remove-orphans"), log, cwd=schritte.ZIEL)
            if not _laeuft_mit(neu, 420):
                raise Fehler(f"Version {neu} antwortet nicht.")
            eintrag["ok"] = True
            log(f"Update auf {neu} fertig.")
            _aufraeumen_vor_update(behalten=alt)
            return eintrag
        except Fehler as exc:
            eintrag["fehler"] = str(exc)
            log(f"FEHLER: {exc} – zurück auf {alt}.")
            eintrag["rueckweg"] = rueckweg(alt, sicherung_alt, log)
            raise Fehler(f"Update auf {neu} gescheitert ({exc}). {eintrag['rueckweg']}") from exc
        finally:
            eintrag["ende"] = iso()
            zustand_aendern(update={"laeuft": None})
            verlauf_anhaengen("update", eintrag)
            try:
                _cloud("POST", "/api/v1/edge/updates/meldung", {
                    "von": eintrag["von"], "nach": eintrag["nach"], "ok": eintrag["ok"], "anlass": anlass,
                    "fehler": eintrag.get("fehler"), "rueckweg": eintrag.get("rueckweg"),
                    "dauer_s": int((jetzt() - start).total_seconds())})
            except Fehler:
                pass


def images_laden(manifest: dict, registry: dict, log) -> None:
    version = manifest["version"]
    with tempfile.TemporaryDirectory(prefix="studio-docker-") as konfig:
        env = {"DOCKER_CONFIG": konfig}
        _ausfuehren(["docker", "login", registry["server"], "--username", registry["benutzer"],
                     "--password-stdin"], log, eingabe=registry["token"], env=env)
        try:
            for name in ("api", "web"):
                log(f"Lade {name} {version} …")
                _ausfuehren(["docker", "pull", manifest["images"][name]], log, env=env)
                _ausfuehren(["docker", "tag", manifest["images"][name], f"studio-{name}:{version}"], log)
        finally:
            _ausfuehren(["docker", "logout", registry["server"]], log, env=env, pruefen=False)


def edge_paket_einsetzen(version: str, log) -> None:
    with tempfile.TemporaryDirectory(prefix="studio-paket-") as tmp:
        name = f"studio-paket-{uuid.uuid4().hex[:6]}"
        _ausfuehren(["docker", "create", "--name", name, f"studio-api:{version}"], log)
        try:
            _ausfuehren(["docker", "cp", f"{name}:/app/edge-paket/.", tmp], log)
        finally:
            _ausfuehren(["docker", "rm", "-f", name], pruefen=False)
        drin = (Path(tmp) / "VERSION").read_text().strip() if (Path(tmp) / "VERSION").exists() else ""
        if drin != version or not (Path(tmp) / "compose.yml").exists():
            raise Fehler(f"Im Image steckt kein passendes Edge-Paket (gefunden: {drin or 'nichts'}).")
        for eintrag in Path(tmp).iterdir():
            ziel = schritte.ZIEL / eintrag.name
            if eintrag.name in (".env", speicher.LOKAL_DATEI, "installiert.json"):
                continue
            if eintrag.is_dir():
                shutil.copytree(eintrag, ziel, dirs_exist_ok=True)
            else:
                shutil.copy2(eintrag, ziel)


def _laeuft_mit(version: str, sekunden: float) -> bool:
    return bool(schritte._warten(
        lambda: (schritte._http_ok("http://127.0.0.1/api/v1/system/info") or {}).get("core_version") == version,
        sekunden, pause=5))


def rueckweg(alt: str, sicherung_alt: Path, log) -> str:
    """Zurück auf die alte Version. Startet sie nicht (die Datenbank ist schon migriert),
    kommt die Datenbank von vor dem Update zurück."""
    try:
        for n in ("compose.yml", "profile.yml", "VERSION"):
            if (sicherung_alt / n).exists():
                shutil.copy2(sicherung_alt / n, schritte.ZIEL / n)
        if (sicherung_alt / "mosquitto").is_dir():
            shutil.copytree(sicherung_alt / "mosquitto", schritte.ZIEL / "mosquitto", dirs_exist_ok=True)
        schritte.env_setzen({"STUDIO_VERSION": alt})
        _ausfuehren(schritte._compose("up", "-d", "--no-build", "--remove-orphans"), log, cwd=schritte.ZIEL,
                    pruefen=False)
        if _laeuft_mit(alt, 240):
            return f"Version {alt} läuft wieder, die Datenbank ist unverändert geblieben."
        dump = sicherung_alt / "roh" / "db.dump"
        if not dump.exists():
            return f"Version {alt} startet nicht, und es gibt keine Datenbank-Kopie. Bitte den Support rufen."
        log("Die alte Version startet nicht mit der neuen Datenbank – spiele die Kopie von vor dem Update ein …")
        datenbank_einspielen(dump, log)
        _ausfuehren(schritte._compose("up", "-d", "--no-build"), log, cwd=schritte.ZIEL, pruefen=False)
        if _laeuft_mit(alt, 240):
            return (f"Version {alt} läuft wieder mit der Datenbank von vor dem Update. Kassenvorgänge "
                    "dazwischen gab es nicht (das Update startet nur ohne offenen Verkauf).")
        return f"Auch der Rückweg auf {alt} ist gescheitert. Bitte sofort den Support rufen."
    except Exception as exc:  # der Rückweg darf nicht selbst abstürzen
        log(traceback.format_exc())
        return f"Rückweg gescheitert ({exc}). Bitte sofort den Support rufen."


def datenbank_einspielen(dump: Path, log) -> None:
    """Datenbank leeren und aus einem pg_dump neu befüllen. API und Web stehen dabei."""
    _ausfuehren(schritte._compose("stop", "api", "web"), log, cwd=schritte.ZIEL, pruefen=False)
    _ausfuehren(schritte._compose("up", "-d", "db"), log, cwd=schritte.ZIEL)
    schritte._warten(lambda: subprocess.run(schritte._compose("exec", "-T", "db", "pg_isready", "-U", "studio"),
                                            capture_output=True, cwd=schritte.ZIEL).returncode == 0, 120, pause=2)
    _ausfuehren(schritte._compose("exec", "-T", "db", "psql", "-U", "studio", "-d", "postgres", "-v",
                                  "ON_ERROR_STOP=1", "-c",
                                  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'studio' "
                                  "AND pid <> pg_backend_pid()"), log, cwd=schritte.ZIEL, pruefen=False)
    _ausfuehren(schritte._compose("exec", "-T", "db", "dropdb", "-U", "studio", "--if-exists", "studio"), log,
                cwd=schritte.ZIEL)
    _ausfuehren(schritte._compose("exec", "-T", "db", "createdb", "-U", "studio", "studio"), log, cwd=schritte.ZIEL)
    with open(dump, "rb") as ein:
        proc = subprocess.run(schritte._compose("exec", "-T", "db", "pg_restore", "-U", "studio", "-d", "studio",
                                                "--no-owner", "--exit-on-error"),
                              stdin=ein, capture_output=True, cwd=schritte.ZIEL)
    if proc.returncode != 0:
        raise Fehler("Datenbank einspielen fehlgeschlagen: " + proc.stderr.decode(errors="replace")[-300:])


def _aufraeumen_vor_update(behalten: str) -> None:
    ordner = ARBEIT / "vor-update"
    if not ordner.exists():
        return
    grenze = time.time() - VOR_UPDATE_TAGE * 86400
    for unter in ordner.iterdir():
        if unter.name != behalten and unter.stat().st_mtime < grenze:
            shutil.rmtree(unter, ignore_errors=True)


# ── Installer selbst ──────────────────────────────────────────────────────────
INSTALLER_QUELLE = os.environ.get(
    "STUDIO_INSTALLER_QUELLE",
    "https://github.com/boeckmannniklas-web/studio-installer/releases/latest/download")


def installer_aktualisieren(log) -> bool:
    """Neuere Fassung dieses Pakets einspielen. Prüfsumme aus SHA256SUMS des Releases."""
    try:
        with urllib.request.urlopen(urllib.request.Request(INSTALLER_QUELLE + "/SHA256SUMS",
                                                           headers={"User-Agent": "studio-betrieb"}), timeout=30) as r:
            summen = r.read().decode()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Fehler(f"Installer-Release nicht erreichbar ({exc}).") from exc
    eintraege = dict(reversed(z.split(None, 1)) for z in summen.splitlines() if len(z.split(None, 1)) == 2)
    datei = next((n for n in eintraege if re.fullmatch(r"\*?studio-installer_\d+\.\d+\.\d+_all\.deb", n)), None)
    if not datei:
        return False
    neu = re.search(r"_(\d+\.\d+\.\d+)_", datei).group(1)
    if _versionsschluessel(neu) <= _versionsschluessel(_installer_version()):
        return False
    with tempfile.TemporaryDirectory(prefix="studio-installer-") as tmp:
        ziel = Path(tmp) / datei.lstrip("*")
        urllib.request.urlretrieve(INSTALLER_QUELLE + "/" + datei.lstrip("*"), ziel)
        if _sha256(ziel) != eintraege[datei]:
            raise Fehler("Die Prüfsumme des Installer-Pakets stimmt nicht – nicht eingespielt.")
        os.chmod(ziel, 0o644)
        os.chmod(tmp, 0o755)
        log(f"Installer {_installer_version()} → {neu} …")
        _ausfuehren(["apt-get", "install", "-y", str(ziel)], log)
    return True


# ── Aufträge aus dem Studio ───────────────────────────────────────────────────
AKTIONEN = ("sichern", "update_suchen", "update_jetzt", "einstellungen", "schluessel")


def auftrag_ausfuehren(a: dict, log) -> dict | None:
    aktion = a.get("aktion")
    if aktion == "sichern":
        return sichern(a.get("anlass") if a.get("anlass") in ("manuell", "kassenabschluss") else "manuell", log)
    if aktion == "update_suchen":
        return update_pruefen(log, ausfuehren_erlaubt=False)
    if aktion == "update_jetzt":
        v = a.get("version")
        if v is not None and not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", str(v)):
            raise Fehler("Ungültige Version.")
        return update_ausfuehren(v, log, anlass="manuell")
    if aktion == "einstellungen":
        return updates_einstellen(str(a.get("modus", "")), a.get("fenster_von"), a.get("fenster_bis"))
    if aktion == "schluessel":
        oeffentlichen_schluessel_setzen(str(a.get("oeffentlich", "")))
        return {"ok": True}
    raise Fehler(f"Unbekannte Aktion {aktion!r}.")


def auftraege(log=log_stdout) -> int:
    """Anforderungen aus dem Austauschordner abarbeiten, älteste zuerst."""
    ordner = speicher.AUSTAUSCH / "anforderung"
    erledigt = 0
    for _ in range(20):
        dateien = sorted(ordner.glob("*.json"), key=lambda p: p.stat().st_mtime) if ordner.exists() else []
        if not dateien:
            break
        datei = dateien[0]
        try:
            a = json.loads(datei.read_text())
        except (OSError, ValueError):
            datei.unlink(missing_ok=True)
            continue
        datei.unlink(missing_ok=True)
        kennung = str(a.get("id") or datei.stem)[:64]
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", kennung) or a.get("aktion") not in AKTIONEN:
            continue
        _auftrag_merken(kennung, {"aktion": a["aktion"], "start": iso(), "fertig": False})
        try:
            ergebnis = auftrag_ausfuehren(a, log)
            _auftrag_merken(kennung, {"aktion": a["aktion"], "fertig": True, "ok": True, "ende": iso(),
                                      "ergebnis": ergebnis})
        except Fehler as exc:
            _auftrag_merken(kennung, {"aktion": a["aktion"], "fertig": True, "ok": False, "ende": iso(),
                                      "fehler": str(exc)})
        except Exception as exc:
            log(traceback.format_exc())
            _auftrag_merken(kennung, {"aktion": a["aktion"], "fertig": True, "ok": False, "ende": iso(),
                                      "fehler": f"Unerwarteter Fehler: {exc}"})
        erledigt += 1
    return erledigt


def _auftrag_merken(kennung: str, daten: dict) -> None:
    z = zustand()
    alle = z.get("auftraege") or {}
    alle[kennung] = daten
    # nur die letzten 20 behalten
    z["auftraege"] = dict(sorted(alle.items(), key=lambda kv: kv[1].get("start") or kv[1].get("ende") or "")[-20:])
    schritte._zustand_schreiben(ZUSTAND_DATEI, z)
    status_schreiben(z)


# ── Nachrüsten ────────────────────────────────────────────────────────────────
def nachruesten(log=log_stdout) -> bool:
    """Studios, die mit einem Installer vor 0.2 eingerichtet wurden: Speicherart „bestand“
    festhalten und den Austauschordner einbinden, damit Studio Sicherung und Updates
    ansprechen kann. Startet dafür einmal die API neu – nur, wenn sich etwas ändert."""
    if not schritte.installiert() or not (schritte.ZIEL / "compose.yml").exists():
        return False
    speicher.austausch_anlegen()
    if not speicher.speicher_config():
        art = "bestand" if speicher.bestand_vorhanden() else None
        if art is None:
            return False
        speicher._json_schreiben(speicher.ETC / "speicher.json", {"art": art}, 0o644)
    neu = speicher.compose_lokal_text(Path(speicher.speicher_config()["daten"])
                                      if speicher.speicher_config().get("art") in ("system", "platte") else None)
    datei = schritte.ZIEL / speicher.LOKAL_DATEI
    if datei.exists() and datei.read_text() == neu:
        status_schreiben()
        return False
    _sicher_schreiben(datei, neu, 0o644)
    if not (speicher.ETC / "updates.json").exists():
        updates_einstellen("automatisch")
    log("Austauschordner eingebunden – API startet neu.")
    _ausfuehren(schritte._compose("up", "-d", "--no-build", "api"), log, cwd=schritte.ZIEL, pruefen=False)
    status_schreiben()
    return True


# ── Befehlszeile ──────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="studio-betrieb", description="Sicherung, Updates und Aufträge des Studio-Edge")
    unter = p.add_subparsers(dest="befehl", required=True)
    s = unter.add_parser("sichern", help="jetzt sichern")
    s.add_argument("--anlass", default="manuell", choices=["nacht", "manuell", "update", "kassenabschluss"])
    unter.add_parser("update-pruefen", help="bei der Cloud nach Updates fragen (Automatik im Wartungsfenster)")
    u = unter.add_parser("update", help="Update jetzt einspielen")
    u.add_argument("--version")
    unter.add_parser("auftraege", help="Anforderungen aus dem Studio abarbeiten")
    unter.add_parser("status", help="Zustand ausgeben und für das Studio ablegen")
    unter.add_parser("nachruesten", help="ältere Installation an Sicherung und Updates anschließen")
    e = unter.add_parser("updates-einstellen")
    e.add_argument("modus", choices=["automatisch", "manuell"])
    e.add_argument("--von")
    e.add_argument("--bis")
    a = p.parse_args(argv)
    try:
        if a.befehl == "sichern":
            sichern(a.anlass)
        elif a.befehl == "update-pruefen":
            update_pruefen()
        elif a.befehl == "update":
            update_ausfuehren(a.version)
        elif a.befehl == "auftraege":
            auftraege()
        elif a.befehl == "nachruesten":
            nachruesten()
        elif a.befehl == "updates-einstellen":
            print(json.dumps(updates_einstellen(a.modus, a.von, a.bis), indent=2))
        else:
            status_schreiben()
            print((speicher.AUSTAUSCH / "status.json").read_text())
    except Fehler as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
