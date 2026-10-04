"""Die Schritte der Studio-Einrichtung. Läuft als root im Dienst studio-setup (setup.py).

Reihenfolge wie im Wizard:

  1. passwort_setzen      nur im Studio-Image: Anfangspasswort des Benutzers `studio` ersetzen
  2. internet_pruefen     Cloud und ghcr.io erreichbar?
  3. docker_installieren  Docker Engine + Compose aus dem Docker-Repo (sonst Ubuntu-Pakete)
  4. aktivieren           Lizenzschlüssel prüfen, bei der Cloud aktivieren (bindet die Hardware)
  5. fernwartung_waehlen  Tailscale der Plattform, eigener Schlüssel oder keine Fernwartung
  6. konfiguration_*      .env aus der Cloud holen oder im Wizard erzeugen, ablegen, an die Cloud schicken;
                          dazu Updates (automatisch/manuell, Wartungsfenster)
  7. speicher (speicher.py) Datenspeicher, Backup-Ziel, Sicherungsschlüssel, ggf. Wiederherstellung
  8. installieren         signiertes Manifest prüfen, Images ziehen, Edge-Paket auspacken, ggf.
                          Sicherung einspielen, starten, Sicherung und Updates einschalten
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

CLOUD = os.environ.get("STUDIO_CLOUD", "https://ki-sunlounge.de").rstrip("/")
HIER = Path(__file__).resolve().parent
ZIEL = Path(os.environ.get("STUDIO_ZIEL", "/opt/studio"))
ZUSTAND = Path(os.environ.get("STUDIO_ZUSTAND", "/var/lib/studio-installer"))
PROJEKT = os.environ.get("STUDIO_PROJEKT", "studio-edge")
PC_BENUTZER = "studio"

#: Das Studio-Image setzt diese Marke (iso/autoinstall.yaml). Nur dann gibt es den Passwortschritt.
IST_IMAGE = Path("/etc/studio-os").exists()


class Fehler(RuntimeError):
    """Eine Meldung für den Menschen vor dem Bildschirm."""


# ── Hilfen ────────────────────────────────────────────────────────────────────
def _b64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _oeffentlicher_schluessel():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    roh = (HIER / "cloud-key.pub").read_text().strip()
    return Ed25519PublicKey.from_public_bytes(_b64(roh))


def _pruefen(signatur: bytes, daten: bytes, was: str) -> None:
    from cryptography.exceptions import InvalidSignature
    try:
        _oeffentlicher_schluessel().verify(signatur, daten)
    except InvalidSignature as exc:
        raise Fehler(f"{was}: Die Signatur stimmt nicht – das stammt nicht von der Studio-Cloud.") from exc


def _anfrage(methode: str, pfad: str, *, daten: dict | None = None, token: str | None = None,
             timeout: float = 30) -> dict:
    kopf = {"Accept": "application/json", "User-Agent": "studio-installer"}
    rumpf = None
    if daten is not None:
        rumpf = json.dumps(daten).encode()
        kopf["Content-Type"] = "application/json"
    if token:
        kopf["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(CLOUD + pfad, data=rumpf, method=methode, headers=kopf)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as antwort:
            return json.loads(antwort.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail")
        except Exception:
            detail = None
        if isinstance(detail, list):  # Validierungsfehler von FastAPI
            detail = "; ".join(str(d.get("msg", d)) for d in detail)
        raise Fehler(detail or f"Die Cloud antwortet mit Fehler {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise Fehler(f"Die Cloud ist nicht erreichbar ({CLOUD}). Bitte die Internetverbindung prüfen.") from exc


def _ausfuehren(befehl: list[str], log=None, *, eingabe: str | None = None, env: dict | None = None,
                cwd: Path | None = None, pruefen: bool = True) -> str:
    """Befehl ausführen; jede Ausgabezeile geht ins Protokoll. Liefert die Ausgabe."""
    if log:
        log("$ " + " ".join(befehl))
    proc = subprocess.Popen(befehl, stdin=subprocess.PIPE if eingabe is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive", **(env or {})},
                            cwd=cwd)
    if eingabe is not None:
        proc.stdin.write(eingabe)
        proc.stdin.close()
    zeilen = []
    for zeile in proc.stdout:
        zeile = zeile.rstrip()
        zeilen.append(zeile)
        if log and zeile:
            log(zeile)
    proc.wait()
    if pruefen and proc.returncode != 0:
        raise Fehler(f"Befehl fehlgeschlagen ({befehl[0]} {befehl[1] if len(befehl) > 1 else ''}), "
                     f"Rückgabe {proc.returncode}. Details im Protokoll.")
    return "\n".join(zeilen)


def _sicher_schreiben(pfad: Path, text: str, modus: int = 0o600) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    neu = pfad.with_name(pfad.name + ".neu")
    fd = os.open(neu, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modus)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(neu, pfad)


def _zustand_lesen(name: str) -> dict:
    try:
        return json.loads((ZUSTAND / name).read_text())
    except (OSError, ValueError):
        return {}


def _zustand_schreiben(name: str, daten: dict) -> None:
    _sicher_schreiben(ZUSTAND / name, json.dumps(daten, indent=2, default=str))


# ── 1. Passwort des PCs ───────────────────────────────────────────────────────
def passwort_setzen(passwort: str) -> None:
    if not IST_IMAGE:
        raise Fehler("Nur im Studio-Image: hier gehört das Konto nicht dem Installer.")
    if len(passwort) < 8:
        raise Fehler("Das Passwort braucht mindestens 8 Zeichen.")
    if ":" in passwort or "\n" in passwort:
        raise Fehler("Doppelpunkt und Zeilenumbruch sind im Passwort nicht möglich.")
    _ausfuehren(["chpasswd"], eingabe=f"{PC_BENUTZER}:{passwort}\n")
    _zustand_schreiben("passwort.json", {"gesetzt": datetime.now(timezone.utc).isoformat()})


def passwort_gesetzt() -> bool:
    return not IST_IMAGE or bool(_zustand_lesen("passwort.json"))


# ── 2. Internet ───────────────────────────────────────────────────────────────
def internet_pruefen() -> dict:
    ergebnis = {}
    for name, url, ok_codes in (("cloud", CLOUD + "/api/v1/health", {200}),
                                ("registry", "https://ghcr.io/v2/", {200, 401})):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "studio-installer"}),
                                        timeout=10) as r:
                ergebnis[name] = r.status in ok_codes
        except urllib.error.HTTPError as exc:
            ergebnis[name] = exc.code in ok_codes
        except (urllib.error.URLError, TimeoutError, OSError):
            ergebnis[name] = False
    ergebnis["ok"] = all(ergebnis.values())
    return ergebnis


# ── 3. Docker ─────────────────────────────────────────────────────────────────
def docker_vorhanden() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0 and \
        subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _os_release() -> dict:
    daten = {}
    for zeile in Path("/etc/os-release").read_text().splitlines():
        if "=" in zeile:
            k, v = zeile.split("=", 1)
            daten[k] = v.strip('"')
    return daten


def docker_installieren(log) -> None:
    if docker_vorhanden():
        log("Docker ist schon installiert.")
        return
    codename = _os_release().get("VERSION_CODENAME", "")
    log(f"Ubuntu {codename}: Docker aus dem offiziellen Docker-Repository …")
    try:
        _ausfuehren(["apt-get", "update"], log)
        _ausfuehren(["apt-get", "install", "-y", "ca-certificates", "curl"], log)
        Path("/etc/apt/keyrings").mkdir(mode=0o755, parents=True, exist_ok=True)
        _ausfuehren(["curl", "-fsSL", "https://download.docker.com/linux/ubuntu/gpg",
                     "-o", "/etc/apt/keyrings/docker.asc"], log)
        os.chmod("/etc/apt/keyrings/docker.asc", 0o644)
        arch = _ausfuehren(["dpkg", "--print-architecture"]).strip() or "amd64"
        Path("/etc/apt/sources.list.d/docker.list").write_text(
            f"deb [arch={arch} signed-by=/etc/apt/keyrings/docker.asc] "
            f"https://download.docker.com/linux/ubuntu {codename} stable\n")
        _ausfuehren(["apt-get", "update"], log)
        _ausfuehren(["apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
                     "docker-buildx-plugin", "docker-compose-plugin"], log)
    except Fehler:
        # Gibt es für diese Ubuntu-Version (noch) nichts, dann die Pakete von Ubuntu selbst.
        log("Docker-Repository nicht nutzbar – nehme die Docker-Pakete von Ubuntu.")
        Path("/etc/apt/sources.list.d/docker.list").unlink(missing_ok=True)
        _ausfuehren(["apt-get", "update"], log)
        _ausfuehren(["apt-get", "install", "-y", "docker.io", "docker-compose-v2"], log)
    _ausfuehren(["systemctl", "enable", "--now", "docker"], log)
    if not docker_vorhanden():
        raise Fehler("Docker ist installiert, startet aber nicht. Details im Protokoll.")
    log("Docker läuft.")


# ── 4. Aktivieren ─────────────────────────────────────────────────────────────
def hardware_id() -> str:
    """Stabil über eine Neuinstallation des Betriebssystems hinweg, solange das Mainboard bleibt."""
    teile = []
    for pfad in ("/sys/class/dmi/id/product_uuid", "/etc/machine-id"):
        try:
            teile.append(Path(pfad).read_text().strip().lower())
        except OSError:
            pass
    # Die DMI-UUID allein, wenn es sie gibt: machine-id ändert sich mit jeder Neuinstallation.
    quelle = teile[0] if teile and len(teile[0]) >= 16 and teile[0] != "03000200-0400-0500-0006-000700080009" \
        else "|".join(teile)
    if not quelle:
        raise Fehler("Dieser PC hat keine erkennbare Hardware-Kennung.")
    return "hw-" + hashlib.sha256(quelle.encode()).hexdigest()


def lizenz_vorpruefen(schluessel: str) -> dict:
    """Format und Signatur prüfen, bevor die Cloud gefragt wird – ein Tippfehler fällt so sofort auf."""
    text = "".join((schluessel or "").split())
    if not text.startswith("STUDIO-1."):
        raise Fehler("Das sieht nicht wie ein Studio-Lizenzschlüssel aus (er beginnt mit STUDIO-1.).")
    nutzlast, _, signatur = text[len("STUDIO-1."):].partition(".")
    try:
        roh, sig = _b64(nutzlast), _b64(signatur)
    except Exception as exc:
        raise Fehler("Der Schlüssel ist unvollständig oder beschädigt.") from exc
    _pruefen(sig, roh, "Lizenzschlüssel")
    daten = json.loads(roh)
    if daten.get("app") != "studio":
        raise Fehler("Das ist eine App-Lizenz, keine Studio-Lizenz.")
    if daten.get("exp") and date.fromisoformat(daten["exp"]) < date.today():
        raise Fehler(f"Diese Lizenz ist am {date.fromisoformat(daten['exp']).strftime('%d.%m.%Y')} abgelaufen.")
    return {"schluessel": text, **daten}


def aktivieren(schluessel: str) -> dict:
    lizenz = lizenz_vorpruefen(schluessel)
    antwort = _anfrage("POST", "/api/v1/install/aktivieren", daten={
        "lizenzschluessel": lizenz["schluessel"], "hardware_id": hardware_id(),
        "hostname": socket.gethostname()[:100]})
    antwort["aktiviert_at"] = datetime.now(timezone.utc).isoformat()
    _zustand_schreiben("aktivierung.json", antwort)
    return aktivierung_oeffentlich()


def _aktivierung() -> dict:
    a = _zustand_lesen("aktivierung.json")
    if not a:
        raise Fehler("Erst den Lizenzschlüssel eingeben.")
    gueltig = datetime.fromisoformat(a["install_token_gueltig_bis"].replace("Z", "+00:00"))
    if gueltig < datetime.now(timezone.utc):
        raise Fehler("Die Aktivierung ist abgelaufen (nach 2 Stunden). Bitte den Lizenzschlüssel erneut eingeben.")
    return a


def aktivierung_oeffentlich() -> dict | None:
    """Was die Oberfläche von der Aktivierung sehen darf – keine Tokens."""
    a = _zustand_lesen("aktivierung.json")
    if not a:
        return None
    return {k: a.get(k) for k in ("tenant_name", "neuinstallation", "konfiguration_vorhanden",
                                   "tailscale_verfuegbar", "tailscale_hostname")}


# ── 5. Fernwartung ────────────────────────────────────────────────────────────
TS_SCHLUESSEL = re.compile(r"^tskey-[A-Za-z0-9-]{10,200}$")


def fernwartung_waehlen(wahl: str, eigener_schluessel: str = "", geraetename: str = "") -> dict:
    a = _aktivierung()
    if wahl == "plattform":
        if not a.get("tailscale_verfuegbar"):
            raise Fehler("Die Plattform bietet keine Fernwartung über Tailscale an.")
        # Den Einmal-Schlüssel holt installieren() erst direkt vor dem Start – er gilt nur kurz.
        schluessel = ""
    elif wahl == "eigen":
        schluessel = eigener_schluessel.strip()
        if not TS_SCHLUESSEL.match(schluessel):
            raise Fehler("Ein Tailscale-Schlüssel beginnt mit tskey- (Admin-Konsole → Settings → Keys).")
    elif wahl == "aus":
        schluessel = ""
    else:
        raise Fehler("Unbekannte Wahl.")
    name = re.sub(r"[^a-z0-9-]+", "-", (geraetename or a.get("tailscale_hostname") or "studio-edge").lower()).strip("-")
    _zustand_schreiben("fernwartung.json", {"wahl": wahl, "schluessel": schluessel, "name": name[:63]})
    return {"wahl": wahl, "name": name[:63]}


def _fernwartung() -> dict:
    f = _zustand_lesen("fernwartung.json")
    if not f:
        raise Fehler("Erst die Fernwartung wählen.")
    return f


# ── 6. Konfiguration ──────────────────────────────────────────────────────────
AUSGEBLENDET = ("lo", "docker", "br-", "veth", "tailscale", "virbr", "vnet", "macvlan")


def netzwerkkarten() -> list[dict]:
    try:
        daten = json.loads(subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True).stdout or "[]")
    except ValueError:
        daten = []
    karten = []
    for k in daten:
        name = k.get("ifname", "")
        if not name or name.startswith(AUSGEBLENDET):
            continue
        ipv4 = next((a["local"] for a in k.get("addr_info", []) if a.get("family") == "inet"), None)
        karten.append({"name": name, "wlan": Path(f"/sys/class/net/{name}/wireless").exists(),
                       "ip": ipv4, "verbunden": "LOWER_UP" in k.get("flags", [])})
    # Kabel mit Adresse zuerst: macvlan für das Gerätenetz geht praktisch nur über Kabel.
    return sorted(karten, key=lambda k: (k["wlan"], k["ip"] is None, k["name"]))


def env_lesen(text: str) -> dict[str, str]:
    werte = {}
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        k, v = zeile.removeprefix("export ").split("=", 1)
        werte[k.strip()] = v.strip()
    return werte


def vorschlaege() -> dict:
    a = _aktivierung()
    karten = netzwerkkarten()
    erste = karten[0] if karten else None
    cloud = None
    if a.get("konfiguration_vorhanden"):
        env = _anfrage("GET", "/api/v1/install/konfiguration", token=a["install_token"])["env"]
        werte = env_lesen(env)
        # Nur zur Anzeige – Geheimnisse bleiben verdeckt.
        cloud = {k: ("••••••" if re.search(r"PASSWORD|SECRET|TOKEN|KEY|DATABASE_URL", k) else v)
                 for k, v in werte.items()}
        _zustand_schreiben("cloud-env.json", {"env": env})
    return {
        "karten": karten,
        "karte": erste["name"] if erste else "",
        "lan_ip": erste["ip"] if erste and erste["ip"] else "",
        "marke": (a.get("tenant_name") or "STUDIO").upper()[:24],
        "cloud": cloud,
    }


GEHEIME_VORGABEN = ("POSTGRES_PASSWORD", "DATABASE_URL", "JWT_SECRET_KEY", "SECRETS_KEY")


def _geheimnisse(vorhanden: dict[str, str]) -> dict[str, str]:
    """Bestehende Geheimnisse behalten: eine vorhandene Datenbank kennt ihr Passwort schon."""
    if all(vorhanden.get(k) for k in GEHEIME_VORGABEN):
        return {k: vorhanden[k] for k in GEHEIME_VORGABEN}
    pw = secrets.token_hex(24)
    return {"POSTGRES_PASSWORD": pw, "DATABASE_URL": f"postgresql+asyncpg://studio:{pw}@db:5432/studio",
            "JWT_SECRET_KEY": secrets.token_hex(32), "SECRETS_KEY": secrets.token_hex(32)}


def _ip_oder_leer(wert: str, feld: str) -> str:
    wert = (wert or "").strip()
    if not wert:
        return ""
    try:
        return str(ipaddress.IPv4Address(wert))
    except ValueError as exc:
        raise Fehler(f"{feld}: „{wert}“ ist keine IPv4-Adresse.") from exc


def konfiguration_speichern(eingaben: dict) -> dict:
    """Die .env schreiben und an die Cloud schicken.

    Reihenfolge der Quellen: was auf diesem PC schon liegt (Geheimnisse einer vorhandenen
    Datenbank), dann die hinterlegte Konfiguration der Cloud, dann die Eingaben im Wizard.
    Was die Aktivierung neu vergibt (Token, Mandant, Fernwartung), gilt immer.
    """
    a = _aktivierung()
    f = _fernwartung()
    lokal = env_lesen((ZIEL / ".env").read_text()) if (ZIEL / ".env").exists() else {}
    cloud = env_lesen(_zustand_lesen("cloud-env.json").get("env", "")) if a.get("konfiguration_vorhanden") else {}

    werte: dict[str, str] = {
        "STUDIO_ENV": "production", "PROFILE_PATH": "/app/profile.yml", "DATA_DIR": "/data",
        "REDIS_URL": "redis://redis:6379/0", "CORS_ORIGINS": "http://localhost",
        "WEB_PORT": "80", "SYNC_INTERVAL_SECONDS": "60",
    }
    werte.update(cloud)
    if not cloud:
        karte = (eingaben.get("karte") or "").strip()
        if karte and not re.match(r"^[A-Za-z0-9_.:-]{1,15}$", karte):
            raise Fehler("Unbekannte Netzwerkkarte.")
        lan_ip = _ip_oder_leer(eingaben.get("lan_ip", ""), "Feste LAN-Adresse")
        werte.update({
            "EDGE_LAN_INTERFACE": karte, "EDGE_LAN_IP": lan_ip, "QR_TUER_SERVER_IP": lan_ip,
            "BRAND_NAME": (eingaben.get("marke") or "").strip()[:30],
            "BRAND_ACCENT": (eingaben.get("akzent") or "").strip()[:12],
        })
        werte["_GERAETENETZ"] = "1" if eingaben.get("geraetenetz") else ""
    else:
        werte["_GERAETENETZ"] = "1" if "geraetenetz" in cloud.get("COMPOSE_PROFILES", "") else ""
    werte.update(_geheimnisse({**cloud, **lokal}))
    werte.update({
        "SYNC_UPSTREAM_URL": a["sync_upstream_url"], "SYNC_TOKEN": a["sync_token"],
        "EDGE_TENANT_ID": a["tenant_id"], "EDGE_TENANT_NAME": a["tenant_name"],
        "FERNWARTUNG": f["wahl"], "TAILSCALE_HOSTNAME": f["name"],
        "TAILSCALE_AUTH_KEY": f["schluessel"], "TAILSCALE_IP": "", "TAILSCALE_NAME": "",
    })
    profile = [p for p, an in (("geraetenetz", werte.pop("_GERAETENETZ")), ("fernwartung", f["wahl"] != "aus")) if an]
    werte["COMPOSE_PROFILES"] = ",".join(profile)

    _updates_aus_eingaben(eingaben)
    text = env_text(werte)
    _sicher_schreiben(ZIEL / ".env", text)
    _anfrage("PUT", "/api/v1/install/konfiguration", token=a["install_token"],
             daten={"env": text, "fernwartung": f["wahl"]})
    return {"gespeichert": True, "aus_cloud": bool(cloud)}


def _updates_aus_eingaben(eingaben: dict) -> None:
    """Updates automatisch (Standard) oder manuell – der SystemOwner kann das später im
    Studio umstellen (Grundeinstellungen → Updates)."""
    import betrieb
    u = eingaben.get("updates") or {}
    betrieb.updates_einstellen(u.get("modus") or "automatisch", u.get("fenster_von") or None,
                               u.get("fenster_bis") or None)


GRUPPEN = (
    ("Grundeinstellungen", ("STUDIO_ENV", "PROFILE_PATH", "DATA_DIR", "REDIS_URL", "CORS_ORIGINS", "WEB_PORT")),
    ("Geheimnisse – nicht weitergeben", ("POSTGRES_PASSWORD", "DATABASE_URL", "JWT_SECRET_KEY", "SECRETS_KEY")),
    ("Cloud", ("SYNC_UPSTREAM_URL", "SYNC_TOKEN", "SYNC_INTERVAL_SECONDS", "EDGE_TENANT_ID", "EDGE_TENANT_NAME")),
    ("Studio-Netz und Geräte", ("EDGE_LAN_INTERFACE", "EDGE_LAN_IP", "QR_TUER_SERVER_IP", "COMPOSE_PROFILES")),
    ("Fernwartung", ("FERNWARTUNG", "TAILSCALE_HOSTNAME", "TAILSCALE_AUTH_KEY", "TAILSCALE_IP", "TAILSCALE_NAME")),
    ("Marke", ("BRAND_NAME", "BRAND_ACCENT")),
    ("Software", ("STUDIO_API_IMAGE", "STUDIO_WEB_IMAGE", "STUDIO_VERSION")),
)


def env_text(werte: dict[str, str]) -> str:
    zeilen = [f"# Studio-Edge – erzeugt vom Studio-Installer am {date.today().strftime('%d.%m.%Y')}"]
    rest = dict(werte)
    for titel, namen in GRUPPEN:
        zeilen.append(f"\n# ── {titel}")
        zeilen += [f"{n}={rest.pop(n)}" for n in namen if n in rest]
    if rest:
        zeilen.append("\n# ── Weitere")
        zeilen += [f"{n}={v}" for n, v in rest.items()]
    return "\n".join(zeilen) + "\n"


def env_setzen(aenderungen: dict[str, str | None]) -> None:
    """Einzelne Werte der .env ändern (None = Zeile entfernen); der Rest bleibt, wie er ist."""
    pfad = ZIEL / ".env"
    zeilen, gesehen = [], set()
    for zeile in pfad.read_text().splitlines():
        name = zeile.split("=", 1)[0].strip() if "=" in zeile and not zeile.lstrip().startswith("#") else None
        if name in aenderungen:
            gesehen.add(name)
            if aenderungen[name] is not None:
                zeilen.append(f"{name}={aenderungen[name]}")
            continue
        zeilen.append(zeile)
    zeilen += [f"{n}={v}" for n, v in aenderungen.items() if n not in gesehen and v is not None]
    _sicher_schreiben(pfad, "\n".join(zeilen) + "\n")


# ── 7. Installieren ───────────────────────────────────────────────────────────
def _compose(*args: str) -> list[str]:
    dateien = ["-f", str(ZIEL / "compose.yml")]
    # Ergänzung des Installers: Datenspeicher und Austauschordner (speicher.py)
    if (ZIEL / "compose.lokal.yml").exists():
        dateien += ["-f", str(ZIEL / "compose.lokal.yml")]
    return ["docker", "compose", "-p", PROJEKT, *dateien, "--env-file", str(ZIEL / ".env"), *args]


def _manifest(log) -> tuple[dict, dict]:
    a = _aktivierung()
    antwort = _anfrage("GET", "/api/v1/install/release", token=a["install_token"])
    roh = _b64(antwort["manifest"])
    _pruefen(_b64(antwort["signatur"]), roh, "Manifest der Cloud")
    manifest = json.loads(roh)
    for name in ("api", "web"):
        if not re.match(r"^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", manifest["images"].get(name, "")):
            raise Fehler(f"Das Manifest nennt kein festes Image für {name}.")
    log(f"Version {manifest['version']} – Manifest der Cloud geprüft.")
    return manifest, antwort["registry"]


def _warten(bedingung, sekunden: float, pause: float = 3):
    ende = time.monotonic() + sekunden
    while time.monotonic() < ende:
        try:
            ergebnis = bedingung()
            if ergebnis:
                return ergebnis
        except Exception:
            pass
        time.sleep(pause)
    return None


def _http_ok(url: str) -> dict | None:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read()) if r.status == 200 else None


LIZENZ_PRUEFUNG = """
import asyncio
from sqlalchemy import text
from app.core.database import AsyncSessionLocal
from app.platform import studiolizenz
async def m():
    async with AsyncSessionLocal() as db:
        t = await db.scalar(text("SELECT id FROM tenants ORDER BY created_at LIMIT 1"))
        z = await studiolizenz.zustand(db, t)
        print("OK" if z.lizenziert and not z.gesperrt else "NEIN", t)
asyncio.run(m())
"""


def installieren(log) -> dict:
    import betrieb
    import speicher
    a = _aktivierung()
    f = _fernwartung()
    if not (ZIEL / ".env").exists():
        raise Fehler("Erst die Konfiguration speichern.")
    if not speicher.speicher_stand()["festgelegt"]:
        raise Fehler("Erst den Datenspeicher wählen (Schritt Speicher & Sicherung).")
    wahl = _zustand_lesen("wiederherstellung.json")
    if wahl.get("wahl") not in ("neu", "sicherung"):
        raise Fehler("Erst wählen: neu beginnen oder aus einer Sicherung wiederherstellen.")
    manifest, registry = _manifest(log)
    version = manifest["version"]
    if wahl["wahl"] == "sicherung":
        alt = (wahl.get("manifest") or {}).get("version") or "0"
        if betrieb._versionsschluessel(alt) > betrieb._versionsschluessel(version):
            raise Fehler(f"Die Sicherung stammt von Version {alt}, freigegeben ist erst {version}. "
                         "Bitte den Plattform-Betreiber ansprechen.")

    # Anmeldedaten nur in einem Wegwerf-Ordner – sie landen nie in /root/.docker.
    with tempfile.TemporaryDirectory(prefix="studio-docker-") as konfig:
        env = {"DOCKER_CONFIG": konfig}
        _ausfuehren(["docker", "login", registry["server"], "--username", registry["benutzer"],
                     "--password-stdin"], log, eingabe=registry["token"], env=env)
        for name in ("api", "web"):
            log(f"Lade {name} …")
            _ausfuehren(["docker", "pull", manifest["images"][name]], log, env=env)
            _ausfuehren(["docker", "tag", manifest["images"][name], f"studio-{name}:{version}"], log)
        _ausfuehren(["docker", "logout", registry["server"]], log, env=env, pruefen=False)

    log("Edge-Paket aus dem Image auspacken …")
    _ausfuehren(["docker", "rm", "-f", "studio-paket"], pruefen=False)
    _ausfuehren(["docker", "create", "--name", "studio-paket", f"studio-api:{version}"], log)
    try:
        _ausfuehren(["docker", "cp", "studio-paket:/app/edge-paket/.", str(ZIEL)], log)
    finally:
        _ausfuehren(["docker", "rm", "-f", "studio-paket"], pruefen=False)
    drin = (ZIEL / "VERSION").read_text().strip() if (ZIEL / "VERSION").exists() else ""
    if drin != version or not (ZIEL / "compose.yml").exists():
        raise Fehler(f"Im Image steckt kein passendes Edge-Paket (gefunden: {drin or 'nichts'}).")

    env_setzen({"STUDIO_API_IMAGE": "studio-api", "STUDIO_WEB_IMAGE": "studio-web", "STUDIO_VERSION": version})
    speicher.austausch_anlegen()
    speicher.compose_lokal_schreiben()
    if wahl["wahl"] == "sicherung":
        sicherung_einspielen(wahl, log)
    if f["wahl"] == "plattform":
        try:
            ts = _anfrage("POST", "/api/v1/install/tailscale", token=a["install_token"])
            env_setzen({"TAILSCALE_AUTH_KEY": ts["authkey"]})
            log("Schlüssel für die Fernwartung von der Plattform erhalten.")
        except Fehler as exc:
            log(f"WARNUNG: Kein Schlüssel für die Fernwartung ({exc}). Studio startet trotzdem.")
    log("Studio starten …")
    _ausfuehren(_compose("up", "-d", "--no-build", "--remove-orphans"), log, cwd=ZIEL)

    log("Warte, bis Studio antwortet (beim ersten Start richtet es die Datenbank ein) …")
    info = _warten(lambda: _http_ok("http://127.0.0.1/api/v1/system/info"), 420)
    if not info:
        raise Fehler("Studio antwortet nicht. Details: docker compose -p studio-edge logs api")
    log(f"Studio {info.get('core_version')} läuft.")

    log("Warte auf Verbindung zur Cloud und die Studio-Lizenz …")
    lizenz = _warten(lambda: "OK" in _ausfuehren(_compose("exec", "-T", "api", "python", "-c", LIZENZ_PRUEFUNG),
                                                 cwd=ZIEL, pruefen=False), 180, pause=10)
    if lizenz:
        log("Mit der Cloud verbunden, Lizenz aktiv.")
    else:
        log("WARNUNG: Die Lizenz ist noch nicht aktiv. Studio versucht es jede Minute weiter.")

    tailscale = None
    if f["wahl"] != "aus":
        log("Warte auf die Anmeldung bei Tailscale …")
        ip = _warten(_tailnet_adresse, 120, pause=5)
        if ip:
            tailscale = {"ip": ip, "name": f["name"]}
            # Der Knotenschlüssel liegt im Volume – den Anmeldeschlüssel braucht es nicht mehr.
            env_setzen({"TAILSCALE_IP": ip, "TAILSCALE_NAME": f["name"], "TAILSCALE_AUTH_KEY": None})
            _ausfuehren(_compose("up", "-d", "--no-build", "api"), log, cwd=ZIEL)
            log(f"Fernwartung aktiv: {f['name']} ({ip}).")
        else:
            log("WARNUNG: Tailscale hat sich nicht angemeldet – Studio läuft trotzdem. "
                "Schlüssel prüfen (abgelaufen? schon verbraucht?).")

    log("Sicherung und Updates einschalten …")
    _ausfuehren(["systemctl", "enable", "--now", "studio-sicherung.timer", "studio-update.timer",
                 "studio-auftrag.path"], log, pruefen=False)
    betrieb.status_schreiben()

    zugang = anmeldedaten()
    ergebnis = {"version": version, "studio": a["tenant_name"], "lizenz_aktiv": bool(lizenz),
                "fernwartung": f["wahl"], "tailscale": tailscale,
                "wiederhergestellt": wahl.get("manifest", {}).get("erstellt") if wahl["wahl"] == "sicherung" else None,
                "installiert_at": datetime.now(timezone.utc).isoformat()}
    _sicher_schreiben(ZIEL / "installiert.json", json.dumps(ergebnis, indent=2), 0o644)
    # Tokens der Aktivierung werden nicht mehr gebraucht; die .env hat, was Studio braucht.
    for name in ("aktivierung.json", "fernwartung.json", "cloud-env.json", "wiederherstellung.json"):
        (ZUSTAND / name).unlink(missing_ok=True)
    shutil.rmtree(ZUSTAND / "wiederherstellung", ignore_errors=True)
    WIEDERHERSTELLUNGSBLATT.clear()
    # Der Assistent startet ab jetzt nicht mehr beim Hochfahren; `studio-einrichtung` holt ihn zurück.
    _ausfuehren(["systemctl", "disable", "studio-setup.service"], log, pruefen=False)
    log("Fertig.")
    return {**ergebnis, "zugang": zugang}


def _tailnet_adresse() -> str | None:
    aus = _ausfuehren(_compose("exec", "-T", "tailscale", "tailscale", "ip", "-4"), cwd=ZIEL, pruefen=False)
    return next((z.strip() for z in aus.splitlines() if re.match(r"^100\.\d+\.\d+\.\d+$", z.strip())), None)


def anmeldedaten() -> dict | None:
    """Das Startpasswort des SystemOwners, solange es die Datei im Studio noch gibt."""
    try:
        text = _ausfuehren(_compose("exec", "-T", "api", "cat", "/data/initial-credentials.txt"),
                           cwd=ZIEL, pruefen=False)
    except Exception:
        return None
    werte = dict(z.split(": ", 1) for z in text.splitlines() if ": " in z)
    if "Benutzer" not in werte or "Startpasswort" not in werte:
        return None
    return {"benutzer": werte["Benutzer"], "passwort": werte["Startpasswort"]}


def installiert() -> dict | None:
    try:
        return json.loads((ZIEL / "installiert.json").read_text())
    except (OSError, ValueError):
        return None


# ── Sicherungsschlüssel und Wiederherstellung (Schritt „Speicher & Sicherung“) ──
#: Das Wiederherstellungsblatt nur im Speicher dieses Dienstes – bis die Einrichtung fertig
#: ist, kann man es erneut anzeigen und drucken. Auf der Platte liegt der private Schlüssel nie.
WIEDERHERSTELLUNGSBLATT: dict = {}


def sicherungsschluessel_einrichten() -> dict:
    """Einen Schlüssel je Studio. Hat die Cloud schon einen (frühere Installation), gilt er
    weiter – das Blatt von damals öffnet auch die alten Sicherungen. Sonst entsteht hier ein
    neues Paar: der private Teil geht verschlüsselt an die Plattform und einmal aufs Blatt."""
    import betrieb
    import speicher
    a = _aktivierung()
    stand = _anfrage("GET", "/api/v1/install/sicherung", token=a["install_token"])
    if stand.get("oeffentlicher_schluessel"):
        betrieb.oeffentlichen_schluessel_setzen(stand["oeffentlicher_schluessel"])
        return {"vorhanden": True, "neu": False, "fingerabdruck": betrieb._fingerabdruck(stand["oeffentlicher_schluessel"])}
    pub, privat = betrieb.schluessel_erzeugen()
    _anfrage("POST", "/api/v1/install/sicherung/schluessel", token=a["install_token"],
             daten={"oeffentlich": pub, "privat": privat})
    betrieb.oeffentlichen_schluessel_setzen(pub)
    WIEDERHERSTELLUNGSBLATT.update({"privat": privat, "oeffentlich": pub, "studio": a.get("tenant_name"),
                                    "erstellt": date.today().strftime("%d.%m.%Y"),
                                    "fingerabdruck": betrieb._fingerabdruck(pub)})
    speicher.sicherung_config_schreiben({**speicher.sicherung_config(), "cloud": True})
    return {"vorhanden": True, "neu": True, "fingerabdruck": betrieb._fingerabdruck(pub)}


def wiederherstellungsblatt() -> dict | None:
    if not WIEDERHERSTELLUNGSBLATT:
        return None
    blatt = dict(WIEDERHERSTELLUNGSBLATT)
    try:
        import qrcode
        import qrcode.image.svg
        bild = qrcode.make(blatt["privat"], image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
        blatt["qr_svg"] = bild.to_string(encoding="unicode")
    except Exception:  # python3-qrcode fehlt: das Blatt geht auch ohne QR-Code
        blatt["qr_svg"] = None
    return blatt


def sicherungen_auflisten() -> dict:
    """Sicherungen dieses Studios: in der Cloud und am Backup-Ziel vor Ort."""
    import speicher
    a = _aktivierung()
    stand = _anfrage("GET", "/api/v1/install/sicherung", token=a["install_token"])
    cloud = [{"quelle": "cloud", **s} for s in stand.get("sicherungen") or []]
    vor_ort = []
    if speicher.backup_ziel_stand().get("art") not in (None, "spaeter"):
        try:
            for ordner in sorted(p for p in speicher.BACKUP_PFAD.iterdir() if p.is_dir()):
                for datei in sorted(ordner.glob("studio-*.tar.gz.age"), reverse=True)[:30]:
                    vor_ort.append({"quelle": "ziel", "id": f"{ordner.name}/{datei.name}", "name": datei.name,
                                    "groesse": datei.stat().st_size,
                                    "erstellt": datetime.fromtimestamp(datei.stat().st_mtime, timezone.utc).isoformat()})
        except OSError:
            pass
    return {"cloud": cloud, "vor_ort": vor_ort, "freigabe_aktiv": bool(stand.get("freigabe_aktiv")),
            "schluessel_vorhanden": bool(stand.get("oeffentlicher_schluessel"))}


def neu_beginnen() -> dict:
    _zustand_schreiben("wiederherstellung.json", {"wahl": "neu"})
    return {"wahl": "neu"}


def sicherung_laden(eingaben: dict, log) -> dict:
    """Eine Sicherung holen, entschlüsseln und prüfen. Läuft als Auftrag (Download)."""
    import speicher
    a = _aktivierung()
    ordner = ZUSTAND / "wiederherstellung"
    shutil.rmtree(ordner, ignore_errors=True)
    ordner.mkdir(parents=True, mode=0o700)
    quelle, kennung = eingaben.get("quelle"), str(eingaben.get("id") or "")
    archiv = ordner / "sicherung.tar.gz.age"
    if quelle == "cloud":
        if not re.fullmatch(r"[0-9a-f-]{36}", kennung):
            raise Fehler("Unbekannte Sicherung.")
        log("Sicherung aus der Cloud laden …")
        req = urllib.request.Request(f"{CLOUD}/api/v1/install/sicherung/{kennung}", headers={
            "Authorization": f"Bearer {a['install_token']}", "User-Agent": "studio-installer"})
        try:
            with urllib.request.urlopen(req, timeout=600) as antwort, open(archiv, "wb") as aus:
                shutil.copyfileobj(antwort, aus, 1 << 20)
        except (urllib.error.URLError, OSError) as exc:
            raise Fehler(f"Download der Sicherung gescheitert ({exc}).") from exc
    elif quelle == "ziel":
        if not re.fullmatch(r"[A-Za-z0-9._-]+/studio-[A-Za-z0-9._-]+\.tar\.gz\.age", kennung):
            raise Fehler("Unbekannte Sicherung.")
        datei = speicher.BACKUP_PFAD / kennung
        if not datei.is_file():
            raise Fehler("Die Sicherung ist am Backup-Ziel nicht (mehr) da.")
        log(f"Sicherung vom Backup-Ziel kopieren ({datei.name}) …")
        shutil.copy2(datei, archiv)
    else:
        raise Fehler("Bitte eine Sicherung wählen.")

    privat = "".join((eingaben.get("schluessel") or "").split()).upper()
    if eingaben.get("plattform"):
        log("Schlüssel bei der Plattform anfordern …")
        privat = _anfrage("GET", "/api/v1/install/sicherung/schluessel", token=a["install_token"])["privat"]
    if not re.fullmatch(r"AGE-SECRET-KEY-1[0-9A-Z]{58}", privat):
        raise Fehler("Der Schlüssel vom Wiederherstellungsblatt beginnt mit AGE-SECRET-KEY-1 (74 Zeichen).")
    schluessel = ordner / "schluessel.txt"
    fd = os.open(schluessel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(privat + "\n")
    try:
        log("Entschlüsseln und auspacken …")
        entpackt = ordner / "inhalt"
        entpackt.mkdir(mode=0o700)
        age = subprocess.Popen(["age", "-d", "-i", str(schluessel), str(archiv)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
        tar = subprocess.run(["tar", "-xzf", "-", "-C", str(entpackt)], stdin=age.stdout, capture_output=True)
        age.stdout.close()
        age.wait()
        if age.returncode != 0:
            raise Fehler("Der Schlüssel passt nicht zu dieser Sicherung.")
        if tar.returncode != 0:
            raise Fehler("Die Sicherung ist beschädigt (tar).")
    finally:
        schluessel.unlink(missing_ok=True)
    archiv.unlink(missing_ok=True)
    try:
        manifest = json.loads((ordner / "inhalt" / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise Fehler("Der Sicherung fehlt das Inhaltsverzeichnis.") from exc
    for name, soll in (manifest.get("dateien") or {}).items():
        datei = ordner / "inhalt" / name
        if not datei.exists() or hashlib.sha256(datei.read_bytes()).hexdigest() != soll["sha256"]:
            raise Fehler(f"Prüfsumme von {name} stimmt nicht – die Sicherung ist beschädigt.")
    if manifest.get("tenant_id") and manifest["tenant_id"] != a.get("tenant_id"):
        raise Fehler("Diese Sicherung gehört zu einem anderen Studio.")
    log(f"Sicherung vom {manifest.get('erstellt')} (Version {manifest.get('version')}) geprüft.")
    wahl = {"wahl": "sicherung", "quelle": quelle, "id": kennung, "manifest": manifest}
    _zustand_schreiben("wiederherstellung.json", wahl)
    return wahl


def sicherung_einspielen(wahl: dict, log) -> None:
    """Datenbank und Dateien der gewählten Sicherung in die frische Installation einspielen.
    Danach startet Studio im Wiederherstellungsmodus: die Kasse bleibt gesperrt, bis der
    Abgleich mit Cloud und TSE fertig ist (Studio → Backup → Wiederherstellung abgleichen)."""
    import betrieb
    inhalt = ZUSTAND / "wiederherstellung" / "inhalt"
    if not (inhalt / "db.dump").exists():
        raise Fehler("Die geladene Sicherung ist nicht mehr da. Bitte im Schritt Speicher & Sicherung neu laden.")
    alt_env = env_lesen((inhalt / "konfig" / ".env").read_text()) if (inhalt / "konfig" / ".env").exists() else {}
    # Verschlüsselte Zugangsdaten in der Datenbank (fiskaly, Mail …) hängen an SECRETS_KEY.
    env_setzen({k: alt_env[k] for k in ("SECRETS_KEY", "JWT_SECRET_KEY") if alt_env.get(k)})
    log("Datenbank starten …")
    _ausfuehren(_compose("up", "-d", "db"), log, cwd=ZIEL)
    if not _warten(lambda: subprocess.run(_compose("exec", "-T", "db", "pg_isready", "-U", "studio", "-d", "studio"),
                                          capture_output=True, cwd=ZIEL).returncode == 0, 180, pause=3):
        raise Fehler("Die Datenbank startet nicht.")
    leer = _ausfuehren(_compose("exec", "-T", "db", "psql", "-U", "studio", "-d", "studio", "-Atc",
                                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"), cwd=ZIEL).strip()
    if leer not in ("0", ""):
        raise Fehler("Die Datenbank auf diesem PC ist nicht leer – eine Sicherung wird nur in eine frische "
                     "Installation eingespielt.")
    log("Datenbank aus der Sicherung einspielen …")
    with open(inhalt / "db.dump", "rb") as ein:
        proc = subprocess.run(_compose("exec", "-T", "db", "pg_restore", "-U", "studio", "-d", "studio",
                                       "--no-owner", "--exit-on-error"), stdin=ein, capture_output=True, cwd=ZIEL)
    if proc.returncode != 0:
        raise Fehler("Datenbank einspielen fehlgeschlagen: " + proc.stderr.decode(errors="replace")[-300:])
    log("Dateien aus der Sicherung einspielen …")
    with open(inhalt / "daten.tar", "rb") as ein:
        proc = subprocess.run(_compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "tar", "api",
                                       "-C", "/data", "-xf", "-"), stdin=ein, capture_output=True, cwd=ZIEL)
    if proc.returncode != 0:
        raise Fehler("Dateien einspielen fehlgeschlagen: " + proc.stderr.decode(errors="replace")[-300:])
    m = wahl.get("manifest") or {}
    marke = {"status": "abgleich_offen", "sicherung_erstellt": m.get("erstellt"), "sicherung_version": m.get("version"),
             "anlass": m.get("anlass"), "quelle": wahl.get("quelle"), "datei": wahl.get("id"),
             "kasse": m.get("kasse") or {}, "eingespielt_at": datetime.now(timezone.utc).isoformat(),
             "rechner_alt": m.get("rechner")}
    proc = subprocess.run(_compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "sh", "api", "-c",
                                   "cat > /data/wiederherstellung.json"),
                          input=json.dumps(marke, indent=2).encode(), capture_output=True, cwd=ZIEL)
    if proc.returncode != 0:
        raise Fehler("Die Wiederherstellungsmarke ließ sich nicht schreiben.")
    log("Sicherung eingespielt. Studio startet im Wiederherstellungsmodus – die Kasse bleibt gesperrt, bis "
        "der Abgleich mit Cloud und TSE fertig ist.")
    betrieb.status_schreiben()
