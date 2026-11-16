"""Datenbank-Image mit pgvector aus images.json des Edge-Pakets (Studio ab 0.53.0, Installer 0.5.0).
Ohne Docker: die Befehle werden nachgebildet.   python3 -m unittest discover -s test"""

from __future__ import annotations

import inspect
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "paket/opt/studio-installer"))
import betrieb  # noqa: E402
import schritte  # noqa: E402

REF = "ghcr.io/boeckmannniklas-web/studio-db@sha256:" + "b" * 64
REF_ALT = "ghcr.io/boeckmannniklas-web/studio-db@sha256:" + "a" * 64
REGISTRY = {"server": "ghcr.io", "benutzer": "u", "token": "t"}


class Docker:
    """Nachbildung der Befehle, die db_image_einsetzen über schritte._ausgabe stellt."""

    def __init__(self, *, laeuft: str | None = "16.14", neu: str = "16.16", erweiterung: bool = True,
                 lokal: dict[str, str] | None = None, config_image: str | None = None):
        self.laeuft, self.neu, self.erweiterung = laeuft, neu, erweiterung
        self.lokal = lokal or {}
        self.config_image = config_image
        self.befehle: list[list[str]] = []

    def __call__(self, befehl: list[str]) -> str | None:
        self.befehle.append(befehl)
        if befehl[:2] == ["docker", "compose"]:
            if "exec" in befehl and "postgres" in befehl:
                return f"postgres (PostgreSQL) {self.laeuft}\n" if self.laeuft else None
            if "config" in befehl:
                return json.dumps({"services": {"db": {"image": self.config_image}}}) if self.config_image else None
            return None
        if befehl[:3] == ["docker", "image", "inspect"]:
            return "[]" if befehl[3] in self.lokal else None
        if befehl[:2] == ["docker", "run"]:
            bild = befehl[befehl.index("--entrypoint") + 2]
            if "test" in befehl:
                return "" if bild == REF and self.erweiterung else None
            version = self.neu if bild == REF else self.lokal.get(bild)
            return f"postgres (PostgreSQL) {version}\n" if version else None
        raise AssertionError(f"unerwarteter Befehl {befehl}")


class MitZiel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="studio-installer-db-"))
        self.alt = schritte.ZIEL
        schritte.ZIEL = self.tmp / "opt"
        schritte.ZIEL.mkdir()
        (schritte.ZIEL / ".env").write_text("POSTGRES_PASSWORD=p\nSTUDIO_VERSION=0.53.0\n")
        self.zeilen: list[str] = []
        self.gezogen: list[list[str]] = []

    def tearDown(self):
        schritte.ZIEL = self.alt
        shutil.rmtree(self.tmp, ignore_errors=True)

    def paket(self, ref: str = REF, **extra):
        (schritte.ZIEL / "images.json").write_text(json.dumps({"db": {"ref": ref, "erweiterungen": ["vector"], **extra}}))

    def env(self) -> dict[str, str]:
        return schritte.env_lesen((schritte.ZIEL / ".env").read_text())

    def einsetzen(self, docker: Docker, *, ziehen_scheitert: bool = False):
        def ziehen(registry, refs, log):
            self.gezogen.append(refs)
            if ziehen_scheitert:
                raise schritte.Fehler("docker login denied")
        with mock.patch.object(schritte, "_ausgabe", docker), mock.patch.object(schritte, "images_ziehen", ziehen):
            return schritte.db_image_einsetzen(REGISTRY, self.zeilen.append)


class EinsetzenTest(MitZiel):
    def test_ohne_images_json_bleibt_alles(self):
        self.assertIsNone(self.einsetzen(Docker()))
        self.assertNotIn("STUDIO_DB_IMAGE", self.env())
        self.assertEqual(self.gezogen, [])

    def test_traegt_das_image_ein_und_nennt_die_versionen(self):
        self.paket()
        self.assertEqual(self.einsetzen(Docker(laeuft="16.14", neu="16.16")), REF)
        self.assertEqual(self.env()["STUDIO_DB_IMAGE"], REF)
        self.assertEqual(self.env()["POSTGRES_PASSWORD"], "p", "der Rest der .env bleibt")
        self.assertEqual(self.gezogen, [[REF]])
        self.assertTrue(any("PostgreSQL 16.16, vorher 16.14" in z for z in self.zeilen), self.zeilen)

    def test_gleiche_unterversion_ist_erlaubt(self):
        self.paket()
        self.assertEqual(self.einsetzen(Docker(laeuft="16.16", neu="16.16")), REF)

    def test_nie_aeltere_unterversion(self):
        """Fable M21: Ein Server, der schon mit 16.17 läuft, bekommt kein 16.16."""
        self.paket()
        self.assertIsNone(self.einsetzen(Docker(laeuft="16.17", neu="16.16")))
        self.assertNotIn("STUDIO_DB_IMAGE", self.env())
        self.assertTrue(any("WARNUNG" in z and "16.17" in z for z in self.zeilen), self.zeilen)

    def test_nie_andere_hauptversion(self):
        self.paket()
        for laeuft in ("15.10", "17.2"):
            with self.subTest(laeuft=laeuft):
                self.assertIsNone(self.einsetzen(Docker(laeuft=laeuft, neu="16.16")))
                self.assertNotIn("STUDIO_DB_IMAGE", self.env())

    def test_ohne_laufende_datenbank_zaehlt_das_bisherige_image(self):
        self.paket()
        docker = Docker(laeuft=None, neu="16.16", lokal={"postgres:16-alpine": "16.18"})
        self.assertIsNone(self.einsetzen(docker))
        self.assertNotIn("STUDIO_DB_IMAGE", self.env())

    def test_frische_installation_ohne_bisheriges_image(self):
        self.paket()
        self.assertEqual(self.einsetzen(Docker(laeuft=None, neu="16.16", lokal={})), REF)
        self.assertFalse(any("vorher" in z for z in self.zeilen), self.zeilen)

    def test_eigener_eintrag_von_hand_bleibt(self):
        """Edge in Rietberg, Cloud nach W1 oder ein Server, den jemand von Hand umgestellt hat."""
        self.paket()
        schritte.env_setzen({"STUDIO_DB_IMAGE": "studio-db:16-pgvector-0.8.7"})
        self.assertIsNone(self.einsetzen(Docker()))
        self.assertEqual(self.env()["STUDIO_DB_IMAGE"], "studio-db:16-pgvector-0.8.7")
        self.assertEqual(self.gezogen, [], "nichts geladen")

    def test_eigener_eintrag_des_installers_wird_ersetzt(self):
        self.paket()
        schritte.env_setzen({"STUDIO_DB_IMAGE": REF_ALT})
        self.assertEqual(self.einsetzen(Docker()), REF)
        self.assertEqual(self.env()["STUDIO_DB_IMAGE"], REF)

    def test_gleiches_image_laedt_nichts(self):
        self.paket()
        schritte.env_setzen({"STUDIO_DB_IMAGE": REF})
        self.assertIsNone(self.einsetzen(Docker()))
        self.assertEqual(self.gezogen, [])

    def test_nur_feste_referenzen_auf_studio_db(self):
        for ref in ("studio-db:16-pgvector-0.8.7", "ghcr.io/x/studio-db:latest", "docker.io/library/postgres@sha256:" + "a" * 64,
                    "ghcr.io/x/anderes@sha256:" + "a" * 64, REF + "; rm -rf /"):
            with self.subTest(ref=ref):
                self.paket(ref)
                self.assertIsNone(self.einsetzen(Docker()))
                self.assertNotIn("STUDIO_DB_IMAGE", self.env())
        self.assertEqual(self.gezogen, [])

    def test_ohne_erweiterung_im_image_bleibt_alles(self):
        self.paket()
        self.assertIsNone(self.einsetzen(Docker(erweiterung=False)))
        self.assertNotIn("STUDIO_DB_IMAGE", self.env())
        self.assertTrue(any("ohne vector" in z for z in self.zeilen), self.zeilen)

    def test_unsinniger_name_einer_erweiterung_gilt_als_fehlend(self):
        self.paket(erweiterungen=["../../etc/passwd"])
        self.assertIsNone(self.einsetzen(Docker()))
        self.assertNotIn("STUDIO_DB_IMAGE", self.env())

    def test_laden_scheitert_ohne_abbruch(self):
        self.paket()
        self.assertIsNone(self.einsetzen(Docker(), ziehen_scheitert=True))
        self.assertNotIn("STUDIO_DB_IMAGE", self.env())
        self.assertTrue(any("WARNUNG" in z and "denied" in z for z in self.zeilen), self.zeilen)

    def test_kaputtes_images_json_ohne_abbruch(self):
        (schritte.ZIEL / "images.json").write_text("{kein json")
        self.assertIsNone(self.einsetzen(Docker()))
        (schritte.ZIEL / "images.json").write_text("[]")
        self.assertIsNone(self.einsetzen(Docker()))


class VersionTest(unittest.TestCase):
    def test_pg_version(self):
        self.assertEqual(schritte.pg_version("postgres (PostgreSQL) 16.16\n"), (16, 16))
        self.assertEqual(schritte.pg_version("postgres (PostgreSQL) 17.2 (Debian 17.2-1)"), (17, 2))
        self.assertIsNone(schritte.pg_version(None))
        self.assertIsNone(schritte.pg_version("Error: No such container"))


class ProbeImageTest(MitZiel):
    def test_probe_nimmt_das_image_der_laufenden_datenbank(self):
        """Bis 0.4.0 nahm die Probe nur Namen mit „postgres:“ und fiel auf postgres:16-alpine zurück."""
        with mock.patch.object(schritte, "_ausgabe", Docker(config_image=REF)):
            self.assertEqual(betrieb._db_image(), REF)
        with mock.patch.object(schritte, "_ausgabe", Docker(config_image="studio-db:16-pgvector-0.8.7")):
            self.assertEqual(betrieb._db_image(), "studio-db:16-pgvector-0.8.7")

    def test_ohne_compose_aus_der_env(self):
        with mock.patch.object(schritte, "_ausgabe", lambda b: None):
            self.assertEqual(betrieb._db_image(), "postgres:16-alpine")
            schritte.env_setzen({"STUDIO_DB_IMAGE": REF})
            self.assertEqual(betrieb._db_image(), REF)


class RueckwegTest(MitZiel):
    def setUp(self):
        super().setUp()
        self.vorher = self.tmp / "vor-update"
        self.vorher.mkdir()
        shutil.copy2(schritte.ZIEL / ".env", self.vorher / ".env")
        (self.vorher / "compose.yml").write_text("services: {}\n")
        schritte.env_setzen({"STUDIO_DB_IMAGE": REF})  # das gescheiterte Update hatte umgestellt

    def rueckweg(self, starts: list[bool]):
        laeufe = iter(starts)
        with mock.patch.object(betrieb, "_ausfuehren", lambda *a, **k: ""), \
             mock.patch.object(betrieb, "_laeuft_mit", lambda v, s: next(laeufe)), \
             mock.patch.object(betrieb, "datenbank_einspielen", lambda dump, log: None):
            return betrieb.rueckweg("0.52.1", self.vorher, self.zeilen.append)

    def test_neues_image_bleibt_wenn_die_alte_version_damit_startet(self):
        text = self.rueckweg([True])
        self.assertIn("pgvector bleibt", text)
        self.assertEqual(self.env()["STUDIO_DB_IMAGE"], REF)
        self.assertEqual(self.env()["STUDIO_VERSION"], "0.52.1")

    def test_altes_image_zurueck_wenn_die_alte_version_nicht_startet(self):
        text = self.rueckweg([False, True])
        self.assertIn("alten Datenbank-Image", text)
        self.assertNotIn("STUDIO_DB_IMAGE", self.env(), "vorher gab es keinen Eintrag")

    def test_eigener_alter_wert_kommt_zurueck(self):
        schritte.env_setzen({"STUDIO_DB_IMAGE": "studio-db:eigen"})
        shutil.copy2(schritte.ZIEL / ".env", self.vorher / ".env")
        schritte.env_setzen({"STUDIO_DB_IMAGE": REF})
        self.rueckweg([False, True])
        self.assertEqual(self.env()["STUDIO_DB_IMAGE"], "studio-db:eigen")

    def test_ohne_aenderung_am_image_wie_bisher(self):
        shutil.copy2(schritte.ZIEL / ".env", self.vorher / ".env")
        text = self.rueckweg([True])
        self.assertNotIn("pgvector", text)


class ReihenfolgeTest(unittest.TestCase):
    def test_neuinstallation_vor_dem_einspielen_einer_sicherung(self):
        quelle = inspect.getsource(schritte.installieren)
        self.assertLess(quelle.index("db_image_einsetzen(registry"), quelle.index("sicherung_einspielen(wahl"))
        self.assertLess(quelle.index("db_image_einsetzen(registry"), quelle.index('_compose("up"'))

    def test_update_nach_dem_paket_und_vor_dem_start(self):
        quelle = inspect.getsource(betrieb.update_ausfuehren)
        self.assertLess(quelle.index("edge_paket_einsetzen("), quelle.index("db_image_einsetzen("))
        self.assertLess(quelle.index("db_image_einsetzen("), quelle.index('_compose("up"'))
