"""KI-Dienste des Studios: Compose-Profil `ki` mit ki-worker, app-laeufer und pii-gate (Installer ab 0.5.2, Studio ab 0.53.1).

Bis 0.5.1 schaltete der Installer das Profil nie ein und rief rollen.sh nie auf, und für das PII-Gate gab es kein Release-Image.
Server, die nur der Installer eingerichtet hatte, liefen deshalb ohne KI-Worker: Wissens-Eingänge (IMAP, WhatsApp …) holten nie ab
(„Wartet auf den ersten Lauf“), der nächtliche Prüflauf blieb aus, App-Bau und Hintergrund-Agenten standen. Gefunden am 16.11.2026 auf
dem Edge Suntime No1 Wiedenbrück.

Reihenfolge (Studio docs/UMSETZUNG.md 9.3.14, Vertrag G4) – fest, weil Worker und Läufer nie mit dem Zugang der API laufen dürfen:
  1. Studio (api, web, db …) läuft und hat migriert; Worker und Läufer stehen dabei.
  2. rollen.sh --env .env --db <db> --api <api>: Rollen studio_worker/studio_laeufer, .env.prozess*, Rechte, Eingänge umziehen.
  3. COMPOSE_PROFILES bekommt `ki`, dann ki-worker und app-laeufer neu erstellen, pii-gate starten.
  4. rollen.sh --pruefen meldet nur OK, und der Worker meldet sich an der Datenbank an.
Scheitert 2–4, laufen Worker und Läufer nicht, und `ki` kommt wieder aus COMPOSE_PROFILES. Studio selbst (Kasse, Tür) bleibt davon
unberührt; ein Fehler hier bricht weder Einrichtung noch Update ab.

Abschalten je Server: STUDIO_KI=aus in /opt/studio/.env. Dann schaltet der Installer `ki` nie ein (und nimmt es heraus, wenn er es
selbst eingetragen hat).
"""

from __future__ import annotations

import json
import re
import subprocess

import schritte

PROFIL = "ki"
#: Dienste im Profil ki; Worker und Läufer brauchen die Rollen, das Gate nicht.
MIT_ROLLE = ("ki-worker", "app-laeufer")
GATE = "pii-gate"
PII_REF = re.compile(r"^ghcr\.io/[a-z0-9._/-]+/studio-pii-gate@sha256:([0-9a-f]{64})$")
PII_LOKAL = "studio-pii-gate"
#: application_name, mit dem der Worker dauerhaft an der Datenbank hängt (LISTEN, Studio modules/ki/worker.py)
WORKER_ANMELDUNG = "ki-worker-lauschen"
#: Rollen aus rollen.sh. Eine Sicherung eines Studios mit Rollen enthält GRANTs an sie; ohne die Rollen bricht pg_restore ab.
ROLLEN = ("studio_worker", "studio_laeufer")
ROLLEN_VORAB_SQL = "DO $$ BEGIN " + " ".join(
    f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{r}') THEN CREATE ROLE {r} NOLOGIN; END IF;" for r in ROLLEN) + " END $$;"


def _env() -> dict[str, str]:
    pfad = schritte.ZIEL / ".env"
    return schritte.env_lesen(pfad.read_text()) if pfad.exists() else {}


def profile(env: dict[str, str] | None = None) -> list[str]:
    env = _env() if env is None else env
    return [p.strip() for p in env.get("COMPOSE_PROFILES", "").split(",") if p.strip()]


def an(env: dict[str, str] | None = None) -> bool:
    return PROFIL in profile(env)


def abgeschaltet(env: dict[str, str] | None = None) -> bool:
    env = _env() if env is None else env
    return env.get("STUDIO_KI", "").strip().lower() in ("aus", "0", "nein", "false")


def _profile_setzen(liste: list[str]) -> None:
    schritte.env_setzen({"COMPOSE_PROFILES": ",".join(dict.fromkeys(liste))})


def ohne_ki() -> dict[str, str]:
    """Umgebung für einen Compose-Aufruf, der das Profil ki nicht startet. COMPOSE_PROFILES aus der Umgebung geht der .env vor."""
    return {"COMPOSE_PROFILES": ",".join(p for p in profile() if p != PROFIL)}


# ── PII-Gate aus dem Edge-Paket ───────────────────────────────────────────────
def pii_eintrag() -> dict | None:
    datei = schritte.ZIEL / "images.json"
    try:
        eintrag = json.loads(datei.read_text()).get("pii") or {}
    except (OSError, ValueError, AttributeError):
        return None
    return eintrag if PII_REF.match(str(eintrag.get("ref") or "")) else None


def pii_tag(ref: str) -> str:
    """Lokaler Tag aus dem Digest: gleicher Digest, gleicher Tag, und das Gate startet beim Update nicht neu."""
    return "r-" + PII_REF.match(ref).group(1)[:16]


def pii_bereit(env: dict[str, str] | None = None) -> bool:
    """Ist das Image eingetragen, das compose für pii-gate nimmt, und liegt es hier?"""
    env = _env() if env is None else env
    bild, version = env.get("STUDIO_PII_IMAGE", ""), env.get("STUDIO_PII_VERSION", "")
    if not bild or not version:
        return False
    return schritte._ausgabe(["docker", "image", "inspect", f"{bild}:{version}"]) is not None


def pii_laden(registry: dict, log) -> bool:
    """PII-Gate aus images.json laden und eintragen. Scheitert nie das Ganze; ohne Gate bleiben nur die KI-Dienste aus."""
    eintrag = pii_eintrag()
    if not eintrag:
        if not pii_bereit():
            log("Das Studio-Paket nennt kein PII-Gate (erst ab Studio 0.53.1) – die KI-Dienste bleiben vorerst aus.")
        return pii_bereit()
    ref, tag = eintrag["ref"], pii_tag(eintrag["ref"])
    lokal = f"{PII_LOKAL}:{tag}"
    if schritte._ausgabe(["docker", "image", "inspect", lokal]) is None:
        log(f"PII-Gate laden ({eintrag.get('modell') or 'Sprachmodell'}) …")
        try:
            schritte.images_ziehen(registry, [ref], log)
            schritte._ausfuehren(["docker", "tag", ref, lokal], log)
        except schritte.Fehler as exc:
            log(f"WARNUNG: PII-Gate nicht geladen ({exc}) – die KI-Dienste bleiben, wie sie sind.")
            return pii_bereit()
    schritte.env_setzen({"STUDIO_PII_IMAGE": PII_LOKAL, "STUDIO_PII_VERSION": tag})
    return True


# ── Ablauf ───────────────────────────────────────────────────────────────────
def anhalten(log) -> None:
    """Worker und Läufer stoppen, bevor eine neue Version migriert. Sie kommen erst mit `einrichten` wieder."""
    schritte._ausfuehren(schritte._compose("--profile", PROFIL, "stop", *MIT_ROLLE), log, cwd=schritte.ZIEL, pruefen=False)


def _container(dienst: str) -> str:
    kennung = (schritte._ausgabe(schritte._compose("ps", "-q", dienst)) or "").strip().splitlines()
    if not kennung:
        raise schritte.Fehler(f"Der Container {dienst} läuft nicht.")
    return kennung[0]


def _rollen_sh(*argumente: str, log) -> tuple[int, str]:
    befehl = ["bash", str(schritte.ZIEL / "rollen.sh"), "--env", str(schritte.ZIEL / ".env"), *argumente]
    log("$ " + " ".join(befehl))
    proc = subprocess.run(befehl, capture_output=True, text=True, cwd=schritte.ZIEL, timeout=300)
    for zeile in (proc.stdout + proc.stderr).splitlines():
        if zeile.strip():
            log("  " + zeile)
    return proc.returncode, proc.stdout


def worker_angemeldet() -> bool:
    aus = schritte._ausgabe(schritte._compose("exec", "-T", "db", "psql", "-U", "studio", "-d", "studio", "-Atc",
                                     f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{WORKER_ANMELDUNG}'"))
    return (aus or "").strip() not in ("", "0")


def _zuruecknehmen(log, grund: str) -> dict:
    """Worker und Läufer aus, ki aus COMPOSE_PROFILES. Das Gate darf stehen bleiben: Es hat keinen Datenbankzugang."""
    log(f"FEHLER bei den KI-Diensten: {grund} – Worker und Läufer bleiben aus, Studio läuft weiter.")
    anhalten(log)
    _profile_setzen([p for p in profile() if p != PROFIL])
    return {"ki": "fehler", "grund": grund}


def einrichten(log, *, warten_s: float = 180) -> dict:
    """Rollen, dann Profil ki, dann Dienste, dann Prüfung. Vorher muss Studio laufen (API hat migriert). Wirft nie."""
    try:
        return _einrichten(log, warten_s)
    except Exception as exc:  # Studio läuft; die KI-Dienste dürfen Einrichtung und Update nicht mitreißen
        try:
            return _zuruecknehmen(log, str(exc))
        except Exception:
            return {"ki": "fehler", "grund": str(exc)}


def _einrichten(log, warten_s: float) -> dict:
    env = _env()
    if abgeschaltet(env):
        if an(env):
            anhalten(log)
            _profile_setzen([p for p in profile(env) if p != PROFIL])
        log("KI-Dienste sind auf diesem Server abgeschaltet (STUDIO_KI=aus).")
        return {"ki": "aus"}
    if not (schritte.ZIEL / "rollen.sh").exists():
        log("Das Studio-Paket bringt kein rollen.sh mit – die KI-Dienste bleiben aus.")
        return {"ki": "ohne_rollen"}
    if not pii_bereit(env):
        log("Kein PII-Gate auf diesem Server – die KI-Dienste bleiben aus. Ohne Gate ginge nichts mit Kundendaten an ein Sprachmodell.")
        return {"ki": "ohne_gate"}

    log("KI-Dienste einrichten: Datenbankrollen für Worker und Läufer …")
    rc, _ = _rollen_sh("--db", _container("db"), "--api", _container("api"), log=log)
    if rc != 0:
        return _zuruecknehmen(log, "rollen.sh konnte die Rollen nicht anlegen")
    _profile_setzen([*profile(), PROFIL])
    log("KI-Dienste starten (Worker, App-Läufer, PII-Gate) …")
    # Neu erstellen: .env.prozess* können sich geändert haben, und compose sieht Dateiinhalte nicht immer.
    schritte._ausfuehren(schritte._compose("up", "-d", "--no-build", "--no-deps", "--force-recreate", *MIT_ROLLE), log, cwd=schritte.ZIEL)
    schritte._ausfuehren(schritte._compose("up", "-d", "--no-build", "--no-deps", GATE), log, cwd=schritte.ZIEL)
    if not schritte._warten(worker_angemeldet, warten_s, pause=5):
        return _zuruecknehmen(log, "der KI-Worker meldet sich nicht an der Datenbank an (docker compose -p studio-edge logs ki-worker)")
    rc, aus = _rollen_sh("--db", _container("db"), "--pruefen", log=log)
    fehler = [z for z in aus.splitlines() if z.startswith("FEHLER")]
    if rc != 0 or fehler:
        return _zuruecknehmen(log, "Prüfung der Rollen nicht sauber: " + ("; ".join(f.removeprefix("FEHLER").strip() for f in fehler[:3])
                                                                              or f"Rückgabe {rc}"))
    log("KI-Dienste laufen: Worker mit eigener Datenbankrolle, Prüfung der Rollen ohne Befund.")
    return {"ki": "an"}


def nachzuziehen() -> bool:
    """Nächtlicher Lauf ohne neue Version: Das Paket der laufenden Version kann die KI-Dienste, sie laufen aber nicht."""
    env = _env()
    if abgeschaltet(env) or not (schritte.ZIEL / "rollen.sh").exists():
        return False
    if not (pii_eintrag() or pii_bereit(env)):
        return False
    return not an(env) or not pii_bereit(env) or not (schritte.ZIEL / ".env.prozess").exists()


def rollen_vorab(ausfuehren) -> None:
    """Platzhalter-Rollen (NOLOGIN) vor pg_restore anlegen. `ausfuehren(sql)` führt SQL als Superuser in der Ziel-Datenbank aus.
    rollen.sh macht daraus später die richtigen Rollen (LOGIN, Passwort, Rechte)."""
    ausfuehren(ROLLEN_VORAB_SQL)
