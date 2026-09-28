#!/usr/bin/env python3
"""Studio-Einrichtung: lokaler Assistent unter http://127.0.0.1:8099 (Dienst studio-setup).

Läuft als root, weil er Docker installiert und /opt/studio schreibt. Deshalb nimmt er nur
Anfragen an, die
  - an 127.0.0.1/localhost:8099 gehen (Host-Header: kein DNS-Rebinding),
  - das Sitzungstoken aus /run/studio-setup/token tragen (lesbar nur für root und die
    Gruppe sudo – der Starter /usr/bin/studio reicht es an den Browser weiter),
  - bei API-Aufrufen den Kopf X-Studio-Setup mitschicken und keine fremde Origin haben.
Eine fremde Webseite im selben Browser kommt damit nicht an den Assistenten.

Lange Schritte (Docker, Installation) laufen als Auftrag im Hintergrund; die Oberfläche
fragt das Protokoll ab.
"""

from __future__ import annotations

import grp
import json
import mimetypes
import os
import secrets
import sys
import threading
import traceback
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schritte  # noqa: E402

ADRESSE, PORT = "127.0.0.1", 8099
WEB = Path(__file__).resolve().parent / "web"
TOKEN_DATEI = Path(os.environ.get("STUDIO_SETUP_TOKEN", "/run/studio-setup/token"))
ERLAUBTE_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ERLAUBTE_ORIGINS = {f"http://{h}" for h in ERLAUBTE_HOSTS}
TOKEN = secrets.token_urlsafe(32)


def token_ablegen() -> None:
    TOKEN_DATEI.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_DATEI, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w") as fh:
        fh.write(TOKEN)
    try:
        gid = grp.getgrnam("sudo").gr_gid
        os.chown(TOKEN_DATEI.parent, 0, gid)
        os.chmod(TOKEN_DATEI.parent, 0o750)
        os.chown(TOKEN_DATEI, 0, gid)
    except (KeyError, PermissionError):
        pass


# ── Hintergrundauftrag ────────────────────────────────────────────────────────
class Auftrag:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.name: str | None = None
        self.laeuft = False
        self.fehler: str | None = None
        self.ergebnis: dict | None = None
        self.zeilen: list[str] = []

    def log(self, zeile: str) -> None:
        with self.lock:
            self.zeilen.append(zeile)
            del self.zeilen[:-5000]

    def starten(self, name: str, arbeit) -> None:
        with self.lock:
            if self.laeuft:
                raise schritte.Fehler(f"Es läuft schon: {self.name}.")
            self.name, self.laeuft, self.fehler, self.ergebnis, self.zeilen = name, True, None, None, []

        def lauf():
            try:
                ergebnis = arbeit(self.log)
                with self.lock:
                    self.ergebnis = ergebnis if isinstance(ergebnis, dict) else {"ok": True}
            except schritte.Fehler as exc:
                self.log(f"FEHLER: {exc}")
                with self.lock:
                    self.fehler = str(exc)
            except Exception as exc:  # unerwartet – ins Protokoll, damit man es findet
                self.log(traceback.format_exc())
                with self.lock:
                    self.fehler = f"Unerwarteter Fehler: {exc}"
            finally:
                with self.lock:
                    self.laeuft = False

        threading.Thread(target=lauf, daemon=True).start()

    def stand(self, seit: int = 0) -> dict:
        with self.lock:
            return {"name": self.name, "laeuft": self.laeuft, "fehler": self.fehler,
                    "ergebnis": self.ergebnis, "zeilen": self.zeilen[seit:], "gesamt": len(self.zeilen)}


AUFTRAG = Auftrag()


def status() -> dict:
    a = schritte.aktivierung_oeffentlich()
    return {
        "ist_image": schritte.IST_IMAGE,
        "passwort_gesetzt": schritte.passwort_gesetzt(),
        "docker": schritte.docker_vorhanden(),
        "aktivierung": a,
        "fernwartung": (lambda f: {"wahl": f.get("wahl"), "name": f.get("name")} if f else None)(
            schritte._zustand_lesen("fernwartung.json")),
        "konfiguration": (schritte.ZIEL / ".env").exists() and a is not None,
        "installiert": schritte.installiert(),
        "auftrag": AUFTRAG.stand(10**9),  # nur der Kopf, keine Zeilen
        "cloud": schritte.CLOUD,
    }


# ── HTTP ──────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "studio-setup"

    def log_message(self, fmt, *args):  # kein Zugriffslog mit Tokens in der URL
        pass

    def _senden(self, code: int, rumpf: bytes, typ: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(rumpf)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(rumpf)

    def _json(self, code: int, daten) -> None:
        self._senden(code, json.dumps(daten, default=str).encode(), "application/json; charset=utf-8")

    def _cookie_ok(self) -> bool:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        wert = c.get("studio_setup")
        return bool(wert) and secrets.compare_digest(wert.value, TOKEN)

    def _vorab(self) -> bool:
        if self.headers.get("Host", "") not in ERLAUBTE_HOSTS:
            self._senden(421, b"Falscher Host", "text/plain; charset=utf-8")
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in ERLAUBTE_ORIGINS:
            self._senden(403, b"Fremde Herkunft", "text/plain; charset=utf-8")
            return False
        return True

    def _api_ok(self) -> bool:
        if not self._cookie_ok() or self.headers.get("X-Studio-Setup") != "1":
            self._json(401, {"detail": "Sitzung ungültig – bitte über das Studio-Icon neu öffnen."})
            return False
        return True

    def do_GET(self):
        if not self._vorab():
            return
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            if not self._api_ok():
                return
            return self._api_get(url)
        # Einstieg über den Starter: ?t=<token> wird zum Cookie, die URL danach ohne Token.
        t = parse_qs(url.query).get("t", [""])[0]
        if t:
            if not secrets.compare_digest(t, TOKEN):
                return self._senden(401, "Sitzung ungültig – bitte über das Studio-Icon öffnen.".encode(),
                                    "text/plain; charset=utf-8")
            return self._senden(303, b"", "text/plain", {
                "Location": "/",
                "Set-Cookie": f"studio_setup={TOKEN}; HttpOnly; SameSite=Strict; Path=/"})
        if not self._cookie_ok():
            return self._senden(401, "Bitte den Assistenten über das Studio-Icon öffnen.".encode(),
                                "text/plain; charset=utf-8")
        datei = (WEB / (url.path.lstrip("/") or "index.html")).resolve()
        if WEB not in datei.parents or not datei.is_file():
            datei = WEB / "index.html"
        typ = mimetypes.guess_type(datei.name)[0] or "application/octet-stream"
        if typ.startswith("text/") or typ in ("application/javascript",):
            typ += "; charset=utf-8"
        self._senden(200, datei.read_bytes(), typ)

    def do_POST(self):
        if not self._vorab() or not self._api_ok():
            return
        laenge = int(self.headers.get("Content-Length") or 0)
        if laenge > 256 * 1024:
            return self._json(413, {"detail": "Zu groß."})
        try:
            daten = json.loads(self.rfile.read(laenge) or b"{}")
        except ValueError:
            return self._json(400, {"detail": "Kein JSON."})
        try:
            self._json(200, self._api_post(urlparse(self.path).path, daten))
        except schritte.Fehler as exc:
            self._json(422, {"detail": str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"detail": f"Unerwarteter Fehler: {exc}"})

    def _api_get(self, url):
        try:
            if url.path == "/api/status":
                return self._json(200, status())
            if url.path == "/api/auftrag":
                seit = int(parse_qs(url.query).get("seit", ["0"])[0] or 0)
                return self._json(200, AUFTRAG.stand(seit))
            if url.path == "/api/vorschlaege":
                return self._json(200, schritte.vorschlaege())
            if url.path == "/api/anmeldedaten":
                return self._json(200, {"zugang": schritte.anmeldedaten()})
            if url.path == "/api/hardware":
                return self._json(200, {"hardware_id": schritte.hardware_id()})
            self._json(404, {"detail": "Unbekannt."})
        except schritte.Fehler as exc:
            self._json(422, {"detail": str(exc)})

    def _api_post(self, pfad: str, d: dict):
        if pfad == "/api/passwort":
            schritte.passwort_setzen(str(d.get("passwort", "")))
            return {"ok": True}
        if pfad == "/api/internet":
            return schritte.internet_pruefen()
        if pfad == "/api/docker":
            AUFTRAG.starten("Docker installieren", schritte.docker_installieren)
            return {"gestartet": True}
        if pfad == "/api/lizenz/pruefen":
            daten = schritte.lizenz_vorpruefen(str(d.get("schluessel", "")))
            return {"gueltig_bis": daten.get("exp"), "tenant": daten.get("tenant")}
        if pfad == "/api/aktivieren":
            return schritte.aktivieren(str(d.get("schluessel", "")))
        if pfad == "/api/fernwartung":
            return schritte.fernwartung_waehlen(str(d.get("wahl", "")), str(d.get("schluessel", "")),
                                                str(d.get("name", "")))
        if pfad == "/api/konfiguration":
            return schritte.konfiguration_speichern(d)
        if pfad == "/api/installieren":
            AUFTRAG.starten("Studio installieren", schritte.installieren)
            return {"gestartet": True}
        raise schritte.Fehler("Unbekannter Schritt.")


def main() -> None:
    token_ablegen()
    schritte.ZUSTAND.mkdir(mode=0o700, parents=True, exist_ok=True)
    server = ThreadingHTTPServer((ADRESSE, PORT), Handler)
    print(f"Studio-Einrichtung läuft auf http://{ADRESSE}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
