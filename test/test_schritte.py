"""Tests der Installer-Logik ohne root, Docker und Cloud:  python3 -m unittest discover -s test"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "paket/opt/studio-installer"))
import schritte  # noqa: E402


def b64(roh: bytes) -> str:
    return base64.urlsafe_b64encode(roh).decode().rstrip("=")


class MitSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="studio-installer-test-"))
        self.alt = (schritte.HIER, schritte.ZIEL, schritte.ZUSTAND, schritte._anfrage)
        schritte.HIER, schritte.ZIEL, schritte.ZUSTAND = self.tmp, self.tmp / "opt", self.tmp / "zustand"
        self.privat = Ed25519PrivateKey.generate()
        roh = self.privat.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        (self.tmp / "cloud-key.pub").write_text(b64(roh) + "\n")
        self.anfragen = []

    def tearDown(self):
        schritte.HIER, schritte.ZIEL, schritte.ZUSTAND, schritte._anfrage = self.alt

    def schluessel(self, app="studio", exp=None) -> str:
        nutzlast = json.dumps({"v": 1, "lid": "l1", "tenant": "t1", "app": app, "exp": exp,
                               "iat": date.today().isoformat()}, separators=(",", ":"), sort_keys=True).encode()
        return f"STUDIO-1.{b64(nutzlast)}.{b64(self.privat.sign(nutzlast))}"

    def aktiviert(self, fernwartung="plattform", konfiguration=False):
        bis = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        schritte._zustand_schreiben("aktivierung.json", {
            "tenant_id": "0f0e0d0c-0b0a-4000-8000-000000000001", "tenant_name": "Teststudio",
            "sync_upstream_url": "https://ki-sunlounge.de", "sync_token": "studio_edge_x_geheim",
            "install_token": "install", "install_token_gueltig_bis": bis,
            "konfiguration_vorhanden": konfiguration, "tailscale_verfuegbar": True,
            "tailscale_authkey": "tskey-auth-plattform-123456", "tailscale_hostname": "studio-teststudio"})
        schritte.fernwartung_waehlen(fernwartung, "tskey-auth-eigener-1234567" if fernwartung == "eigen" else "")

        def anfrage(methode, pfad, **kw):
            self.anfragen.append((methode, pfad, kw))
            return {}
        schritte._anfrage = anfrage


class EnvTest(MitSandbox):
    def test_lesen_und_schreiben_passen_zusammen(self):
        werte = {"STUDIO_ENV": "production", "SYNC_TOKEN": "abc", "EIGENES": "x=y"}
        self.assertEqual(schritte.env_lesen(schritte.env_text(werte)), werte)

    def test_setzen_aendert_entfernt_und_ergaenzt(self):
        (schritte.ZIEL).mkdir(parents=True)
        (schritte.ZIEL / ".env").write_text("# Kopf\nA=1\nB=2\n")
        schritte.env_setzen({"A": "neu", "B": None, "C": "3"})
        self.assertEqual((schritte.ZIEL / ".env").read_text(), "# Kopf\nA=neu\nC=3\n")


class LizenzTest(MitSandbox):
    def test_echter_schluessel_geht_auch_mit_umbruechen(self):
        s = self.schluessel()
        self.assertEqual(schritte.lizenz_vorpruefen(s[:40] + "\n  " + s[40:])["app"], "studio")

    def test_fremde_signatur_app_lizenz_und_abgelaufen(self):
        fremd = Ed25519PrivateKey.generate()
        teile = self.schluessel().split(".")
        nutzlast = base64.urlsafe_b64decode(teile[1] + "==")
        gefaelscht = f"STUDIO-1.{teile[1]}.{b64(fremd.sign(nutzlast))}"
        for falsch, text in ((gefaelscht, "Signatur"), (self.schluessel(app="club"), "App-Lizenz"),
                             (self.schluessel(exp="2020-01-01"), "abgelaufen"), ("irgendwas", "STUDIO-1.")):
            with self.assertRaises(schritte.Fehler) as ctx:
                schritte.lizenz_vorpruefen(falsch)
            self.assertIn(text, str(ctx.exception))


class KonfigurationTest(MitSandbox):
    def test_neue_konfiguration_mit_plattform_fernwartung(self):
        self.aktiviert("plattform")
        schritte.konfiguration_speichern({"karte": "enp3s0", "lan_ip": "192.168.178.20", "geraetenetz": True,
                                          "marke": "TESTSTUDIO", "akzent": "No.1"})
        env = schritte.env_lesen((schritte.ZIEL / ".env").read_text())
        self.assertEqual(env["COMPOSE_PROFILES"], "geraetenetz,fernwartung")
        self.assertEqual(env["TAILSCALE_AUTH_KEY"], "tskey-auth-plattform-123456")
        self.assertEqual(env["SYNC_TOKEN"], "studio_edge_x_geheim")
        self.assertEqual(env["EDGE_TENANT_ID"], "0f0e0d0c-0b0a-4000-8000-000000000001")
        self.assertEqual(env["QR_TUER_SERVER_IP"], "192.168.178.20")
        self.assertIn(env["POSTGRES_PASSWORD"], env["DATABASE_URL"])
        self.assertGreaterEqual(len(env["SECRETS_KEY"]), 32)
        self.assertEqual(oct((schritte.ZIEL / ".env").stat().st_mode & 0o777), "0o600")
        methode, pfad, kw = self.anfragen[-1]
        self.assertEqual((methode, pfad, kw["daten"]["fernwartung"]), ("PUT", "/api/v1/install/konfiguration", "plattform"))

    def test_ohne_fernwartung_kein_tailscale_und_geheimnisse_bleiben(self):
        self.aktiviert("aus")
        schritte.konfiguration_speichern({"karte": "enp3s0", "lan_ip": ""})
        erst = schritte.env_lesen((schritte.ZIEL / ".env").read_text())
        self.assertEqual(erst["COMPOSE_PROFILES"], "")
        self.assertEqual(erst["TAILSCALE_AUTH_KEY"], "")
        # Zweiter Lauf auf demselben PC: die Datenbank kennt ihr Passwort schon.
        schritte.konfiguration_speichern({"karte": "enp3s0", "lan_ip": ""})
        zweit = schritte.env_lesen((schritte.ZIEL / ".env").read_text())
        for k in schritte.GEHEIME_VORGABEN:
            self.assertEqual(erst[k], zweit[k])

    def test_hinterlegte_konfiguration_der_cloud_gewinnt_ausser_bei_verbindung(self):
        self.aktiviert("eigen", konfiguration=True)
        schritte._zustand_schreiben("cloud-env.json", {"env": "BRAND_NAME=AUS-DER-CLOUD\nSYNC_TOKEN=alt\n"
                                                              "POSTGRES_PASSWORD=p\nDATABASE_URL=postgresql+asyncpg://studio:p@db:5432/studio\n"
                                                              "JWT_SECRET_KEY=j\nSECRETS_KEY=s\nCOMPOSE_PROFILES=geraetenetz\n"})
        schritte.konfiguration_speichern({"marke": "WIRD-IGNORIERT"})
        env = schritte.env_lesen((schritte.ZIEL / ".env").read_text())
        self.assertEqual(env["BRAND_NAME"], "AUS-DER-CLOUD")
        self.assertEqual(env["SYNC_TOKEN"], "studio_edge_x_geheim")
        self.assertEqual(env["POSTGRES_PASSWORD"], "p")
        self.assertEqual(env["COMPOSE_PROFILES"], "geraetenetz,fernwartung")
        self.assertEqual(env["TAILSCALE_AUTH_KEY"], "tskey-auth-eigener-1234567")

    def test_falsche_eingaben(self):
        self.aktiviert("aus")
        with self.assertRaises(schritte.Fehler):
            schritte.konfiguration_speichern({"karte": "enp3s0; rm -rf /"})
        with self.assertRaises(schritte.Fehler):
            schritte.konfiguration_speichern({"karte": "enp3s0", "lan_ip": "192.168.1"})
        with self.assertRaises(schritte.Fehler):
            schritte.fernwartung_waehlen("eigen", "kein-schluessel")


if __name__ == "__main__":
    unittest.main()
