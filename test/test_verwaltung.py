"""Verwaltung nach Alt+F4 und „Für ein anderes Studio einrichten“ – ohne root, Docker und Cloud.
    python3 -m unittest discover -s test"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "paket/opt/studio-installer"))
from test_schritte import MitSandbox  # noqa: E402

import betrieb  # noqa: E402
import schritte  # noqa: E402
import speicher  # noqa: E402
import verwaltung  # noqa: E402


class VerwaltungTest(MitSandbox):
    def setUp(self):
        super().setUp()
        self.alt_pfade = (speicher.SYSTEM_DATEN, speicher.PLATTE_DATEN, speicher.BACKUP_PFAD, verwaltung.REBOOT_DATEI,
                          betrieb.sperre)
        speicher.SYSTEM_DATEN = self.tmp / "var-lib-studio"
        speicher.PLATTE_DATEN = self.tmp / "srv-studio-daten"
        speicher.BACKUP_PFAD = self.tmp / "srv-studio-backup"
        verwaltung.REBOOT_DATEI = self.tmp / "52studio-unattended-reboot"
        self.sperren = []

        from contextlib import contextmanager

        @contextmanager
        def sperre(name):
            self.sperren.append(name)
            yield
        betrieb.sperre = sperre
        verwaltung.BREMSE = verwaltung.Bremse()
        (self.tmp / "VERSION").write_text("0.3.0\n")

    def tearDown(self):
        (speicher.SYSTEM_DATEN, speicher.PLATTE_DATEN, speicher.BACKUP_PFAD, verwaltung.REBOOT_DATEI,
         betrieb.sperre) = self.alt_pfade
        super().tearDown()

    def eingerichtet(self, art="system"):
        z = schritte.ZIEL
        z.mkdir(parents=True)
        (z / ".env").write_text("STUDIO_VERSION=0.52.0\nEDGE_TENANT_ID=t-1\nEDGE_TENANT_NAME=Suntime Rietberg\n"
                                "SYNC_UPSTREAM_URL=https://ki-sunlounge.de\nSYNC_TOKEN=geheim\n")
        (z / "compose.yml").write_text("services: {}\n")
        (z / "installiert.json").write_text(json.dumps({"studio": "Suntime Rietberg", "version": "0.52.0",
                                                        "fernwartung": "plattform", "installiert_at": "2026-10-01"}))
        (z / "mosquitto").mkdir()
        (z / "mosquitto" / "passwd").write_text("x")
        speicher.ETC.mkdir(parents=True)
        konfig = {"art": art, "daten": str(speicher.SYSTEM_DATEN if art == "system" else speicher.PLATTE_DATEN)}
        (speicher.ETC / "speicher.json").write_text(json.dumps(konfig))
        (speicher.ETC / "sicherung.json").write_text(json.dumps({"oeffentlicher_schluessel": "age1x"}))
        for ordner in (speicher.SYSTEM_DATEN, speicher.PLATTE_DATEN):
            (ordner / "db").mkdir(parents=True)
            (ordner / "db" / "PG_VERSION").write_text("16")
        speicher.BACKUP_PFAD.mkdir()
        (speicher.BACKUP_PFAD / "studio-alt.tar.gz.age").write_text("alt")
        schritte._zustand_schreiben("passwort.json", {"gesetzt": "x"})
        schritte._zustand_schreiben("betrieb.json", {"sicherung": {"letzte_erfolgreich": "2026-11-15T03:00:00+00:00"}})
        speicher.AUSTAUSCH.mkdir(parents=True, exist_ok=True)
        verwaltung.REBOOT_DATEI.write_text("x")
        speicher.FSTAB.write_text("UUID=abc /  ext4 defaults 0 1\nUUID=d /srv/studio-daten ext4 x 0 2  # studio:daten\n"
                                  "//nas/b /srv/studio-backup cifs x 0 0  # studio:backup\n")

    # ── Übersicht ──
    def test_uebersicht_zeigt_bindung_ohne_geheimnisse(self):
        self.eingerichtet()
        v = verwaltung.uebersicht()
        self.assertEqual((v["studio"], v["tenant_id"], v["cloud"], v["studio_version"], v["installer"]),
                         ("Suntime Rietberg", "t-1", "ki-sunlounge.de", "0.52.0", "0.3.0"))
        self.assertTrue(v["gekoppelt"])
        self.assertEqual(v["speicher"]["art"], "system")
        self.assertEqual(v["letzte_sicherung"], "2026-11-15T03:00:00+00:00")
        self.assertNotIn("geheim", json.dumps(v))

    def test_bestaetigung_nur_mit_studionamen(self):
        self.eingerichtet()
        verwaltung.bestaetigung_pruefen("  suntime   RIETBERG ")
        with self.assertRaises(schritte.Fehler):
            verwaltung.bestaetigung_pruefen("Suntime")

    # ── SystemOwner ──
    def studio_antworten(self, rollen, login_code=200):
        def studio(methode, pfad, daten=None, token=None):
            if pfad == "/api/v1/auth/login":
                return (login_code, {"access_token": "tok"} if login_code == 200 else {})
            self.assertEqual(token, "tok")
            return 200, {"username": "chef", "display_name": "Chefin", "roles": [{"key": r} for r in rollen]}
        return mock.patch.object(verwaltung, "_studio", side_effect=studio)

    def test_nur_systemowner_darf(self):
        with self.studio_antworten(["system_owner"]):
            self.assertEqual(verwaltung.systemowner_pruefen("chef", "pw"), "Chefin")
        for rollen in (["admin"], ["mitarbeiter"], []):
            with self.studio_antworten(rollen), self.assertRaises(schritte.Fehler) as ctx:
                verwaltung.systemowner_pruefen("chef", "pw")
            self.assertIn("kein SystemOwner", str(ctx.exception))

    def test_falsches_passwort_und_bremse(self):
        with self.studio_antworten(["system_owner"], login_code=401):
            for _ in range(5):
                with self.assertRaises(schritte.Fehler) as ctx:
                    verwaltung.systemowner_pruefen("chef", "falsch")
                self.assertIn("stimmt nicht", str(ctx.exception))
            with self.assertRaises(schritte.Fehler) as ctx:
                verwaltung.systemowner_pruefen("chef", "falsch")
            self.assertIn("Zu viele Fehlversuche", str(ctx.exception))
        # Auch das richtige Passwort wartet, bis die Bremse abgelaufen ist.
        with self.studio_antworten(["system_owner"]), self.assertRaises(schritte.Fehler):
            verwaltung.systemowner_pruefen("chef", "pw")

    def test_studio_nicht_erreichbar_nennt_terminalweg(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("weg")), self.assertRaises(schritte.Fehler) as ctx:
            verwaltung.systemowner_pruefen("chef", "pw")
        self.assertIn("studio-betrieb anderes-studio", str(ctx.exception))

    # ── Zurücksetzen ──
    def zuruecksetzen(self, *, sichern_fehler=None, cloud_fehler=None, eingehaengt=(), ohne_sicherung=False):
        befehle, cloud, self.gesichert = [], [], []

        def ausfuehren(befehl, log=None, **kw):
            befehle.append(befehl)
            return ""

        def sichern(anlass, log):
            self.gesichert.append(anlass)
            if sichern_fehler:
                raise schritte.Fehler(sichern_fehler)
            return {"datei": "studio-letzte.tar.gz.age", "ok": True}

        def cloud_aufruf(methode, pfad, daten=None):
            cloud.append((methode, pfad))
            if cloud_fehler:
                raise schritte.Fehler(cloud_fehler)
            return {}
        log = []
        with mock.patch.object(schritte, "_ausfuehren", side_effect=ausfuehren), \
                mock.patch.object(betrieb, "sichern", side_effect=sichern), \
                mock.patch.object(betrieb, "_cloud", side_effect=cloud_aufruf), \
                mock.patch.object(verwaltung.os.path, "ismount", side_effect=lambda p: str(p) in map(str, eingehaengt)):
            ergebnis = verwaltung.anderes_studio(log.append, ohne_sicherung=ohne_sicherung, wer="Chefin")
        return ergebnis, befehle, cloud, log

    def test_systemplatte_alles_weg_backup_ziel_bleibt(self):
        self.eingerichtet("system")
        e, befehle, cloud, log = self.zuruecksetzen(eingehaengt=[speicher.BACKUP_PFAD])
        self.assertEqual(e, {"studio_alt": "Suntime Rietberg", "sicherung": "studio-letzte.tar.gz.age",
                             "cloud_geloest": True})
        self.assertEqual(cloud, [("POST", "/api/v1/edge/freigeben")])
        self.assertIn(["systemctl", "disable", "--now", *verwaltung.TIMER], befehle)
        self.assertTrue(any(b[:2] == ["docker", "compose"] and b[-3:] == ["down", "-v", "--remove-orphans"]
                            for b in befehle))
        self.assertIn(["umount", str(speicher.BACKUP_PFAD)], befehle)
        self.assertFalse(speicher.SYSTEM_DATEN.exists())
        self.assertEqual(list(schritte.ZIEL.iterdir()), [])
        self.assertFalse(schritte.installiert())
        self.assertEqual(list(schritte.ZUSTAND.iterdir()), [])
        for weg in (speicher.ETC, speicher.AUSTAUSCH, verwaltung.REBOOT_DATEI):
            self.assertFalse(weg.exists(), weg)
        # Sicherungen am Ziel bleiben, fremde fstab-Zeilen auch.
        self.assertTrue((speicher.BACKUP_PFAD / "studio-alt.tar.gz.age").exists())
        self.assertEqual(speicher.FSTAB.read_text(), "UUID=abc /  ext4 defaults 0 1\n")
        self.assertEqual(set(self.sperren), {"update", "installer"})

    def test_eigene_platte_wird_geleert_nicht_formatiert(self):
        self.eingerichtet("platte")
        e, befehle, _, _ = self.zuruecksetzen(eingehaengt=[speicher.PLATTE_DATEN])
        self.assertTrue(speicher.PLATTE_DATEN.is_dir())
        self.assertEqual(list(speicher.PLATTE_DATEN.iterdir()), [])
        self.assertIn(["umount", str(speicher.PLATTE_DATEN)], befehle)
        self.assertFalse(any("mkfs" in " ".join(b) for b in befehle))
        self.assertNotIn("platte_nicht_geleert", e)

    def test_platte_nicht_eingehaengt_wird_gemeldet(self):
        self.eingerichtet("platte")
        e, _, _, log = self.zuruecksetzen()
        self.assertTrue(e["platte_nicht_geleert"])
        self.assertTrue(any("neu formatieren" in z for z in log))

    def test_sicherung_gescheitert_nichts_geloescht(self):
        self.eingerichtet("system")
        with self.assertRaises(schritte.Fehler) as ctx:
            self.zuruecksetzen(sichern_fehler="Cloud nicht erreichbar")
        self.assertIn("Es wurde nichts gelöscht", str(ctx.exception))
        self.assertTrue(schritte.installiert())
        self.assertTrue((speicher.SYSTEM_DATEN / "db" / "PG_VERSION").exists())
        self.assertTrue((speicher.ETC / "speicher.json").exists())

    def test_ohne_sicherung_ausdruecklich(self):
        self.eingerichtet("system")
        e, _, _, log = self.zuruecksetzen(ohne_sicherung=True)
        self.assertEqual(self.gesichert, [])
        self.assertIsNone(e["sicherung"])
        self.assertTrue(any("ohne letzte Sicherung" in z for z in log))

    def test_cloud_nicht_erreichbar_loescht_trotzdem_mit_hinweis(self):
        self.eingerichtet("system")
        e, _, _, log = self.zuruecksetzen(cloud_fehler="Fehler 404")
        self.assertFalse(e["cloud_geloest"])
        self.assertTrue(any("Gerät lösen" in z for z in log))
        self.assertFalse(schritte.installiert())

    def test_leeren_verweigert_systemordner(self):
        with self.assertRaises(schritte.Fehler):
            verwaltung._leeren(Path("/opt"), print)


if __name__ == "__main__":
    unittest.main()
