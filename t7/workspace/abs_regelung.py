"""
abs_regelung.py — Regelung der virtuellen Pedale.

╔══════════════════════════════════════════════════════════════════════════════╗
║  IN DIESEM PROJEKT DARF AUSSCHLIESSLICH DER RUMPF DER FUNKTION               ║
║      berechne_pedalwerte(...)                                               ║
║  IN DIESER DATEI VERAENDERT WERDEN.                                          ║
║  Keine weiteren Funktionen, keine zusaetzlichen Importe, keine neuen Dateien, ║
║  keine Aenderung am Modulkopf. Ein automatischer Pruefschritt vergleicht die  ║
║  Datei per AST mit dem Original und meldet jede Abweichung ausserhalb des     ║
║  Rumpfes.                                                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝

Einbettung
----------
``lfs_link.Regelkreis`` ruft ``berechne_pedalwerte`` alle 50 ms (20 Hz) auf einem
eigenen Thread auf, waehrend das Fahrzeug in *Live for Speed* faehrt::

    Pfeiltasten ─► virtuelle Achsen ─► Fahrereingabe ─┐
                                                      ├─► berechne_pedalwerte
    LFS ──OutGauge/IS_MCI──► Fahrzeugzustand ─────────┘            │
                                                                   ▼
                                       virtuelle Laengsachse ─► Maus ─► LFS

Der Rueckgabewert wird auf 0..100 begrenzt und als virtuelle Pedalstellung an
LFS gegeben. **Der Fahrerwunsch wirkt nicht direkt** — das Fahrzeug nimmt
ausschliesslich an, was diese Funktion zurueckgibt.

Wirft die Funktion eine Ausnahme oder liefert sie NaN, faellt die Anlage fuer
diesen Takt auf "Fahrerwunsch unveraendert durchreichen" zurueck und zaehlt
einen Fehler.

Randbedingungen der Anlage
--------------------------
* **Die Bremskraft ist nur global stellbar.** Eine radindividuelle Ansteuerung
  gibt es nicht.
* **Es gibt keine einzelnen Raddrehzahlen.** Gemeldet werden genau zwei
  Geschwindigkeiten: die ueber Grund (aus IS_MCI, radunabhaengig) und die
  Radgeschwindigkeit der **Hinterachse** (aus OutGauge). Das Fahrzeug ist
  hinterradgetrieben.
* Gas und Bremse teilen sich in der Stellkette **eine** Achse (die Maus-Y-Achse
  von LFS). Solange Bremse > 0 gefordert wird, kommt kein Gas an. Beides
  gleichzeitig zu fordern ist wirkungslos, nicht schaedlich.
* Die Lenkung wird unveraendert vom Fahrer durchgereicht und ist hier nicht
  beeinflussbar.
* Der Aufrufabstand ist nominell 50 ms, aber nicht exakt: immer
  ``fahrzeug.dt_s`` verwenden, niemals einen festen Takt annehmen.

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
    """Bildet den Fahrerwunsch auf die virtuellen Pedale ab.

    Aktuelle Implementierung: unveraendertes Durchreichen — es gibt kein ABS.

    Parameter
    ---------
    fahrer : Fahrereingabe
        Der Fahrerwunsch, aus den Pfeiltasten bereits zeitlich verrampt.

        =========================  ==========  ================================
        Feld                       Bereich     Bedeutung
        =========================  ==========  ================================
        ``gas_prozent``            0 .. 100    Gaspedalstellung des Fahrers
        ``bremse_prozent``         0 .. 100    Bremspedalstellung des Fahrers
        ``lenkung_prozent``        -100..100   Lenkung, negativ = links. Wird von
                                               der Anlage gestellt, hier nur
                                               Information.
        =========================  ==========  ================================

    fahrzeug : Fahrzeugzustand
        Die **Rohwerte** beider Telemetriequellen zum Aufrufzeitpunkt, in SI
        (Meter, Sekunde, Radiant), soweit der Feldname nichts anderes sagt. Die
        Anlage rechnet nichts aus ihnen aus: kein Schlupf, keine
        Beschleunigung, keine Filterung.

        **Aus IS_MCI (InSim) — Bewegung ueber Grund, radunabhaengig:**

        ==============================  ==========================================
        Feld                            Bedeutung
        ==============================  ==========================================
        ``v_ueber_grund_mps``           Betrag der Fahrzeuggeschwindigkeit. Bleibt
                                        richtig, auch wenn die Raeder stehen.
        ``kurswinkel_rad``              wohin das Fahrzeug zeigt; 0 = Norden (+Y),
                                        gegen den Uhrzeigersinn wachsend
        ``bewegungsrichtung_rad``       wohin es sich bewegt, gleiche Kodierung.
                                        Nur aussagekraeftig bei v > 0.
        ``gierrate_rad_s``              Giergeschwindigkeit, positiv = nach links
        ``position_m``                  (x, y, z) in Weltkoordinaten
        ``mci_alter_s``                 Alter des juengsten IS_MCI-Pakets
        ==============================  ==========================================

        **Aus OutGauge — Anzeigewerte des Fahrzeugs:**

        ==============================  ==========================================
        Feld                            Bedeutung
        ==============================  ==========================================
        ``v_rad_hinterachse_mps``       Radgeschwindigkeit der **Hinterachse**.
                                        So liefert LFS die Tachogeschwindigkeit.
        ``motordrehzahl_rpm``           Motordrehzahl
        ``gang``                        0 = R, 1 = N, 2 = 1. Gang, ...
        ``ladedruck_bar``               Ladedruck
        ``motortemperatur_c``           Kuehlmitteltemperatur
        ``kraftstoff_norm``             0 .. 1
        ``oeldruck_bar`` / ``oeltemperatur_c``
        ``gas_ist_norm``                0 .. 1, was tatsaechlich am Fahrzeug ankam
        ``bremse_ist_norm``             0 .. 1, was tatsaechlich am Fahrzeug ankam
        ``kupplung_ist_norm``           0 .. 1
        ``handbremse_an``               Kontrollleuchte Handbremse: an = gezogen
        ``leuchten_verfuegbar``         Rohbitfeld: welche Kontrollleuchten das
                                        Fahrzeug hat (OutGauge.DashLights)
        ``leuchten_an``                 Rohbitfeld: welche gerade leuchten
                                        (OutGauge.ShowLights). Die Leuchten fuer
                                        ABS und Traktionskontrolle bedeuten
                                        "aktiv ODER abgeschaltet" und taugen
                                        deshalb nicht als Zustandssignal
        ``fahrzeug``                    Fahrzeugkuerzel, z. B. "XRG"
        ``outgauge_alter_s``            Alter des juengsten OutGauge-Pakets
        ==============================  ==========================================

        **Takt und Guete:**

        ==============================  ==========================================
        Feld                            Bedeutung
        ==============================  ==========================================
        ``zeit_s``                      monotone Zeit seit Start des Regelkreises
        ``dt_s``                        tatsaechlicher Abstand zum letzten Aufruf
        ``daten_gueltig``               False = eine der beiden Quellen fehlt oder
                                        ist zu alt; **dann sind alle Messwerte 0**
        ``mci_gueltig``/``outgauge_gueltig``  welche Quelle fuer sich frisch ist
        ``letzte_ausgabe_gas_prozent``       eigene Ausgabe des letzten Takts
        ``letzte_ausgabe_bremse_prozent``    eigene Ausgabe des letzten Takts
        ==============================  ==========================================

    zustand : dict
        Freier, ueber die Aufrufe hinweg erhaltener Speicher fuer diese Funktion
        (Filter, Zaehler, Zustandsautomat). Vor jedem Fahrversuch wird er
        geleert; er kann bei jedem Aufruf leer sein. Da nur diese eine Funktion
        veraendert werden darf, ist er die einzige Moeglichkeit, Historie zu
        halten — also immer mit Vorbelegung zugreifen, z. B.
        ``zustand.get("x", 0.0)``.

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
