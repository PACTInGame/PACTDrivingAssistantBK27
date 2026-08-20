"""
abs_regelung.py — Fahrerassistenz-Regelung fuer die virtuellen Pedale.

╔══════════════════════════════════════════════════════════════════════════════╗
║  IN DIESEM PROJEKT DARF AUSSCHLIESSLICH DIE FUNKTION                         ║
║      berechne_pedalwerte(...)                                                ║
║  IN DIESER DATEI VERAENDERT WERDEN.                                          ║
║  Keine weiteren Funktionen, keine zusaetzlichen Importe, keine neuen Dateien, ║
║  keine Aenderung am Modulkopf. Ein automatischer Pruefschritt vergleicht die  ║
║  Datei mit dem Original und meldet jede Abweichung ausserhalb des Rumpfes.    ║
╚══════════════════════════════════════════════════════════════════════════════╝

Einbettung
----------
``lfs_link.Regelkreis`` ruft ``berechne_pedalwerte`` alle 50 ms auf (20 Hz), auf
einem eigenen Thread, waehrend das Fahrzeug in Live for Speed faehrt:

    Tasteneingabe  ->  Rampen  ->  Fahrereingabe ─┐
                                                  ├─► berechne_pedalwerte ─► Maus-Achsen ─► LFS
    OutGauge/OutSim ─► Fahrzeugzustand ───────────┘

Der Rueckgabewert wird auf 0..100 begrenzt und dann als virtuelle Pedalstellung
an LFS gegeben. Wirft die Funktion eine Ausnahme oder liefert sie NaN, faellt das
Harness fuer diesen Takt auf "Fahrereingabe unveraendert durchreichen" zurueck und
zaehlt einen Fehler.

Randbedingungen der Simulation
------------------------------
* **Der Bremsdruck ist nur global steuerbar** — es gibt keine radindividuelle
  Ansteuerung.
* **Es gibt keine einzelnen Raddrehzahlen** — nur die gemittelte
  Vorderachsgeschwindigkeit ``v_vorderachse_mps`` und die Geschwindigkeit ueber
  Grund ``v_ueber_grund_mps``.
* Gas und Bremse teilen sich beim Stellen eine Achse. Solange Bremse > 0
  gefordert wird, kommt kein Gas an. Beides gleichzeitig zu fordern ist daher
  wirkungslos, nicht schaedlich.
* Die Lenkung wird vom Harness unveraendert vom Fahrer durchgereicht und ist
  hier nicht beeinflussbar.
* Aufrufabstand ist ca. 50 ms, aber nicht exakt: immer ``fahrzeug.dt_s``
  verwenden, niemals einen festen Takt annehmen.

Rechenzeit
----------
Der Aufruf laeuft im Regeltakt. Alles, was laenger als wenige Millisekunden
dauert, verletzt den Takt. Keine Datei-, Netz- oder Konsolenausgabe, kein
``sleep``, keine unbegrenzt wachsenden Listen.
"""

from __future__ import annotations

from lfs_link import Fahrereingabe, Fahrzeugzustand, Pedalstellung


def berechne_pedalwerte(fahrer: Fahrereingabe,
                        fahrzeug: Fahrzeugzustand,
                        zustand: dict) -> Pedalstellung:
    """Bildet die Fahrereingabe auf die virtuellen Pedale ab.

    Aktuelle Implementierung: unveraendertes Durchreichen (kein ABS).

    Parameter
    ---------
    fahrer : Fahrereingabe
        Der Fahrerwunsch, bereits zeitlich verrampt.

        =========================  =========  =================================
        Feld                       Bereich    Bedeutung
        =========================  =========  =================================
        ``gas_prozent``            0 .. 100   Gaspedalstellung des Fahrers
        ``bremse_prozent``         0 .. 100   Bremspedalstellung des Fahrers
        ``lenkung_prozent``        -100..100  Lenkung (negativ = links); wird vom
                                              Harness gestellt, nur Information
        =========================  =========  =================================

    fahrzeug : Fahrzeugzustand
        Messwerte des Fahrzeugs zum Aufrufzeitpunkt. Alle Groessen in SI
        (Meter, Sekunde, Radiant), sofern der Feldname nichts anderes sagt.

        ==================================  ===========================================
        Feld                                Bedeutung
        ==================================  ===========================================
        ``zeit_s``                          monotone Zeit seit Start des Regelkreises
        ``dt_s``                            tatsaechlicher Abstand zum letzten Aufruf
        ``v_ueber_grund_mps``               Betrag der horizontalen Fahrzeug-
                                            geschwindigkeit (Referenzgeschwindigkeit)
        ``v_vorderachse_mps``               Umfangsgeschwindigkeit der Vorderachse,
                                            Mittel beider Vorderraeder
        ``v_laengs_mps`` / ``v_quer_mps``   Geschwindigkeit im Fahrzeugkoordinaten-
                                            system (x vorwaerts, y quer)
        ``schwimmwinkel_rad``               atan2(v_quer, v_laengs)
        ``gierrate_rad_s``                  Giergeschwindigkeit, positiv = nach links
        ``laengsbeschleunigung_mps2``       positiv = beschleunigen, negativ = bremsen
        ``querbeschleunigung_mps2``         Querbeschleunigung
        ``vertikalbeschleunigung_mps2``     Vertikalbeschleunigung
        ``nickwinkel_rad`` / ``rollwinkel_rad`` / ``kurswinkel_rad``
        ``gas_ist_norm`` / ``bremse_ist_norm``   0..1, was bei LFS tatsaechlich
                                            ankam (Rueckmeldung der Stellkette)
        ``lenkung_ist_norm``                -1..1, tatsaechlicher Lenkeingang
        ``kupplung_norm`` / ``handbremse_norm``  0..1
        ``gang``                            0 = R, 1 = N, 2 = 1. Gang, ...
        ``motordrehzahl_rpm``               Motordrehzahl
        ``position_m``                      (x, y, z) in Metern, Weltkoordinaten
        ``daten_gueltig``                   False = Telemetrie fehlt oder ist zu alt;
                                            dann sind alle Messwerte 0
        ``vorderachse_gueltig``             False = Raddaten fehlen oder der
                                            Rollradius ist noch nicht eingemessen;
                                            ``v_vorderachse_mps`` ist dann unbrauchbar
        ``datenalter_s``                    Alter des juengsten Telemetriepakets
        ``rollradius_m``                    verwendeter Rollradius der Vorderraeder
        ``letzte_ausgabe_gas_prozent``      eigene Ausgabe des letzten Takts
        ``letzte_ausgabe_bremse_prozent``   eigene Ausgabe des letzten Takts
        ==================================  ===========================================

    zustand : dict
        Freier, ueber Aufrufe hinweg erhaltener Speicher fuer diese Funktion
        (Filter, Zaehler, Zustandsautomat). Beim Start jedes Szenarios wird das
        Dictionary geleert. Da nur diese eine Funktion veraendert werden darf,
        ist es die einzige Moeglichkeit, Historie zu halten — bei jedem Zugriff
        also mit Vorbelegung arbeiten, z. B. ``zustand.get("x", 0.0)``.

    Rueckgabe
    ---------
    Pedalstellung
        ``gas_prozent`` und ``bremse_prozent``, je 0 .. 100.
    """
    # ══════════════════════════════════════════════════════════════════════
    # ANFANG DES BEARBEITBAREN BEREICHS
    # ══════════════════════════════════════════════════════════════════════

    return Pedalstellung(gas_prozent=fahrer.gas_prozent,
                         bremse_prozent=fahrer.bremse_prozent)

    # ══════════════════════════════════════════════════════════════════════
    # ENDE DES BEARBEITBAREN BEREICHS
    # ══════════════════════════════════════════════════════════════════════
