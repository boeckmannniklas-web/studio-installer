"""Tests der Installer-Logik ohne root, Docker und Cloud:  python3 -m unittest discover -s test"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "paket/opt/studio-installer"))
import betrieb  # noqa: E402
import schritte  # noqa: E402
import speicher  # noqa: E402


def b64(roh: bytes) -> str:
    return base64.urlsafe_b64encode(roh).decode().rstrip("=")


class MitSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="studio-installer-test-"))
        self.alt = (schritte.HIER, schritte.ZIEL, schritte.ZUSTAND, schritte._anfrage)
        self.alt_speicher = (speicher.ETC, speicher.FSTAB, speicher.DOCKER_DROPIN, speicher.AUSTAUSCH,
                             betrieb.snap_fenster, betrieb.ARBEIT, betrieb._naechster_nachtlauf)
        schritte.HIER, schritte.ZIEL, schritte.ZUSTAND = self.tmp, self.tmp / "opt", self.tmp / "zustand"
        speicher.ETC, speicher.FSTAB = self.tmp / "etc", self.tmp / "fstab"
        speicher.DOCKER_DROPIN, speicher.AUSTAUSCH = self.tmp / "dropin.conf", self.tmp / "austausch"
        betrieb.snap_fenster = lambda von, bis: None
        betrieb._naechster_nachtlauf = lambda: None
        betrieb.ARBEIT = self.tmp / "arbeit"
        self.privat = Ed25519PrivateKey.generate()
        roh = self.privat.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        (self.tmp / "cloud-key.pub").write_text(b64(roh) + "\n")
        self.anfragen = []

    def tearDown(self):
        schritte.HIER, schritte.ZIEL, schritte.ZUSTAND, schritte._anfrage = self.alt
        (speicher.ETC, speicher.FSTAB, speicher.DOCKER_DROPIN, speicher.AUSTAUSCH,
         betrieb.snap_fenster, betrieb.ARBEIT, betrieb._naechster_nachtlauf) = self.alt_speicher

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
        # Den Schlüssel der Plattform holt erst die Installation.
        self.assertEqual(env["TAILSCALE_AUTH_KEY"], "")
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


LSBLK = {"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 512110190592, "type": "disk", "tran": "nvme",
     "model": "Samsung SSD 980", "serial": "S1", "rm": False, "hotplug": False, "mountpoints": [None],
     "children": [
         {"name": "nvme0n1p1", "path": "/dev/nvme0n1p1", "type": "part", "fstype": "vfat", "mountpoints": ["/boot/efi"]},
         {"name": "nvme0n1p3", "path": "/dev/nvme0n1p3", "type": "part", "fstype": "LVM2_member", "mountpoints": [None],
          "children": [{"name": "ubuntu--vg-ubuntu--lv", "type": "lvm", "fstype": "ext4", "mountpoints": ["/"]}]}]},
    {"name": "sda", "path": "/dev/sda", "size": 1000204886016, "type": "disk", "tran": "usb", "model": "Extreme SSD",
     "rm": False, "hotplug": True, "mountpoints": [None],
     "children": [{"name": "sda1", "path": "/dev/sda1", "type": "part", "fstype": "ext4", "label": "studio-daten",
                   "uuid": "1111-2222", "mountpoints": [None]}]},
    {"name": "sdb", "path": "/dev/sdb", "size": 8004304896, "type": "disk", "tran": "usb", "model": "Cruzer",
     "rm": True, "mountpoints": [None],
     "children": [{"name": "sdb1", "path": "/dev/sdb1", "type": "part", "fstype": "iso9660", "label": "Ubuntu",
                   "mountpoints": ["/cdrom"]}]},
    {"name": "sdc", "path": "/dev/sdc", "size": 2000398934016, "type": "disk", "tran": "sata", "model": "WD Red",
     "rm": False, "mountpoints": [None], "children": []},
    {"name": "loop0", "path": "/dev/loop0", "size": 1000, "type": "loop", "mountpoints": ["/snap/core/1"]},
    {"name": "zram0", "path": "/dev/zram0", "size": 1000, "type": "disk", "mountpoints": ["[SWAP]"]},
]}


class SpeicherTest(MitSandbox):
    def test_platten_systemplatte_und_stick_gesperrt(self):
        platten = {p["name"]: p for p in speicher.platten_auswerten(LSBLK["blockdevices"])}
        self.assertEqual(set(platten), {"nvme0n1", "sda", "sdb", "sdc"})
        self.assertTrue(platten["nvme0n1"]["system"] and platten["nvme0n1"]["gesperrt"])
        self.assertTrue(platten["sdb"]["installationsmedium"] and platten["sdb"]["gesperrt"])
        self.assertFalse(platten["sda"]["gesperrt"])
        self.assertEqual(platten["sda"]["studio_daten"], {"pfad": "/dev/sda1", "uuid": "1111-2222"})
        self.assertTrue(platten["sda"]["wechselbar"])
        self.assertFalse(platten["sdc"]["wechselbar"])

    def test_systemplatte_wird_nie_formatiert(self):
        with mock.patch.object(speicher, "_lsblk", lambda: LSBLK["blockdevices"]):
            for name in ("nvme0n1", "sdb"):
                with self.assertRaises(schritte.Fehler):
                    speicher.formatieren(name, "studio-daten", lambda z: None)
            with self.assertRaises(schritte.Fehler):
                speicher.formatieren("sda; rm -rf /", "studio-daten", lambda z: None)

    def test_platte_formatieren_nur_mit_bestaetigung(self):
        befehle = []
        with mock.patch.object(speicher, "_lsblk", lambda: LSBLK["blockdevices"]), \
                mock.patch.object(speicher, "_ausfuehren", lambda befehl, *a, **kw: befehle.append(befehl) or ""), \
                mock.patch.object(speicher, "bestand_vorhanden", lambda: False):
            with self.assertRaises(schritte.Fehler) as ctx:
                speicher.datenspeicher_waehlen({"art": "platte", "platte": "sdc", "bestaetigung": "sda"}, lambda z: None)
            self.assertIn("eintippen", str(ctx.exception))
            self.assertFalse(any("wipefs" in b for b in befehle))

    def test_fstab_eigene_zeilen_ersetzen_fremde_bleiben(self):
        speicher.FSTAB.write_text("UUID=abc / ext4 defaults 0 1\n")
        speicher.fstab_setzen("daten", speicher.fstab_daten("1111-2222"))
        speicher.fstab_setzen("daten", speicher.fstab_daten("3333-4444"))
        speicher.fstab_setzen("backup", speicher.fstab_backup({"art": "nfs", "server": "nas", "pfad": "/volume1/b"}))
        zeilen = speicher.FSTAB.read_text().splitlines()
        self.assertEqual(zeilen[0], "UUID=abc / ext4 defaults 0 1")
        self.assertEqual(len(zeilen), 3)
        self.assertIn("UUID=3333-4444 /srv/studio-daten ext4 defaults,nofail", zeilen[1])
        self.assertIn("nas:/volume1/b /srv/studio-backup nfs", zeilen[2])
        self.assertIn("x-systemd.automount", zeilen[2])
        speicher.fstab_setzen("backup", None)
        self.assertEqual(len(speicher.FSTAB.read_text().splitlines()), 2)

    def test_smb_zeile_ohne_passwort(self):
        zeile = speicher.fstab_backup({"art": "smb", "server": "192.168.178.5", "freigabe": "studio",
                                       "benutzer": "b", "passwort": "geheim"})
        self.assertTrue(zeile.startswith("//192.168.178.5/studio /srv/studio-backup cifs credentials="))
        self.assertNotIn("geheim", zeile)
        self.assertIn("_netdev", zeile)

    def test_compose_lokal_mit_und_ohne_datenordner(self):
        mit = speicher.compose_lokal_text(Path("/srv/studio-daten"))
        self.assertIn('"/srv/studio-daten/db:/var/lib/postgresql/data"', mit)
        self.assertIn('"/srv/studio-daten/api:/data"', mit)
        self.assertIn(f'"{speicher.AUSTAUSCH}:/data/host"', mit)
        bestand = speicher.compose_lokal_text(None)
        self.assertNotIn("postgresql", bestand)
        self.assertIn("/data/host", bestand)

    def test_backup_ziel_eingaben_pruefen(self):
        for falsch in ({"art": "smb", "server": "nas; reboot", "freigabe": "x", "benutzer": "u"},
                       {"art": "smb", "server": "nas", "freigabe": "", "benutzer": "u"},
                       {"art": "smb", "server": "nas", "freigabe": "x", "benutzer": ""},
                       {"art": "nfs", "server": "nas", "pfad": "kein/absoluter"},
                       {"art": "ftp"}):
            with self.assertRaises(schritte.Fehler):
                speicher._ziel_aus_eingaben(falsch)
        self.assertEqual(speicher._ziel_aus_eingaben({"art": "spaeter"}), {"art": "spaeter"})
        ziel = speicher._ziel_aus_eingaben({"art": "smb", "server": "//nas/", "freigabe": "/studio-backup/",
                                            "benutzer": "studio", "passwort": "p"})
        self.assertEqual((ziel["server"], ziel["freigabe"]), ("nas", "studio-backup"))

    def test_smb_zugang_datei(self):
        text = speicher._smb_zugang_text({"benutzer": "u", "passwort": "p w", "domaene": "WG"})
        self.assertEqual(text, "username=u\npassword=p w\ndomain=WG\n")


class BetriebTest(MitSandbox):
    def test_gfs_behaelt_tage_wochen_monate(self):
        start = datetime(2026, 10, 4, 2, 30)
        namen = [f"studio-x-{(start - timedelta(days=i)).strftime('%Y%m%d-%H%M%S')}-nacht.tar.gz.age"
                 for i in range(400)]
        behalten = betrieb.gfs_behalten(namen + ["fremd.txt"])
        self.assertIn(namen[0], behalten)
        for i in range(7):
            self.assertIn(namen[i], behalten)
        self.assertNotIn(namen[8], behalten)  # Tag 8 ist weder neueste der Woche noch des Monats
        self.assertLessEqual(len(behalten), 7 + 4 + 12)
        self.assertGreaterEqual(len(behalten), 12)
        self.assertNotIn("fremd.txt", behalten)

    def test_wartungsfenster_auch_ueber_mitternacht(self):
        lokal = lambda h, m: datetime(2026, 10, 4, h, m).astimezone()
        self.assertTrue(betrieb.im_fenster(lokal(3, 30), "03:00", "05:00"))
        self.assertFalse(betrieb.im_fenster(lokal(5, 0), "03:00", "05:00"))
        self.assertTrue(betrieb.im_fenster(lokal(23, 30), "23:00", "01:00"))
        self.assertTrue(betrieb.im_fenster(lokal(0, 30), "23:00", "01:00"))
        self.assertFalse(betrieb.im_fenster(lokal(12, 0), "23:00", "01:00"))

    def test_updates_einstellen(self):
        self.assertEqual(betrieb.updates_config()["modus"], "automatisch")
        betrieb.updates_einstellen("manuell", "02:00", "04:30")
        self.assertEqual(betrieb.updates_config(), {"modus": "manuell", "fenster_von": "02:00", "fenster_bis": "04:30"})
        for falsch in (("nie", None, None), ("automatisch", "25:00", "04:00"), ("automatisch", "03:00", "03:00")):
            with self.assertRaises(schritte.Fehler):
                betrieb.updates_einstellen(*falsch)
        status = json.loads((speicher.AUSTAUSCH / "status.json").read_text())
        self.assertEqual(status["update"]["modus"], "manuell")
        self.assertNotIn("passwort", json.dumps(status))

    def test_auftraege_unbekanntes_wird_verworfen_einstellungen_gelten(self):
        ordner = speicher.AUSTAUSCH / "anforderung"
        ordner.mkdir(parents=True)
        (ordner / "a1.json").write_text(json.dumps({"id": "a1", "aktion": "rm -rf"}))
        (ordner / "a2.json").write_text(json.dumps({"id": "a2", "aktion": "einstellungen", "modus": "manuell"}))
        (ordner / "a3.json").write_text(json.dumps({"id": "a3", "aktion": "schluessel", "oeffentlich": "kein-schluessel"}))
        self.assertEqual(betrieb.auftraege(lambda z: None), 2)
        self.assertEqual(list(ordner.iterdir()), [])
        z = betrieb.zustand()["auftraege"]
        self.assertNotIn("a1", z)
        self.assertTrue(z["a2"]["ok"])
        self.assertFalse(z["a3"]["ok"])
        self.assertEqual(betrieb.updates_config()["modus"], "manuell")

    def test_oeffentlicher_schluessel(self):
        pub = "age1" + "q" * 58
        betrieb.oeffentlichen_schluessel_setzen(pub)
        self.assertEqual(speicher.sicherung_config()["oeffentlicher_schluessel"], pub)
        status = json.loads((speicher.AUSTAUSCH / "status.json").read_text())
        self.assertTrue(status["sicherung"]["schluessel"])
        with self.assertRaises(schritte.Fehler):
            betrieb.oeffentlichen_schluessel_setzen("AGE-SECRET-KEY-1ABC")

    def test_schluesselpaar_mit_age_keygen(self):
        if not shutil.which("age-keygen"):
            self.skipTest("age nicht installiert")
        pub, privat = betrieb.schluessel_erzeugen()
        self.assertRegex(pub, betrieb.AGE_PUB)
        self.assertTrue(privat.startswith("AGE-SECRET-KEY-1"))


if __name__ == "__main__":
    unittest.main()
