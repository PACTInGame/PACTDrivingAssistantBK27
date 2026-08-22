# ABS-Benchmark für Live for Speed

Prüfstand für KI-Agenten: Die zu bewertende KI implementiert ein rudimentäres
Antiblockiersystem in **genau einer Funktion**. Anschließend wird die Regelung in
der Fahrsimulation *Live for Speed* (LFS) in drei reproduzierbaren Bremsversuchen
vermessen und gegen zwei Basisläufe verglichen.

```
Tastatur (W/A/S/D)  ──►  Rampen 0..100 %  ──►  berechne_pedalwerte(...)  ──►  Maus-Achsen  ──►  LFS
                                                        ▲
LFS ──OutGauge/OutSim──►  Fahrzeugzustand (50 ms)  ─────┘
```

---

## 1. Dateien

| Datei | Rolle | Von der KI veränderbar |
|---|---|---|
| `abs_regelung.py` | **Die Aufgabe.** Enthält `berechne_pedalwerte()`. | **ja, nur diese Funktion** |
| `lfs_link.py` | Anbindung an LFS: InSim/OutGauge/OutSim, Datenmodell, Fahrereingabe, Maus-Achsen, 50-ms-Regeltakt | nein |
| `harness/orchestrator.py` | Ablauf, Messung, Bewertung, Codeprüfung | nein (nicht Teil des KI-Workspace) |
| `harness/input_recorder.py` | Aufzeichnen und Abspielen von Tastatur/Maus | nein (nicht Teil des KI-Workspace) |
| `harness/selbsttest.py` | prüft den Prüfstand selbst — ohne LFS, ohne Windows | nein (nicht Teil des KI-Workspace) |
| `AUFGABE.md` | Der Prompt, den die KI bekommt | — |

**Vor dem Klonen für einen Benchmark-Lauf:** den Ordner `harness/` aus dem
Arbeitsbereich der KI entfernen. Die KI soll die Bewertungslogik nicht sehen.
`lfs_link.py` und `abs_regelung.py` bleiben, `lfs_link.py` wird für die Typen und
das Datenmodell gebraucht.

---

## 2. Voraussetzungen

* **Windows** (Maus-Achsen und globale Eingabehaken sind Windows-spezifisch)
* **Python 3.11** (`pip install -r requirements.txt` — nur `pynput`)
* **Live for Speed** mit InSim
* LFS im **Fenster- oder randlosen Vollbildmodus**. Im exklusiven Vollbild nimmt
  LFS gesetzte Cursorpositionen unzuverlässig an.

---

## 3. LFS einrichten (einmalig)

### 3.1 `cfg.txt` (LFS-Wurzelverzeichnis, LFS muss dabei geschlossen sein)

```
OutGauge Mode 2      OutSim Mode 2
OutGauge Delay 1     OutSim Delay 1
OutGauge IP 127.0.0.1  OutSim IP 127.0.0.1
OutGauge Port 30000  OutSim Port 29998
OutGauge ID 0        OutSim ID 0
                     OutSim Opts 1ff
```

`OutSim Opts 1ff` ist zwingend: ohne den Rad-Block gibt es keine
Radwinkelgeschwindigkeiten und damit keine Vorderachsgeschwindigkeit.
Prüfen mit `python harness/orchestrator.py --nur-cfg`.

### 3.2 `data/script/autoexec.lfs`

```
/insim 29999
```

### 3.3 Steuerung

* Steuerungsart: **Maus**, X-Achse = Lenkung, Y-Achse = Gas/Bremse.
* **W, A, S, D dürfen in LFS auf nichts gebunden sein** — sie sind die
  Fahrereingabe des Prüfstands, nicht die Fahrzeugsteuerung.
* **Alle Fahrhilfen aus** (ABS, Traktionskontrolle, Bremshilfe, Automatikgetriebe
  nach Bedarf). Sonst misst der Benchmark die Fahrhilfe von LFS.
* Empfohlen ist ein **Hinterradantrieb** (z. B. XRG): die Vorderachse ist dann
  antriebsfrei, die Vorderachsgeschwindigkeit im Rollen also sauber.

---

## 4. Woher die Messwerte kommen

Der Prüfstand nutzt **OutGauge** (Pedale, Gang, Drehzahl) und **OutSim**
(Bewegung, Beschleunigungen, Raddaten). **IS_MCI** wird nur vom Diagnosewerkzeug
`--quellen` abonniert, nicht im Messbetrieb.

### 4.1 Was OutSim je Rad liefert (`OutSim Opts 1ff`, Reihenfolge HL, HR, VL, VR)

| Feld | Einheit | Inhalt |
|---|---|---|
| `SuspDeflect` | m | Federweg |
| `Steer` | rad | Radlenkwinkel (nur vorne ≠ 0 → identifiziert die Vorderachse) |
| `XForce`, `YForce` | N | Längs- und Querkraft am Reifen |
| `VerticalLoad` | N | Radlast |
| `AngVel` | rad/s | **Radwinkelgeschwindigkeit** |
| `LeanRelToRoad` | rad | Sturz relativ zur Fahrbahn |
| `AirTemp` | °C (Byte) | Reifenlufttemperatur |
| `SlipFraction` | 0…255 | Schlupfanteil |
| `Touching` | Byte | Bodenkontakt |
| `SlipRatio` | – | **Längsschlupf** |
| `TanSlipAngle` | – | tan(Schräglaufwinkel) |

Dazu kommen fahrzeugweit: Winkelgeschwindigkeit (3), Kurs/Nick/Roll,
Beschleunigung (3), Geschwindigkeit (3), Position (3), Eingaben (Gas, Bremse,
Lenkung, Kupplung, Handbremse), Gang, Motorwinkelgeschwindigkeit, Rundendistanz
und Lenkmoment.

**Bewusst nicht an die KI weitergegeben:** sämtliche radindividuellen Felder.
LFS liefert `SlipRatio` je Rad fertig aus — wer das in die Reglerfunktion gibt,
verschenkt die Aufgabe, denn das Schätzen des Schlupfes *ist* die Aufgabe. Der
Prüfstand mittelt nur `AngVel` der beiden Vorderräder zu einer
Achsgeschwindigkeit; alles andere bleibt im Harness.

### 4.2 Welches Signal ist die Vorderachsgeschwindigkeit?

Ob `OutGauge.Speed` die Geschwindigkeit über Grund oder die von den Vorderrädern
abgeleitete Anzeige ist, steht in keiner verfügbaren Dokumentation. Es ist aber
messbar:

```
python lfs_link.py --quellen
```

Beschleunigen, dann **Vollbremsung bis zum Blockieren** (Fahrhilfen aus). Nur
dort trennen sich Rad- und Grundgeschwindigkeit. Das Werkzeug passt je Kandidat
(OutSim-Grundgeschwindigkeit, MCI.Speed, Vorder- und Hinterräder) einen
Skalenfaktor an und zeigt den Restfehler während der Bremsung. Der Kandidat mit
dem kleinsten Restfehler ist die Quelle von `OutGauge.Speed`.

Ergibt sich „Räder vorne", kann die Rollradius-Schätzung entfallen und
`v_vorderachse_mps` direkt aus `OutGauge.Speed` kommen.

## 5. Vorbereitung des Prüfstands (einmalig)

1. **Verbindung prüfen** — Fahrzeug auf die Strecke stellen, Innenansicht:
   ```
   python lfs_link.py --pruefen
   ```
   Muss InSim, OutGauge und OutSim mit **4 Rädern** melden.

2. **Maus-Achsen einmessen** — misst über die LFS-Rückmeldung, welche
   Cursorposition welchem Achswert entspricht:
   ```
   python lfs_link.py --kalibrieren
   ```
   Ergebnis: `kalibrierung.json`.

3. **Quellen prüfen** (siehe §4.2) — entscheidet, ob die Rollradius-Schätzung
   überhaupt gebraucht wird:
   ```
   python lfs_link.py --quellen
   ```

4. **Rollradius einfahren** — einmal ~30 s rollen lassen (Gas und Bremse los,
   über 30 km/h). Der Radius wird automatisch geschätzt und je Fahrzeug in
   `rollradius.json` gespeichert. Ohne ihn ist `vorderachse_gueltig` False.

5. **Traces aufnehmen** (`harness/traces/`):
   ```
   python harness/input_recorder.py aufnehmen harness/traces/sitzung_start.json
   python harness/input_recorder.py aufnehmen harness/traces/szenario_1_asphalt.json
   python harness/input_recorder.py aufnehmen harness/traces/szenario_2_niedrig_mue.json
   python harness/input_recorder.py aufnehmen harness/traces/szenario_3_kurve.json
   ```
   * `sitzung_start.json`: vom Hauptmenü bis "Fahrzeug steht fahrbereit auf der Strecke".
   * Szenario-Traces: **F7 drücken, sobald die Fahrt beginnt** (Marke
     `fahrt_start`). Ab dieser Marke spielt der Orchestrator keine Mausereignisse
     mehr ab, weil die Maus dann die Fahrzeugachse ist. Die Fahrt selbst also nur
     mit W/A/S/D fahren.
   * F8 beendet die Aufnahme.

6. **Messfenster eintragen** — in `harness/orchestrator.py`, `SZENARIEN`:
   `messfenster_ms=(von_ms, bis_ms)` je Szenario, Bezug wahlweise `"start"`
   (Beginn des Abspielens) oder ein Markenname. Fenster großzügig um den
   Bremsvorgang legen; die Auswertung sucht den Bremsbeginn selbst.
   Kontrolle: `python harness/input_recorder.py zeigen <trace>`.

7. **Basisläufe aufzeichnen** (`harness/basiswerte/`):
   ```
   python harness/orchestrator.py --basiswerte ohne_abs    # Fahrhilfen aus, Funktion unverändert
   python harness/orchestrator.py --basiswerte referenz    # mit funktionierendem ABS
   ```
   `referenz` ist die Zielmarke: dasselbe Fahrzeug auf derselben Strecke mit
   aktiviertem ABS (LFS-Fahrhilfe bzw. ein Fahrzeug mit ABS). Beide Läufe
   verwenden dieselben Traces.

---

## 6. Selbsttest des Prüfstands

```
python harness/selbsttest.py
```

Läuft ohne LFS und ohne Windows in ca. 6 s und prüft Paketaufbau,
Zustandsberechnung, Rollradius-Schätzer, Rampen, den kompletten Regelkreis gegen
einen simulierten Telemetriestrom, alle Kennwerte, die Normierung, die
Punktevergabe und den AST-Vergleich der Codeprüfung. Nach jeder Änderung an
`lfs_link.py` oder am Harness ausführen — eine still kaputte Bewertung ist
schlimmer als gar keine.

## 7. Benchmark-Lauf

```
python harness/orchestrator.py --lauf
```

Ablauf: LFS starten → verbinden → Sitzung aufsetzen → drei Szenarien fahren →
Messschriebe schneiden → bewerten → `harness/ergebnisse/ergebnis_<zeit>.json`.

---

## 8. Bewertung

**Kennwerte je Szenario** (aus dem Messfenster, `berechne_metriken`):

| Kennwert | Bedeutung |
|---|---|
| `bremsweg_referenz_m` | Weg zwischen 25 m/s und 5 m/s — unabhängig von kleinen Unterschieden in der Anfangsgeschwindigkeit |
| `mittlere_verzoegerung_mps2` | aus dem Referenzbremsweg, `(v₁²−v₂²)/(2s)` |
| `blockieranteil` | Zeitanteil mit Bremsschlupf ≥ 0,40 |
| `schwimmwinkel_max_grad` | Stabilität; über 45° gilt der Lauf als Dreher |
| `kurswinkelaenderung_grad` | Lenkbarkeit — blockierte Vorderräder fahren geradeaus weiter |
| `bremsmodulationen_pro_s` | Regelaktivität der Bremsanforderung |
| `regler_laufzeit_max_ms`, `regler_fehler` | Echtzeitverhalten und Robustheit |

**Normierung:** jeder Kennwert wird zwischen den beiden Basisläufen skaliert —
`0` = so gut wie ohne ABS, `1` = so gut wie die Referenz mit ABS. Die Richtung
(kleiner oder größer ist besser) ergibt sich aus den Basiswerten selbst.
Begrenzung auf −0,5 … 1,25, ein Übertreffen der Referenz wird also belohnt.

**Gewichte:** geradeaus 60 % Bremsweg / 25 % Blockieranteil / 15 % Stabilität;
in der Kurve 35 % Bremsweg / 40 % Lenkbarkeit / 25 % Stabilität.
Szenariogewichte 0,35 / 0,35 / 0,30.

**Abzüge:** Dreher × 0,2; Ausnahmen in der Reglerfunktion −20 %;
Reglerlaufzeit über 10 ms −10 %.

**Gesamtnote:** 70 % Fahrleistung (dieses Skript) + 30 % Codebewertung.
Die Codebewertung wird außerhalb vergeben und beurteilt physikalische
Sinnhaftigkeit, Behandlung der Randfälle, Lesbarkeit und Echtzeittauglichkeit.
Zusätzlich prüft `pruefe_kandidatendatei()` per Git-Diff und AST-Vergleich, ob
**nur** `berechne_pedalwerte` verändert wurde — jeder Verstoß wird im Ergebnis
ausgewiesen.

---

## 9. Fallstricke

| Symptom | Ursache |
|---|---|
| `vorderachse_gueltig` bleibt False | `OutSim Opts 1ff` fehlt, oder der Rollradius wurde nie eingefahren |
| Keine OutGauge-Daten | Kamera nicht in der Innenansicht, Fahrzeug in der Box, oder `OutGauge Mode 0` |
| "Bremse angefordert, LFS meldet aber keine Bremse" | Maus-Achse nicht kalibriert, LFS nicht im Vordergrund, oder exklusives Vollbild |
| Fahrzeug lenkt beim Abspielen von selbst | Der Trace enthält nach `fahrt_start` noch Mausereignisse — neu aufnehmen und die Marke früher setzen |
| Streuung zwischen zwei Läufen | Startposition unterscheidet sich. Szenario-Kommandos (`/restart`) und Trace müssen jedes Mal denselben Ausgangszustand herstellen |
| Reifentemperatur beeinflusst den Bremsweg | Basisläufe und Kandidatenlauf mit vergleichbarem Reifenzustand fahren, Aufwärmrunde in den Trace aufnehmen |
