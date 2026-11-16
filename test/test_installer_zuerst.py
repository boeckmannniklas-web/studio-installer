"""Installer vor dem Studio-Update und Datenbank nachziehen (0.5.1).

Bis 0.5.0 holte nur der nächtliche Lauf den Installer, und erst nach dem Studio-Update. „Jetzt aktualisieren“
spielte Studio 0.53.0 deshalb mit dem alten Installer ein, und die Datenbank bekam kein pgvector.
    python3 -m unittest discover -s test"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "paket/opt/studio-installer"))
from test_schritte import MitSandbox  # noqa: E402

import betrieb  # noqa: E402
import schritte  # noqa: E402

REF = "ghcr.io/boeckmannniklas-web/studio-db@sha256:" + "b" * 64
REF_ALT = "ghcr.io/boeckmannniklas-web/studio-db@sha256:" + "a" * 64
REGISTRY = {"server": "ghcr.io", "benutzer": "u", "token": "t"}


class Basis(MitSandbox):
    def setUp(self):
        super().setUp()
        schritte.ZIEL.mkdir(parents=True)
        (schritte.ZIEL / ".env").write_text("POSTGRES_PASSWORD=p\nSTUDIO_VERSION=0.53.0\n")
        self.sperren = mock.patch.dict(os.environ, {"STUDIO_SPERREN": str(self.tmp / "sperren")})
        self.sperren.start()
        (self.tmp / "sperren").mkdir()
        os.environ.pop(betrieb.INSTALLER_FRISCH, None)
        self.zeilen: list[str] = []

    def tearDown(self):
        self.sperren.stop()
        super().tearDown()

    def paket(self, ref: str = REF):
        (schritte.ZIEL / "images.json").write_text(json.dumps({"db": {"ref": ref, "erweiterungen": ["vector"]}}))

    def env(self) -> dict[str, str]:
        return schritte.env_lesen((schritte.ZIEL / ".env").read_text())


class UpdateHoltInstallerZuerstTest(Basis):
    def test_neuer_installer_uebernimmt_das_update(self):
        aufrufe = []
        with mock.patch.object(betrieb, "installer_aktualisieren", return_value=True), \
             mock.patch.object(betrieb, "_im_neuen_installer", lambda arg, log: aufrufe.append(arg) or {"ok": True}), \
             mock.patch.object(betrieb, "_cloud", side_effect=AssertionError("alter Code darf nicht weitermachen")):
            ergebnis = betrieb.update_ausfuehren("0.53.0", self.zeilen.append, anlass="manuell")
        self.assertEqual(ergebnis, {"ok": True})
        self.assertEqual(aufrufe, [["update", "--anlass", "manuell", "--version", "0.53.0"]])

    def test_ohne_version_und_automatisch(self):
        aufrufe = []
        with mock.patch.object(betrieb, "installer_aktualisieren", return_value=True), \
             mock.patch.object(betrieb, "_im_neuen_installer", lambda arg, log: aufrufe.append(arg) or {}):
            betrieb.update_ausfuehren(None, self.zeilen.append, anlass="automatisch")
        self.assertEqual(aufrufe, [["update", "--anlass", "automatisch"]])

    def test_installer_aktuell_dann_wie_bisher(self):
        with mock.patch.object(betrieb, "installer_aktualisieren", return_value=False) as inst, \
             mock.patch.object(betrieb, "_cloud", side_effect=betrieb.Fehler("Cloud weg")) as cloud:
            with self.assertRaisesRegex(betrieb.Fehler, "Cloud weg"):
                betrieb.update_ausfuehren("0.53.0", self.zeilen.append)
        inst.assert_called_once()
        cloud.assert_called_once()

    def test_installer_nicht_erreichbar_bricht_das_update_nicht_ab(self):
        with mock.patch.object(betrieb, "installer_aktualisieren", side_effect=betrieb.Fehler("GitHub weg")), \
             mock.patch.object(betrieb, "_cloud", side_effect=betrieb.Fehler("weiter im Update")):
            with self.assertRaisesRegex(betrieb.Fehler, "weiter im Update"):
                betrieb.update_ausfuehren("0.53.0", self.zeilen.append)
        self.assertTrue(any("WARNUNG" in z and "GitHub weg" in z for z in self.zeilen), self.zeilen)

    def test_im_neuen_installer_wird_nicht_noch_einmal_gefragt(self):
        with mock.patch.dict(os.environ, {betrieb.INSTALLER_FRISCH: "1"}), \
             mock.patch.object(betrieb, "installer_aktualisieren", side_effect=AssertionError("nicht noch einmal")), \
             mock.patch.object(betrieb, "_cloud", side_effect=betrieb.Fehler("Update läuft")):
            with self.assertRaisesRegex(betrieb.Fehler, "Update läuft"):
                betrieb.update_ausfuehren("0.53.0", self.zeilen.append)


class ImNeuenInstallerTest(Basis):
    """Der Kindprozess bekommt die Datei für das Ergebnis und INSTALLER_FRISCH."""

    def betrieb_skript(self, rumpf: str):
        skript = self.tmp / "studio-betrieb"
        skript.write_text(f"#!{sys.executable}\n" + textwrap.dedent(rumpf))
        skript.chmod(0o755)
        return mock.patch.object(betrieb, "STUDIO_BETRIEB", str(skript))

    def test_ergebnis_und_ausgabe_kommen_an(self):
        with self.betrieb_skript("""
            import json, os, sys
            assert os.environ["STUDIO_INSTALLER_FRISCH"] == "1"
            print("Lade studio-api …")
            print("  Ausgabe eines Befehls")
            datei = sys.argv[sys.argv.index("--ergebnis") + 1]
            open(datei, "w").write(json.dumps({"ok": True, "ergebnis": {"nach": "0.53.0", "args": sys.argv[1:-2]}}))
        """):
            ergebnis = betrieb._im_neuen_installer(["update", "--version", "0.53.0"], self.zeilen.append)
        self.assertEqual(ergebnis, {"nach": "0.53.0", "args": ["update", "--version", "0.53.0"]})
        self.assertIn("Lade studio-api …", self.zeilen)
        self.assertIn("  Ausgabe eines Befehls", self.zeilen, "Einrückung bleibt für die Details")

    def test_fehler_des_neuen_installers_wird_weitergegeben(self):
        with self.betrieb_skript("""
            import json, sys
            datei = sys.argv[sys.argv.index("--ergebnis") + 1]
            open(datei, "w").write(json.dumps({"ok": False, "fehler": "Version 0.53.0 ist nicht freigegeben."}))
            sys.exit(1)
        """):
            with self.assertRaisesRegex(betrieb.Fehler, "nicht freigegeben"):
                betrieb._im_neuen_installer(["update"], self.zeilen.append)

    def test_absturz_ohne_ergebnis(self):
        with self.betrieb_skript("raise SystemExit(3)\n"):
            with self.assertRaisesRegex(betrieb.Fehler, "Rückgabe 3"):
                betrieb._im_neuen_installer(["update"], self.zeilen.append)

    def test_befehlszeile_schreibt_das_ergebnis(self):
        datei = self.tmp / "ergebnis.json"
        with mock.patch.object(betrieb, "update_ausfuehren", return_value={"nach": "0.53.0", "ok": True}) as upd:
            self.assertEqual(betrieb.main(["update", "--version", "0.53.0", "--anlass", "automatisch",
                                           "--ergebnis", str(datei)]), 0)
        upd.assert_called_once_with("0.53.0", anlass="automatisch")
        self.assertEqual(json.loads(datei.read_text()), {"ok": True, "ergebnis": {"nach": "0.53.0", "ok": True}})
        with mock.patch.object(betrieb, "update_ausfuehren", side_effect=betrieb.Fehler("kaputt")):
            self.assertEqual(betrieb.main(["update", "--ergebnis", str(datei)]), 1)
        self.assertEqual(json.loads(datei.read_text()), {"ok": False, "fehler": "kaputt"})


class NachtlaufTest(Basis):
    def pruefen(self, versionen: list[str], **patches):
        antwort = {"versionen": [{"version": v} for v in versionen], "registry": REGISTRY}
        cfg = {"modus": "automatisch", "fenster_von": "03:00", "fenster_bis": "05:00"}
        with mock.patch.object(betrieb, "_cloud", return_value=antwort), \
             mock.patch.object(betrieb, "_manifest_pruefen", lambda e: {"version": e["version"]}), \
             mock.patch.object(betrieb, "_installierte_version", return_value="0.52.1"), \
             mock.patch.object(betrieb, "updates_config", return_value=cfg), \
             mock.patch.object(betrieb, "im_fenster", return_value=True), \
             mock.patch.multiple(betrieb, **patches):
            return betrieb.update_pruefen(self.zeilen.append)

    def test_mit_neuer_version_nur_das_update_das_den_installer_selbst_holt(self):
        upd = mock.Mock(return_value={})
        self.pruefen(["0.53.0"], update_ausfuehren=upd,
                     installer_aktualisieren=mock.Mock(side_effect=AssertionError("nicht nach dem Update")))
        upd.assert_called_once_with("0.53.0", mock.ANY, anlass="automatisch")

    def test_ohne_neue_version_installer_und_dann_datenbank_im_neuen_installer(self):
        # Seit 0.5.2 danach auch die KI-Dienste, ebenfalls im neuen Installer und nur, wenn nötig (kidienste.py)
        neu = mock.Mock(return_value={})
        self.pruefen(["0.52.1"], installer_aktualisieren=mock.Mock(return_value=True), _im_neuen_installer=neu,
                     datenbank_nachziehen=mock.Mock(side_effect=AssertionError("alter Code")),
                     ki_einrichten=mock.Mock(side_effect=AssertionError("alter Code")))
        self.assertEqual(neu.call_args_list, [mock.call(["datenbank-nachziehen"], mock.ANY),
                                              mock.call(["ki-einrichten", "--wenn-noetig"], mock.ANY)])

    def test_installer_aktuell_aber_datenbank_fehlt(self):
        nach = mock.Mock(return_value={})
        self.pruefen(["0.52.1"], installer_aktualisieren=mock.Mock(return_value=False),
                     datenbank_nachzuziehen=mock.Mock(return_value=True), datenbank_nachziehen=nach)
        nach.assert_called_once()

    def test_ausserhalb_des_fensters_nichts(self):
        antwort = {"versionen": [{"version": "0.53.0"}], "registry": REGISTRY}
        with mock.patch.object(betrieb, "_cloud", return_value=antwort), \
             mock.patch.object(betrieb, "_manifest_pruefen", lambda e: {"version": e["version"]}), \
             mock.patch.object(betrieb, "_installierte_version", return_value="0.52.1"), \
             mock.patch.object(betrieb, "updates_config", return_value={"modus": "automatisch", "fenster_von": "03:00",
                                                                        "fenster_bis": "05:00"}), \
             mock.patch.object(betrieb, "im_fenster", return_value=False), \
             mock.patch.object(betrieb, "update_ausfuehren", side_effect=AssertionError), \
             mock.patch.object(betrieb, "installer_aktualisieren", side_effect=AssertionError):
            self.assertEqual(betrieb.update_pruefen(self.zeilen.append)["automatisch"], "0.53.0")


class DatenbankNachziehenTest(Basis):
    def test_wann_nachzuziehen_ist(self):
        self.assertFalse(betrieb.datenbank_nachzuziehen(), "ohne images.json (Studio vor 0.53.0)")
        self.paket()
        self.assertTrue(betrieb.datenbank_nachzuziehen(), "0.53.0 mit Installer vor 0.5.0 eingespielt")
        schritte.env_setzen({"STUDIO_DB_IMAGE": REF_ALT})
        self.assertTrue(betrieb.datenbank_nachzuziehen(), "älterer Eintrag des Installers")
        schritte.env_setzen({"STUDIO_DB_IMAGE": REF})
        self.assertFalse(betrieb.datenbank_nachzuziehen(), "schon umgestellt")
        schritte.env_setzen({"STUDIO_DB_IMAGE": "studio-db:16-pgvector-0.8.7"})
        self.assertFalse(betrieb.datenbank_nachzuziehen(), "eigener Eintrag von Hand bleibt")
        schritte.env_setzen({"STUDIO_DB_IMAGE": None})
        betrieb.zustand_aendern(datenbank={"abgelehnt": REF})
        self.assertFalse(betrieb.datenbank_nachzuziehen(), "abgelehnt – nicht jede Nacht neu")
        self.paket(REF_ALT)
        self.assertTrue(betrieb.datenbank_nachzuziehen(), "neues Image im nächsten Paket wird wieder versucht")

    def nachziehen(self, *, einsetzen=REF, startet=(True,), offen=0):
        befehle, starts = [], iter(startet)

        def einsetzen_(registry, log):
            if einsetzen:
                schritte.env_setzen({"STUDIO_DB_IMAGE": einsetzen})
            return einsetzen
        with mock.patch.object(betrieb, "_cloud", return_value={"registry": REGISTRY}), \
             mock.patch.object(betrieb, "_installierte_version", return_value="0.53.0"), \
             mock.patch.object(betrieb, "offene_vorgaenge", return_value=offen), \
             mock.patch.object(betrieb, "sichern", lambda anlass, log: befehle.append(("sichern", anlass))), \
             mock.patch.object(schritte, "db_image_einsetzen", einsetzen_), \
             mock.patch.object(betrieb, "_ausfuehren", lambda b, log, **k: befehle.append(tuple(b[-3:]))), \
             mock.patch.object(betrieb, "_laeuft_mit", lambda v, s: next(starts)):
            return betrieb.datenbank_nachziehen(self.zeilen.append), befehle

    def test_sichert_traegt_ein_und_startet(self):
        self.paket()
        ergebnis, befehle = self.nachziehen()
        self.assertEqual(ergebnis, {"db_image": REF})
        self.assertEqual(befehle[0], ("sichern", "update"), "erst sichern")
        self.assertEqual(befehle[1], ("up", "-d", "--no-build"))
        self.assertEqual(self.env()["STUDIO_DB_IMAGE"], REF)
        self.assertFalse(betrieb.datenbank_nachzuziehen())

    def test_startet_nicht_dann_altes_image_zurueck(self):
        self.paket()
        with self.assertRaisesRegex(betrieb.Fehler, "bisherige Image"):
            self.nachziehen(startet=(False,))
        self.assertNotIn("STUDIO_DB_IMAGE", self.env())
        self.assertFalse(betrieb.datenbank_nachzuziehen(), "nicht jede Nacht neu versuchen")

    def test_abgelehnt_wird_gemerkt(self):
        self.paket()
        ergebnis, befehle = self.nachziehen(einsetzen=None)
        self.assertEqual(ergebnis, {"db_image": None})
        self.assertNotIn(("up", "-d", "--no-build"), befehle)
        self.assertFalse(betrieb.datenbank_nachzuziehen())

    def test_nicht_waehrend_eines_verkaufs(self):
        self.paket()
        with self.assertRaisesRegex(betrieb.Fehler, "Verkauf"):
            self.nachziehen(offen=1)
        self.assertNotIn("STUDIO_DB_IMAGE", self.env())

    def test_nichts_zu_tun(self):
        ergebnis, befehle = self.nachziehen()
        self.assertEqual(ergebnis, {"db_image": None})
        self.assertEqual(befehle, [])


if __name__ == "__main__":
    unittest.main()
