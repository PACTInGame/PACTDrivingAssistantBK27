# Fahrdynamik-Prototyp — virtuelle Pedale

Kleiner Prüfstand, der einen Regler in die Fahrsimulation *Live for Speed*
einbettet. Der Regler sitzt zwischen dem Fahrer und dem Fahrzeug: **nur seine
Ausgabe wirkt**, der Fahrerwunsch wirkt nicht direkt.

```
Pfeiltasten ─► virtuelle Achsen ─► Fahrereingabe ─┐
                                                  ├─► berechne_pedalwerte
LFS ──OutGauge/IS_MCI──► Fahrzeugzustand ─────────┘            │
                                                               ▼
                                   virtuelle Längsachse ─► Maus ─► LFS
```

## Dateien

| Datei | Rolle |
|---|---|
| `abs_regelung.py` | die Reglerfunktion `berechne_pedalwerte()` — **die einzige veränderbare Stelle** |
| `lfs_link.py` | die Anlage: Telemetrie, Datenmodell, Fahrereingabe, virtuelle Achsen, 50-ms-Regeltakt. Nicht verändern. |

## Die Anlage in Stichworten

* **Regeltakt 50 ms** (20 Hz), auf einem eigenen Thread. Der tatsächliche
  Abstand steht in `fahrzeug.dt_s` und schwankt.
* **Zwei Telemetriequellen, roh durchgereicht:**
  * IS_MCI — Geschwindigkeit **über Grund**, Position, Kurswinkel,
    Bewegungsrichtung, Gierrate. Radunabhängig.
  * OutGauge — Radgeschwindigkeit der **Hinterachse**, Gang, Drehzahl und die
    Rückmeldung, was von Gas, Bremse und Kupplung am Fahrzeug ankam.

  Die Anlage rechnet aus diesen Werten nichts aus — kein Schlupf, keine
  Beschleunigung, keine Filter.
* **Stellgrößen:** Gas und Bremse, je 0 … 100 %, **global**. Kein
  radindividueller Bremsdruck, keine Lenkung.
* **Gas und Bremse liegen auf einer Achse** (Maus-Y in LFS) und können nicht
  gleichzeitig anliegen; die Bremse hat Vorrang.
* **Hinterradantrieb.** Die Vorderachse ist antriebsfrei, ihre Drehzahl wird
  aber nicht gemeldet.
* **Fahrhilfen von LFS sind aus** (ABS, Traktionskontrolle, Bremshilfe).

## Fahrereingabe

Die Pfeiltasten sind in LFS auf nichts gebunden; der Prüfstand greift sie global
ab und verrampt sie zu analogen Achswerten.

| Taste | Wirkung |
|---|---|
| ↑ | Gas — die Maus fährt nach oben |
| ↓ | Bremse — die Maus fährt nach unten |
| ← / → | Lenkung — die Maus fährt nach links / rechts |

Eine gehaltene Taste ist also ein Pedal, das in 250 ms durchgetreten wird, kein
Schalter.

## Werkzeuge

```
python lfs_link.py --pruefen        Verbindung und Telemetrie prüfen
python lfs_link.py --kalibrieren    Maus-Achsen einmessen
python lfs_link.py --quellen        belegen, dass OutGauge.Speed die Radgeschwindigkeit ist
python lfs_link.py --fahren 120     Regelkreis frei laufen lassen
```

Diese Werkzeuge brauchen ein laufendes LFS unter Windows und stehen in dieser
Arbeitsumgebung nicht zur Verfügung.
