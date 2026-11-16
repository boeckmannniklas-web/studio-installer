"""Laufanzeige im Assistenten: aktueller Schritt eines Auftrags ohne Befehle und ihre Ausgaben.
    python3 -m unittest discover -s test"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "paket/opt/studio-installer"))

import schritte  # noqa: E402
import setup  # noqa: E402


def warten(auftrag: setup.Auftrag) -> dict:
    for _ in range(200):
        stand = auftrag.stand()
        if not stand["laeuft"]:
            return stand
        time.sleep(0.01)
    raise AssertionError("Auftrag wurde nicht fertig")


class MeldungTest(unittest.TestCase):
    def test_eigene_meldungen_zaehlen(self):
        self.assertTrue(setup.ist_meldung("Lade studio-api …"))
        self.assertTrue(setup.ist_meldung("WARNUNG: Tailscale hat sich nicht angemeldet"))

    def test_befehle_ausgaben_und_leeres_zaehlen_nicht(self):
        for zeile in ("$ docker compose pull", "  Pulling api ... done", "\tweiter", "", "   ",
                      "Traceback (most recent call last):"):
            self.assertFalse(setup.ist_meldung(zeile), zeile)


class AuftragSchrittTest(unittest.TestCase):
    def test_schritt_ist_letzte_meldung_nicht_die_befehlsausgabe(self):
        a = setup.Auftrag()

        def arbeit(log):
            log("Docker-Repository einrichten …")
            schritte._ausfuehren([sys.executable, "-c", "print('Paketlisten werden gelesen')"], log)
            return {"ok": True}

        a.starten("Docker installieren", arbeit)
        stand = warten(a)
        self.assertEqual(stand["schritt"], "Docker-Repository einrichten …")
        # Alles steht weiter im Protokoll (Details), die Ausgabe eingerückt.
        self.assertIn("  Paketlisten werden gelesen", stand["zeilen"])
        self.assertTrue(any(z.startswith("$ ") for z in stand["zeilen"]))

    def test_neuer_auftrag_beginnt_ohne_schritt(self):
        a = setup.Auftrag()
        a.starten("Eins", lambda log: log("Erster Schritt"))
        self.assertEqual(warten(a)["schritt"], "Erster Schritt")
        a.starten("Zwei", lambda log: None)
        self.assertIsNone(warten(a)["schritt"])


if __name__ == "__main__":
    unittest.main()
