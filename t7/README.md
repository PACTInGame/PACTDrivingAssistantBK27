# T7 — Antiblockiersystem

Benchmark-Aufgabe: Ein Modell implementiert in **genau einer Funktion** ein
Antiblockiersystem. Bewertet wird zweistufig — deterministisch offline im
Benchmark-Lauf, und danach im Fahrversuch in *Live for Speed*.

```
t7/
  task.yaml           Aufgabenstellung und Ausführungspolitik für den Runner
  checks.py           deterministischer Prüfer (offline, ohne LFS)
  rubric.md           Punkteschema und Fragen für den Judge
  workspace/          wird an das Modell geklont — zwei Dateien plus README
      abs_regelung.py     die einzige veränderbare Datei
      lfs_link.py         die Anlage
      README.md
      requirements.txt
  fahrversuch/        Prüfstand am LFS-Rechner, NICHT Teil des KI-Workspace
      run_fahrversuch.py  Lauf: Prüfstand + Szenario + Auswertung
      auswertung.py       Reglerschrieb -> Kennwerte -> Punkte
      umbau_aufnahme.py   Maustasten-Aufnahme -> Pfeiltasten-Aufnahme
      simulation_tests/   übernommenes Testframework (Replay + InSim-Trace)
      pyinsim/            LFS-Protokollbibliothek
      basiswerte/         die beiden Referenzläufe
```

## 1. Einbau in den Benchmark

`t7/` ist bereits ein vollständiges Task-Verzeichnis im Format von
`AI-Benchmarks/tasks/`. Einbau:

1. `t7/` nach `AI-Benchmarks/tasks/T7_abs_regelung/` kopieren.
2. In `benchmark/config.yaml` unter `tasks:` eintragen:
   ```yaml
   - "T7_abs_regelung"
   ```
3. `python benchmark/runner.py --validate-only` — prüft `task.yaml`,
   `checks.py`, `rubric.md` und `workspace/`.

`prepare_fresh_workspace` klont nur `workspace/`; `fahrversuch/` bleibt draußen
und kommt dem Modell nie unter die Augen. `checks.py` bekommt den Workspace-Pfad
als `argv[1]` und gibt ein JSON-Objekt mit `passed`, `checks` und
`deterministic_score` aus — genau wie T1 und T6.

**Die Ausführungspolitik lässt bewusst keine Shell zu** (`task.yaml`,
`execution.available_tools`). Das Modell kann seine Lösung dadurch nicht gegen
ein selbstgebautes Modell iterieren; es hat einen Versuch. Wer das lockern will,
nimmt `powershell`/`read_powershell` in die Werkzeugliste auf.

Laufzeit von `checks.py`: rund 2 s. Der Runner gibt ihm 300 s.

## 2. Der LFS-Rechner, einmalig

### 2.1 `cfg.txt` (LFS geschlossen)

```
OutGauge Mode 2
OutGauge Delay 1
OutGauge IP 127.0.0.1
OutGauge Port 30000
```

OutSim wird **nicht** gebraucht. Der Prüfstand nutzt nur OutGauge und IS_MCI.

### 2.2 `data/script/autoexec.lfs`

```
/insim 29999
```

### 2.3 Steuerung — der eine Punkt, der wirklich stimmen muss

In LFS unter **Options → Controls**, Steuerungsart **mouse / keyboard**:

* **`Throttle / brake axes :`** auf die kombinierte Achse stellen und ihr
  **`Mouse Y`** zuweisen. Ohne das liest LFS die Y-Position des Cursors nicht,
  und der Regler bremst ins Leere — gemessen am 2026-09-20: Achswerte kamen
  sauber an den Cursor, `gas_ist_norm` blieb konstant bei 0,05 und
  `bremse_ist_norm` bei 0,00, das Fahrzeug bewegte sich keinen Meter.
  In der Achsliste stehen `Mouse X` und `Mouse Y` neben den Joystick-Achsen;
  die Maustasten (`Mouse Left`/`Mouse Right`) sind die Werkseinstellung und
  müssen hier weg.
* Lenkung über **`Mouse X`** (Mausteuerung).
* **Die Pfeiltasten dürfen in LFS auf nichts gebunden sein** — sie sind die
  Fahrereingabe des Prüfstands, nicht die Fahrzeugsteuerung.
* **Alle Fahrhilfen aus** (ABS, Traktionskontrolle, Bremshilfe). Sonst misst der
  Benchmark die Fahrhilfe von LFS. Die Kontrollleuchte taugt als Nachweis
  **nicht**: `DL_ABS` bedeutet laut `OutGaugePack.txt` „ABS aktiv *oder*
  abgeschaltet" und leuchtet bei abgeschalteten Hilfen dauerhaft. Was gilt,
  sagt `IS_PFL` im InSim-Trace.
* LFS im **Fenster- oder randlosen Vollbildmodus**. Im exklusiven Vollbild nimmt
  LFS gesetzte Cursorpositionen unzuverlässig an.
* `Input when window is inactive` hilft bei unbeaufsichtigten Läufen.

### 2.4 Prüfen und einmessen

```bash
cd t7/workspace && python lfs_link.py --pruefen
```

Muss InSim, OutGauge und IS_MCI melden (Fahrzeug dafür auf die Strecke stellen).

```bash
cd t7/workspace && python lfs_link.py --kalibrieren
```

Misst über die OutGauge-Rückmeldung, welche Cursorposition welchem Achswert
entspricht, und schreibt `kalibrierung.json`. Diese Datei einmalig nach
`t7/fahrversuch/kalibrierung.json` kopieren — `run_fahrversuch.py` legt sie vor
jedem Lauf in den jeweiligen Workspace, denn ein frisch geklonter
Benchmark-Workspace bringt sie nicht mit.

```bash
cd t7/workspace && python lfs_link.py --quellen
```

Belegt, dass `OutGauge.Speed` die Radgeschwindigkeit ist und nicht die über
Grund: beschleunigen, dann Vollbremsung bis zum Blockieren. Nur dort trennen
sich die beiden. Auf dieser Annahme steht die ganze Aufgabe.

## 3. Das Szenario

`fahrversuch/simulation_tests/scenarios/90_abs_pfeiltasten` ist aus der
Referenzaufnahme `90_abs_tests` abgeleitet: fünf Vollbremsungen, davon zwei mit
Lenkeingriff, mit `SHIFT+R` dazwischen.

Die Originalaufnahme fährt mit den **Maustasten** und bewegt dabei die Maus. Das
geht mit diesem Prüfstand nicht: die Maus ist während der Fahrt die
Fahrzeugachse, und ein Abspieler, der gleichzeitig Mausereignisse einspielt,
streitet mit dem Regelkreis um den Cursor. `umbau_aufnahme.py` baut die Aufnahme
deshalb um — Maustasten werden zu Pfeiltasten, und die Cursorbahn wird über eine
Rücksimulation der Lenkrampe in ein Tastendrehbuch übersetzt:

```bash
cd t7/fahrversuch && python umbau_aufnahme.py --ueberschreiben
```

Gemessen am 2026-09-20: 20 ersetzte Klicks, 846 Lenkereignisse, Lenkfehler
2,0 % RMS / 8,6 % Maximum gegenüber der aufgezeichneten Bahn. Das ist gut genug,
um die beiden Kurvenbremsungen zu erhalten. **Eine von vornherein mit den
Pfeiltasten gefahrene Aufnahme ist trotzdem besser** — das hier ist die Brücke
zu einer Aufnahme, die es schon gab.

Der Tracer fragt in diesem Szenario **kein** OutGauge an
(`outgauge_interval_ms: 0`), weil der Prüfstand Port 30000 hält. Damit braucht
es kein UDP-Relay: IS_MCI bekommt der Tracer über seine eigene InSim-Verbindung,
der Prüfstand lässt es sich über TCP schicken (`UDPPort = 0`).

## 4. Einen Fahrversuch fahren

```bash
cd t7/fahrversuch && python run_fahrversuch.py --workspace <pfad-zum-workspace>
```

Läuft rund 160 s. Danach stehen in
`fahrversuch/simulation_tests/runs/<name>_<zeit>/`:

| Datei | Inhalt |
|---|---|
| `regler_trace.jsonl` | der 50-ms-Regeltakt: Fahrerwunsch, Rohtelemetrie, Reglerausgabe, Laufzeit |
| `trace.jsonl` | der unabhängige InSim-Trace (MCI, STA, Kontakte, Chat) |
| `run.json` | was abgespielt wurde und wie pünktlich |
| `ergebnis.json` | die Auswertung |

### Basisläufe

Die Normierung braucht zwei Referenzen, beide mit demselben Szenario:

```bash
# unveränderter Workspace, LFS-Fahrhilfen aus
python run_fahrversuch.py --workspace ../workspace --als-basiswert ohne_abs
# dasselbe Fahrzeug mit eingeschalteter LFS-Bremshilfe/ABS
python run_fahrversuch.py --workspace ../workspace --als-basiswert referenz
```

Ohne sie bewertet `auswertung.py` gegen die absoluten Zielwerte in `ZIELE` —
das ist der Notbehelf, nicht das Ziel.

Eine bereits gefahrene Aufzeichnung lässt sich jederzeit neu auswerten:

```bash
python auswertung.py simulation_tests/runs/<lauf> --speichern
```

## 5. Stand der Erprobung

Am 2026-09-20 gegen LFS 0.8C28 auf AU4X gelaufen:

| | |
|---|---|
| Abspielen des umgebauten Szenarios | funktioniert, 2529 Trace-Sätze, alle sechs Marken gesetzt |
| Pfeiltasten → Fahrereingabe | funktioniert, Gas und Bremse erreichen 100 %, Lenkung −65…+51 % |
| virtuelle Achsen → Cursor | funktioniert, `laengs` ±1,00 mit korrekter Vorfahrt der Bremse |
| Telemetrie | funktioniert, 12 482 OutGauge- und 2 494 IS_MCI-Pakete |
| Regelkreis | 3 109 Takte, 0 verpasst, 0 Reglerfehler, längste Reglerlaufzeit 0,018 ms |
| **Achse → LFS** | **offen** — siehe §2.3, `Throttle / brake axes` steht noch auf den Maustasten |

Der deterministische Prüfer ist fertig erprobt (§ `rubric.md`):
unverändertes Durchreichen 35 Punkte, ein Regler mit zu langsamem Druckaufbau
55, ein sauberer Zweipunktregler mit Druckgedächtnis 100. Regelverstöße
(Modulebene verändert, `lfs_link.py` angefasst) werden erkannt und führen
unabhängig von der Punktzahl zu `passed: false`.
