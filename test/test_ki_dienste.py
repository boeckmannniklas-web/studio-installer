"""KI-Dienste (Profil ki: ki-worker, app-laeufer, pii-gate) mit eigenen Datenbankrollen, Installer 0.5.2 / Studio ab 0.53.1.

Anlass (16.11.2026, Edge Wiedenbrück): Bis 0.5.1 schaltete der Installer das Profil ki nie ein und rief rollen.sh nie auf; Wissens-
Eingänge blieben bei „Wartet auf den ersten Lauf“. Geprüft wird die feste Reihenfolge (Studio läuft → Rollen → ki an → Dienste →
Prüfung), der Rückzug bei jedem Fehler, das PII-Gate aus images.json, Update und Nachtlauf, und dass Sicherungen mit Rollen sich
auf einem neuen Server einspielen lassen (der Teil mit echtem PostgreSQL braucht Docker).
    python3 -m unittest discover -s test"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "paket/opt/studio-installer"))
from test_schritte import MitSandbox  # noqa: E402

import betrieb  # noqa: E402
import kidienste  # noqa: E402
import schritte  # noqa: E402

PII = "ghcr.io/boeckmannniklas-web/studio-pii-gate@sha256:" + "c" * 64
PII_NEU = "ghcr.io/boeckmannniklas-web/studio-pii-gate@sha256:" + "d" * 64
REGISTRY = {"server": "ghcr.io", "benutzer": "u", "token": "t"}
PRUEFUNG_OK = "OK      Rollen angelegt: 2\nOK      DB_ROLLE_PFLICHT=1\nErgebnis: wie im Vertrag.\n"


class Basis(MitSandbox):
    def setUp(self):
        super().setUp()
        schritte.ZIEL.mkdir(parents=True)
        (schritte.ZIEL / ".env").write_text("POSTGRES_PASSWORD=p\nSTUDIO_VERSION=0.53.1\nCOMPOSE_PROFILES=fernwartung\n")
        (schritte.ZIEL / "rollen.sh").write_text("#!/bin/bash\n")
        self.sperren = mock.patch.dict(os.environ, {"STUDIO_SPERREN": str(self.tmp / "sperren")})
        self.sperren.start()
        (self.tmp / "sperren").mkdir()
        self.zeilen: list[str] = []

    def tearDown(self):
        self.sperren.stop()
        super().tearDown()

    def env(self) -> dict[str, str]:
        return schritte.env_lesen((schritte.ZIEL / ".env").read_text())

    def paket(self, ref: str | None = PII):
        daten = {"db": {"ref": "ghcr.io/x/studio-db@sha256:" + "a" * 64}}
        if ref:
            daten["pii"] = {"ref": ref, "modell": "de_core_news_md-3.8.0"}
        (schritte.ZIEL / "images.json").write_text(json.dumps(daten))


class Server:
    """Nachbildung von Docker, Compose und rollen.sh. Merkt sich die Reihenfolge und COMPOSE_PROFILES bei jedem Schritt."""

    def __init__(self, test: Basis, *, rollen_rc=0, pruefung=PRUEFUNG_OK, worker_kommt=True, bilder=(), container=True):
        self.test, self.rollen_rc, self.pruefung, self.worker_kommt = test, rollen_rc, pruefung, worker_kommt
        self.bilder, self.container = set(bilder), container
        self.schritte: list[tuple[str, str]] = []

    def _profile(self) -> str:
        return self.test.env().get("COMPOSE_PROFILES", "")

    def ausfuehren(self, befehl, log=None, *, eingabe=None, env=None, cwd=None, pruefen=True):
        b = list(befehl)
        if b[:2] == ["docker", "compose"]:
            rest = b[b.index("--env-file") + 2:]
            self.schritte.append(("compose " + " ".join(rest), (env or {}).get("COMPOSE_PROFILES", self._profile())))
        elif b[:2] == ["docker", "tag"]:
            self.bilder.add(b[3])
            self.schritte.append(("tag " + b[3], self._profile()))
        else:
            self.schritte.append((" ".join(b), self._profile()))
        return ""

    def ausgabe(self, befehl):
        b = list(befehl)
        if b[:3] == ["docker", "image", "inspect"]:
            return "[]" if b[3] in self.bilder else None
        if b[:2] == ["docker", "compose"] and "ps" in b:
            return f"kennung-{b[-1]}\n" if self.container else ""
        if b[:2] == ["docker", "compose"] and "psql" in b:
            return "1\n" if self.worker_kommt else "0\n"
        raise AssertionError(f"unerwarteter Befehl {b}")

    def run(self, befehl, **kw):
        art = "pruefen" if "--pruefen" in befehl else "anlegen"
        self.schritte.append((f"rollen.sh {art}", self._profile()))
        if art == "pruefen":
            return subprocess.CompletedProcess(befehl, 0 if "FEHLER" not in self.pruefung else 1, self.pruefung, "")
        return subprocess.CompletedProcess(befehl, self.rollen_rc, "Rollen angelegt\n", "")

    def ziehen(self, registry, refs, log):
        self.schritte.append(("pull " + " ".join(refs), self._profile()))

    def __enter__(self):
        self._p = [mock.patch.object(schritte, "_ausfuehren", self.ausfuehren),
                   mock.patch.object(schritte, "_ausgabe", self.ausgabe),
                   mock.patch.object(schritte, "images_ziehen", self.ziehen),
                   mock.patch.object(schritte, "_warten", lambda f, s, pause=3: f()),
                   mock.patch.object(kidienste.subprocess, "run", self.run)]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()

    def namen(self) -> list[str]:
        return [s for s, _ in self.schritte]


# ── PII-Gate ─────────────────────────────────────────────────────────────────
class PiiGateTest(Basis):
    def test_ohne_eintrag_im_paket_bleibt_ki_aus(self):
        self.paket(None)
        with Server(self) as s:
            self.assertFalse(kidienste.pii_laden(REGISTRY, self.zeilen.append))
        self.assertEqual(s.namen(), [])
        self.assertTrue(any("0.53.1" in z for z in self.zeilen), self.zeilen)
        self.assertNotIn("STUDIO_PII_IMAGE", self.env())

    def test_laedt_mit_digest_und_gibt_einen_festen_lokalen_namen(self):
        self.paket()
        with Server(self) as s:
            self.assertTrue(kidienste.pii_laden(REGISTRY, self.zeilen.append))
        tag = kidienste.pii_tag(PII)
        self.assertEqual(s.namen(), [f"pull {PII}", f"tag studio-pii-gate:{tag}"])
        self.assertEqual((self.env()["STUDIO_PII_IMAGE"], self.env()["STUDIO_PII_VERSION"]), ("studio-pii-gate", tag))
        self.assertEqual(tag, "r-" + "c" * 16)
        self.assertNotEqual(kidienste.pii_tag(PII_NEU), tag)

    def test_schon_da_kein_neues_laden(self):
        self.paket()
        with Server(self, bilder={f"studio-pii-gate:{kidienste.pii_tag(PII)}"}) as s:
            self.assertTrue(kidienste.pii_laden(REGISTRY, self.zeilen.append))
        self.assertEqual(s.namen(), [])

    def test_laden_scheitert_nur_warnung(self):
        self.paket()
        with Server(self) as s, mock.patch.object(schritte, "images_ziehen", side_effect=schritte.Fehler("denied")):
            self.assertFalse(kidienste.pii_laden(REGISTRY, self.zeilen.append))
        self.assertNotIn("STUDIO_PII_IMAGE", self.env())
        self.assertTrue(any("WARNUNG" in z and "denied" in z for z in self.zeilen), self.zeilen)
        self.assertEqual(s.namen(), [])

    def test_nur_feste_referenzen_auf_ghcr(self):
        for ref in ("studio-pii-gate:md", "ghcr.io/x/studio-pii-gate:md", "docker.io/x/studio-pii-gate@sha256:" + "c" * 64,
                    "ghcr.io/x/studio-db@sha256:" + "c" * 64):
            self.paket(ref)
            self.assertIsNone(kidienste.pii_eintrag(), ref)


# ── Einrichten ───────────────────────────────────────────────────────────────
class EinrichtenTest(Basis):
    def bereit(self):
        self.paket()
        schritte.env_setzen({"STUDIO_PII_IMAGE": "studio-pii-gate", "STUDIO_PII_VERSION": kidienste.pii_tag(PII)})
        return {f"studio-pii-gate:{kidienste.pii_tag(PII)}"}

    def test_feste_reihenfolge_rollen_vor_profil_vor_diensten_vor_pruefung(self):
        with Server(self, bilder=self.bereit()) as s:
            self.assertEqual(kidienste.einrichten(self.zeilen.append), {"ki": "an"})
        self.assertEqual(s.schritte, [
            ("rollen.sh anlegen", "fernwartung"),
            ("compose up -d --no-build --no-deps --force-recreate ki-worker app-laeufer", "fernwartung,ki"),
            ("compose up -d --no-build --no-deps pii-gate", "fernwartung,ki"),
            ("rollen.sh pruefen", "fernwartung,ki"),
        ])
        self.assertEqual(self.env()["COMPOSE_PROFILES"], "fernwartung,ki")

    def test_rollen_sh_bekommt_die_container_der_laufenden_dienste(self):
        aufrufe = []
        with Server(self, bilder=self.bereit()) as s:
            echt = s.run
            with mock.patch.object(kidienste.subprocess, "run", lambda b, **kw: aufrufe.append(b) or echt(b, **kw)):
                kidienste.einrichten(self.zeilen.append)
        ziel = str(schritte.ZIEL)
        self.assertEqual(aufrufe[0], ["bash", f"{ziel}/rollen.sh", "--env", f"{ziel}/.env", "--db", "kennung-db", "--api", "kennung-api"])
        self.assertEqual(aufrufe[1], ["bash", f"{ziel}/rollen.sh", "--env", f"{ziel}/.env", "--db", "kennung-db", "--pruefen"])

    def test_zweimal_ki_bleibt_einmal_in_den_profilen(self):
        with Server(self, bilder=self.bereit()):
            kidienste.einrichten(self.zeilen.append)
            kidienste.einrichten(self.zeilen.append)
        self.assertEqual(self.env()["COMPOSE_PROFILES"], "fernwartung,ki")

    def assert_zurueck(self, s: Server, ergebnis: dict):
        self.assertEqual(ergebnis["ki"], "fehler")
        self.assertEqual(self.env()["COMPOSE_PROFILES"], "fernwartung", "ki muss wieder heraus")
        self.assertIn("compose --profile ki stop ki-worker app-laeufer", s.namen())
        self.assertTrue(any(z.startswith("FEHLER bei den KI-Diensten") for z in self.zeilen), self.zeilen)

    def test_rollen_scheitern_dann_startet_kein_worker(self):
        with Server(self, bilder=self.bereit(), rollen_rc=1) as s:
            ergebnis = kidienste.einrichten(self.zeilen.append)
        self.assert_zurueck(s, ergebnis)
        self.assertFalse([n for n in s.namen() if " up " in f" {n} "], "ohne Rollen kein Start")

    def test_pruefung_mit_befund_nimmt_ki_zurueck(self):
        befund = PRUEFUNG_OK.replace("OK      DB_ROLLE_PFLICHT=1", "FEHLER  studio_worker: schreibt in geschützte Tabellen (3 mit Recht)")
        with Server(self, bilder=self.bereit(), pruefung=befund) as s:
            ergebnis = kidienste.einrichten(self.zeilen.append)
        self.assert_zurueck(s, ergebnis)
        self.assertIn("geschützte Tabellen", ergebnis["grund"])

    def test_worker_meldet_sich_nicht_an(self):
        with Server(self, bilder=self.bereit(), worker_kommt=False) as s:
            ergebnis = kidienste.einrichten(self.zeilen.append)
        self.assert_zurueck(s, ergebnis)
        self.assertNotIn("rollen.sh pruefen", s.namen())

    def test_unerwarteter_fehler_wirft_nicht(self):
        with Server(self, bilder=self.bereit(), container=False) as s:
            ergebnis = kidienste.einrichten(self.zeilen.append)
        self.assert_zurueck(s, ergebnis)
        self.assertIn("db", ergebnis["grund"])

    def test_ohne_gate_kein_ki(self):
        with Server(self) as s:
            self.assertEqual(kidienste.einrichten(self.zeilen.append), {"ki": "ohne_gate"})
        self.assertEqual(s.namen(), [])
        self.assertEqual(self.env()["COMPOSE_PROFILES"], "fernwartung")

    def test_ohne_rollen_sh_kein_ki(self):
        (schritte.ZIEL / "rollen.sh").unlink()
        with Server(self, bilder=self.bereit()) as s:
            self.assertEqual(kidienste.einrichten(self.zeilen.append), {"ki": "ohne_rollen"})
        self.assertEqual(s.namen(), [])

    def test_abgeschaltet_nimmt_ki_heraus(self):
        bilder = self.bereit()
        schritte.env_setzen({"STUDIO_KI": "aus", "COMPOSE_PROFILES": "geraetenetz,ki"})
        with Server(self, bilder=bilder) as s:
            self.assertEqual(kidienste.einrichten(self.zeilen.append), {"ki": "aus"})
        self.assertEqual(self.env()["COMPOSE_PROFILES"], "geraetenetz")
        self.assertEqual(s.namen(), ["compose --profile ki stop ki-worker app-laeufer"])

    def test_ohne_ki_laesst_die_uebrigen_profile(self):
        schritte.env_setzen({"COMPOSE_PROFILES": "geraetenetz,ki,fernwartung"})
        self.assertEqual(kidienste.ohne_ki(), {"COMPOSE_PROFILES": "geraetenetz,fernwartung"})
        schritte.env_setzen({"COMPOSE_PROFILES": "ki"})
        self.assertEqual(kidienste.ohne_ki(), {"COMPOSE_PROFILES": ""}, "leer heißt: kein Profil, nicht die .env")


# ── Update ───────────────────────────────────────────────────────────────────
class UpdateTest(Basis):
    def update(self, *, ki_an: bool, einrichten=None, laeuft=True):
        if ki_an:
            schritte.env_setzen({"COMPOSE_PROFILES": "fernwartung,ki"})
        (schritte.ZIEL / "VERSION").write_text("0.53.0\n")
        antwort = {"versionen": [{"version": "0.53.1"}], "registry": REGISTRY}
        reihenfolge = []
        einrichten = einrichten or (lambda log: reihenfolge.append("einrichten") or {"ki": "an"})
        with Server(self) as s, \
             mock.patch.object(betrieb, "_ausfuehren", s.ausfuehren), \
             mock.patch.object(betrieb, "installer_aktualisieren", return_value=False), \
             mock.patch.object(betrieb, "_cloud", return_value=antwort), \
             mock.patch.object(betrieb, "_manifest_pruefen", lambda e: {"version": e["version"], "images": {}}), \
             mock.patch.object(betrieb, "_installierte_version", return_value="0.53.0"), \
             mock.patch.object(betrieb, "offene_vorgaenge", return_value=0), \
             mock.patch.object(betrieb, "sichern", return_value={}), \
             mock.patch.object(betrieb, "images_laden", lambda m, r, log: None), \
             mock.patch.object(betrieb, "edge_paket_einsetzen", lambda v, log: reihenfolge.append("paket")), \
             mock.patch.object(schritte, "db_image_einsetzen", return_value=None), \
             mock.patch.object(kidienste, "pii_laden", lambda r, log: reihenfolge.append("pii") or True), \
             mock.patch.object(kidienste, "einrichten", lambda log: einrichten(log)), \
             mock.patch.object(betrieb, "_laeuft_mit", lambda v, sek: reihenfolge.append(f"laeuft {v}") or laeuft), \
             mock.patch.object(betrieb, "rueckweg", return_value="zurück"), \
             mock.patch.object(betrieb, "_aufraeumen_vor_update", lambda behalten: None):
            (betrieb.ARBEIT / "vor-update" / "0.53.0").mkdir(parents=True)
            try:
                eintrag = betrieb.update_ausfuehren("0.53.1", self.zeilen.append)
            except betrieb.Fehler as exc:
                eintrag = {"fehler": str(exc)}
        return eintrag, s, reihenfolge

    def test_worker_stehen_waehrend_die_neue_version_migriert(self):
        eintrag, s, reihenfolge = self.update(ki_an=True)
        self.assertEqual(eintrag["ki"], "an")
        self.assertTrue(eintrag["ok"])
        start = [x for x in s.schritte if x[0].startswith("compose up -d --no-build --remove-orphans")]
        self.assertEqual(start, [("compose up -d --no-build --remove-orphans", "fernwartung")], "Start ohne Profil ki")
        namen = s.namen()
        self.assertLess(namen.index("compose --profile ki stop ki-worker app-laeufer"), namen.index(start[0][0]))
        self.assertEqual(reihenfolge, ["paket", "pii", "laeuft 0.53.1", "einrichten"])
        self.assertEqual((betrieb.zustand().get("ki") or {}).get("version"), "0.53.1")

    def test_server_ohne_ki_bekommt_es_mit_dem_update(self):
        """Wiedenbrück: bisher nie ki, mit dem Update auf 0.53.1 laufen die Dienste."""
        eintrag, s, reihenfolge = self.update(ki_an=False)
        self.assertNotIn("compose --profile ki stop ki-worker app-laeufer", s.namen())
        self.assertEqual(reihenfolge[-1], "einrichten")
        self.assertEqual(eintrag["ki"], "an")

    def test_fehler_der_ki_dienste_laesst_das_update_stehen(self):
        eintrag, _, _ = self.update(ki_an=True, einrichten=lambda log: {"ki": "fehler", "grund": "Prüfung"})
        self.assertTrue(eintrag["ok"])
        self.assertEqual(eintrag["ki"], "fehler")
        self.assertNotIn("rueckweg", eintrag)

    def test_neue_version_startet_nicht_dann_keine_ki_dienste(self):
        aufgerufen = []
        eintrag, _, _ = self.update(ki_an=True, einrichten=lambda log: aufgerufen.append(1) or {"ki": "an"}, laeuft=False)
        self.assertIn("antwortet nicht", eintrag["fehler"])
        self.assertEqual(aufgerufen, [])


# ── Nachtlauf und von Hand ───────────────────────────────────────────────────
class NachziehenTest(Basis):
    def test_wann_nachzuziehen_ist(self):
        self.assertFalse(betrieb.ki_nachzuziehen(), "Paket ohne PII-Gate (Studio 0.53.0)")
        self.paket()
        self.assertTrue(betrieb.ki_nachzuziehen(), "Paket kann ki, ki ist aus")
        schritte.env_setzen({"STUDIO_KI": "aus"})
        self.assertFalse(betrieb.ki_nachzuziehen(), "abgeschaltet")
        schritte.env_setzen({"STUDIO_KI": None, "COMPOSE_PROFILES": "ki", "STUDIO_PII_IMAGE": "studio-pii-gate",
                             "STUDIO_PII_VERSION": kidienste.pii_tag(PII)})
        (schritte.ZIEL / ".env.prozess").write_text("DB_ROLLE_PFLICHT=1\n")
        with Server(self, bilder={f"studio-pii-gate:{kidienste.pii_tag(PII)}"}):
            self.assertFalse(betrieb.ki_nachzuziehen(), "läuft schon")
        (schritte.ZIEL / ".env.prozess").unlink()
        with Server(self, bilder={f"studio-pii-gate:{kidienste.pii_tag(PII)}"}):
            self.assertTrue(betrieb.ki_nachzuziehen(), "Rollen-Dateien fehlen")
            with mock.patch.object(betrieb, "_installierte_version", return_value="0.53.1"):
                betrieb.zustand_aendern(ki={"ki": "fehler", "version": "0.53.1"})
                self.assertFalse(betrieb.ki_nachzuziehen(), "mit dieser Version schon gescheitert – nicht jede Nacht")
            with mock.patch.object(betrieb, "_installierte_version", return_value="0.53.2"):
                self.assertTrue(betrieb.ki_nachzuziehen(), "neue Version: wieder versuchen")

    def test_von_hand_laedt_das_gate_und_richtet_ein(self):
        self.paket()
        geladen = []
        with mock.patch.object(betrieb, "_installierte_version", return_value="0.53.1"), \
             mock.patch.object(betrieb, "_laeuft_mit", return_value=True), \
             mock.patch.object(betrieb, "_cloud", return_value={"registry": REGISTRY}), \
             mock.patch.object(kidienste, "pii_bereit", return_value=False), \
             mock.patch.object(kidienste, "pii_laden", lambda r, log: geladen.append(r) or True), \
             mock.patch.object(kidienste, "einrichten", return_value={"ki": "an"}):
            self.assertEqual(betrieb.ki_einrichten(self.zeilen.append), {"ki": "an"})
        self.assertEqual(geladen, [REGISTRY])
        self.assertEqual(betrieb.zustand()["ki"]["ki"], "an")
        self.assertEqual(json.loads((self.tmp / "austausch" / "status.json").read_text())["ki"]["ki"], "an")

    def test_wenn_noetig_tut_nichts_wenn_alles_laeuft(self):
        with mock.patch.object(betrieb, "ki_nachzuziehen", return_value=False), \
             mock.patch.object(kidienste, "einrichten", side_effect=AssertionError("nicht nötig")):
            self.assertEqual(betrieb.ki_einrichten(self.zeilen.append, wenn_noetig=True), {"ki": None})

    def test_studio_steht_dann_nicht(self):
        with mock.patch.object(betrieb, "_installierte_version", return_value="0.53.1"), \
             mock.patch.object(betrieb, "_laeuft_mit", return_value=False):
            with self.assertRaisesRegex(betrieb.Fehler, "antwortet nicht"):
                betrieb.ki_einrichten(self.zeilen.append)

    def test_befehl_auf_der_kommandozeile(self):
        ziel = self.tmp / "ergebnis.json"
        with mock.patch.object(betrieb, "ki_einrichten", lambda wenn_noetig=False: {"ki": "an", "wenn": wenn_noetig}):
            self.assertEqual(betrieb.main(["ki-einrichten", "--wenn-noetig", "--ergebnis", str(ziel)]), 0)
        self.assertEqual(json.loads(ziel.read_text()), {"ok": True, "ergebnis": {"ki": "an", "wenn": True}})


# ── Einrichtung des Servers ──────────────────────────────────────────────────
class InstallierenTest(unittest.TestCase):
    """Reihenfolge in schritte.installieren: Gate laden vor dem Start, Start ohne ki, einrichten, wenn Studio läuft."""

    def test_reihenfolge(self):
        quelle = inspect.getsource(schritte.installieren)
        stellen = [quelle.index(t) for t in ("kidienste.pii_laden(registry, log)", "sicherung_einspielen(wahl, log)",
                                              'env=kidienste.ohne_ki())', 'log(f"Studio {info.get(\'core_version\')} läuft.")',
                                              "ki = kidienste.einrichten(log)", '"ki": ki.get("ki")')]
        self.assertEqual(stellen, sorted(stellen))


# ── Sicherungen mit Rollen ───────────────────────────────────────────────────
class RollenVorabTest(Basis):
    def test_sql_legt_beide_rollen_nur_an_wenn_sie_fehlen(self):
        sql = kidienste.ROLLEN_VORAB_SQL
        for rolle in ("studio_worker", "studio_laeufer"):
            self.assertIn(f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{rolle}') THEN CREATE ROLE {rolle} NOLOGIN;", sql)
        self.assertNotIn("PASSWORD", sql)

    def test_einspielen_auf_neuem_server_legt_rollen_vor_pg_restore_an(self):
        quelle = inspect.getsource(schritte.sicherung_einspielen)
        self.assertLess(quelle.index("kidienste.rollen_vorab("), quelle.index('"pg_restore"'))
        quelle = inspect.getsource(betrieb.datenbank_einspielen)
        self.assertLess(quelle.index("kidienste.rollen_vorab("), quelle.index('"pg_restore"'))

    def test_probe_wiederherstellung_legt_rollen_vor_pg_restore_an(self):
        dump = self.tmp / "db.dump"
        dump.write_bytes(b"x")
        befehle = []

        def run(befehl, **kw):
            befehle.append(list(befehl))
            return subprocess.CompletedProcess(befehl, 0, "42\n" if "psql" in befehl else b"", b"" if "pg_restore" in befehl else "")

        with mock.patch.object(betrieb, "_ausfuehren", lambda b, log=None, **kw: befehle.append(list(b)) or ""), \
             mock.patch.object(betrieb.subprocess, "run", run), \
             mock.patch.object(schritte, "_warten", lambda f, s, pause=3: True), \
             mock.patch.object(betrieb.time, "sleep", lambda s: None), \
             mock.patch.object(betrieb, "_db_image", return_value="studio-db:test"):
            self.assertTrue(betrieb.probe_wiederherstellung(dump, self.zeilen.append)["ok"])
        vorab = next(i for i, b in enumerate(befehle) if kidienste.ROLLEN_VORAB_SQL in b)
        restore = next(i for i, b in enumerate(befehle) if "pg_restore" in b)
        self.assertLess(vorab, restore)


@unittest.skipUnless(shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0,
                     "Docker nicht erreichbar")
class EchteSicherungTest(unittest.TestCase):
    """Mit echtem PostgreSQL: Ohne die Rollen bricht pg_restore ab (so stand es bis 0.5.1), mit ROLLEN_VORAB_SQL nicht. Zweimal
    ausführen schadet nicht."""

    BILD = "postgres:16-alpine"

    def setUp(self):
        self.name = f"studio-ki-test-{uuid.uuid4().hex[:8]}"
        if subprocess.run(["docker", "image", "inspect", self.BILD], capture_output=True).returncode != 0:
            self.skipTest(f"{self.BILD} liegt nicht vor")
        subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, "-e", "POSTGRES_PASSWORD=x", "-e", "POSTGRES_USER=studio",
                        "-e", "POSTGRES_DB=studio", self.BILD], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", self.name], capture_output=True)
        if not schritte._warten(lambda: self.psql("studio", "SELECT 1").returncode == 0, 60, pause=1):
            self.fail("PostgreSQL startet nicht")

    def psql(self, db: str, sql: str):
        return subprocess.run(["docker", "exec", self.name, "psql", "-U", "studio", "-d", db, "-v", "ON_ERROR_STOP=1", "-Atc", sql],
                              capture_output=True, text=True)

    def test_sicherung_mit_rechten_an_die_rollen(self):
        self.psql("studio", "CREATE ROLE studio_worker; CREATE ROLE studio_laeufer; CREATE TABLE t (i int); "
                            "GRANT SELECT ON t TO studio_worker; GRANT SELECT ON t TO studio_laeufer")
        dump = subprocess.run(["docker", "exec", self.name, "pg_dump", "-U", "studio", "-d", "studio", "-Fc"], capture_output=True, check=True).stdout
        for db in ("ohne", "mit"):
            self.psql("studio", f"CREATE DATABASE {db}")
        # Wie ein neuer Server: die Rollen gibt es im Cluster nicht
        self.psql("studio", "REVOKE ALL ON t FROM studio_worker, studio_laeufer; DROP ROLE studio_worker; DROP ROLE studio_laeufer")

        def restore(db):
            return subprocess.run(["docker", "exec", "-i", self.name, "pg_restore", "-U", "studio", "-d", db, "--no-owner", "--exit-on-error"],
                                  input=dump, capture_output=True)

        ohne = restore("ohne")
        self.assertNotEqual(ohne.returncode, 0)
        self.assertIn(b'role "studio_worker" does not exist', ohne.stderr)

        for _ in range(2):
            self.assertEqual(self.psql("mit", kidienste.ROLLEN_VORAB_SQL).returncode, 0)
        mit = restore("mit")
        self.assertEqual(mit.returncode, 0, mit.stderr)
        self.assertEqual(self.psql("mit", "SELECT has_table_privilege('studio_worker', 't', 'SELECT')").stdout.strip(), "t")
        self.assertEqual(self.psql("mit", "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'studio_laeufer'").stdout.strip(), "f",
                         "Platzhalter ohne Anmeldung, bis rollen.sh sie fertig macht")


if __name__ == "__main__":
    unittest.main()
