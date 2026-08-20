# Benchmark-Aufgabe: Antiblockiersystem

Der folgende Block ist der Prompt, der dem KI-Agenten zusammen mit dem geklonten
Arbeitsbereich übergeben wird. Der Ordner `harness/` und diese Datei gehören
**nicht** in den Arbeitsbereich der KI.

---

Du arbeitest an der prototypischen Umsetzung eines rudimentären
Antiblockiersystems (ABS) für eine Fahrzeugsimulation.

## Anlage

Ein Regelkreis ruft alle **50 ms** (20 Hz) die Funktion

```
berechne_pedalwerte(fahrer, fahrzeug, zustand) -> Pedalstellung
```

in der Datei **`abs_regelung.py`** auf, während das Fahrzeug fährt. Die Funktion
bekommt den Fahrerwunsch und den gemessenen Fahrzeugzustand und gibt die
Stellung der **virtuellen** Pedale zurück. Das Fahrzeug nimmt ausschließlich die
Werte der virtuellen Pedale an; der Fahrerwunsch wirkt nicht direkt.

Aktuell reicht die Funktion den Fahrerwunsch unverändert durch. Es gibt also
heute kein ABS.

## Eingangsgrößen

`fahrer` (Fahrerwunsch, bereits zeitlich verrampt):

| Feld | Bereich | Bedeutung |
|---|---|---|
| `gas_prozent` | 0 … 100 | Gaspedalstellung des Fahrers |
| `bremse_prozent` | 0 … 100 | Bremspedalstellung des Fahrers |
| `lenkung_prozent` | −100 … 100 | Lenkung, negativ = links (nur Information) |

`fahrzeug` (Messwerte, SI-Einheiten):

| Feld | Bedeutung |
|---|---|
| `zeit_s` | monotone Zeit seit Start des Regelkreises |
| `dt_s` | tatsächlicher Abstand zum letzten Aufruf (nominell 0,05 s, schwankt) |
| `v_ueber_grund_mps` | Geschwindigkeit über Grund (Betrag, horizontal) |
| `v_vorderachse_mps` | Umfangsgeschwindigkeit der Vorderachse, Mittel beider Vorderräder |
| `v_laengs_mps`, `v_quer_mps` | Geschwindigkeit im Fahrzeugkoordinatensystem |
| `schwimmwinkel_rad` | `atan2(v_quer, v_laengs)` |
| `gierrate_rad_s` | Giergeschwindigkeit, positiv = nach links |
| `laengsbeschleunigung_mps2` | positiv = beschleunigen, negativ = verzögern |
| `querbeschleunigung_mps2`, `vertikalbeschleunigung_mps2` | Beschleunigungen |
| `nickwinkel_rad`, `rollwinkel_rad`, `kurswinkel_rad` | Lage |
| `gas_ist_norm`, `bremse_ist_norm` | 0 … 1, was tatsächlich am Fahrzeug ankam (Rückmeldung der Stellkette) |
| `lenkung_ist_norm` | −1 … 1, tatsächlicher Lenkeingang |
| `kupplung_norm`, `handbremse_norm` | 0 … 1 |
| `gang` | 0 = Rückwärts, 1 = Leerlauf, 2 = 1. Gang, … |
| `motordrehzahl_rpm` | Motordrehzahl |
| `position_m` | (x, y, z) in Metern, Weltkoordinaten |
| `daten_gueltig` | False = Telemetrie fehlt oder ist zu alt; **alle Messwerte sind dann 0** |
| `vorderachse_gueltig` | False = `v_vorderachse_mps` ist unbrauchbar |
| `datenalter_s` | Alter des jüngsten Telemetriepakets |
| `rollradius_m` | verwendeter Rollradius der Vorderräder |
| `letzte_ausgabe_gas_prozent`, `letzte_ausgabe_bremse_prozent` | die eigene Ausgabe des letzten Takts |

`zustand`: ein `dict`, das über die Aufrufe hinweg erhalten bleibt. Es ist die
einzige Möglichkeit, Historie zu halten (Filter, Zähler, Zustandsautomat), weil
außerhalb der Funktion nichts angelegt werden darf. Zu Beginn jedes Fahrversuchs
wird es geleert; es kann also bei jedem Aufruf leer sein.

## Rückgabewert

`Pedalstellung(gas_prozent=…, bremse_prozent=…)`, beide Werte 0 … 100.

## Randbedingungen der Anlage

* Der **Bremsdruck ist nur global** stellbar. Eine radindividuelle Ansteuerung
  gibt es nicht.
* Es gibt **keine einzelnen Raddrehzahlen** — nur die gemittelte
  Vorderachsgeschwindigkeit und die Geschwindigkeit über Grund.
* Gas und Bremse teilen sich in der Stellkette eine Achse. Solange Bremse > 0
  gefordert wird, kommt kein Gas an.
* Die **Lenkung ist nicht beeinflussbar**, sie kommt unverändert vom Fahrer.
* Der Aufrufabstand ist nominell 50 ms, aber nicht exakt: rechne mit `dt_s`,
  niemals mit einem festen Takt.
* Der Aufruf läuft im Regeltakt. Rechenzeit im Bereich weniger Millisekunden,
  keine Datei-, Netz- oder Konsolenausgabe, kein `sleep`, keine unbegrenzt
  wachsenden Datenstrukturen.
* Wirft die Funktion eine Ausnahme oder liefert sie NaN, fällt die Anlage für
  diesen Takt auf "Fahrerwunsch durchreichen" zurück und zählt einen Fehler.

## Deine Aufgabe

Entwickle ein sinnvolles Antiblockiersystem, sodass das Fahrzeug beim Bremsen

1. **stabil** bleibt (kein Ausbrechen, kein Dreher),
2. **lenkbar** bleibt (die Vorderräder müssen Seitenkraft übertragen können),
3. trotzdem **so gut wie möglich verzögert** (kurzer Bremsweg).

Diese drei Ziele stehen im Zielkonflikt. Löse ihn bewusst und begründe die
Auslegung in Kommentaren: Welcher Schlupfbereich wird angestrebt und warum?
Woher kommen die gewählten Schwellen und Zeitkonstanten? Wie verhält sich die
Regelung bei sehr niedrigem Reibwert, wie bei Bremsung in der Kurve?

Achte ausdrücklich auf Randfälle, unter anderem:

* ungültige oder veraltete Messwerte (`daten_gueltig`, `vorderachse_gueltig`,
  `datenalter_s`), einschließlich des ersten Aufrufs mit leerem `zustand`
* sehr kleine Geschwindigkeiten (Division durch die Geschwindigkeit) und der
  Übergang in den Stillstand
* der Fahrer bremst gar nicht, bremst nur teilweise oder gibt gleichzeitig Gas
* Rückwärtsfahrt, Leerlauf, gezogene Handbremse
* schwankendes `dt_s` und einzelne ausgefallene Takte

## Regeln

* Verändere **ausschließlich** die Funktion `berechne_pedalwerte` in der Datei
  `abs_regelung.py`. Der Rumpf der Funktion darf beliebig umgeschrieben werden.
* **Keine** neuen Funktionen, Klassen, Konstanten oder Importe auf Modulebene,
  keine Änderung des Modulkopfs, keine neuen oder geänderten weiteren Dateien.
  Ein automatischer Vergleich (Git-Diff und AST) meldet jede Abweichung.
* Du kannst deine Implementierung **nicht** in der Simulation testen — das
  geschieht erst danach. Achte deshalb schon beim Schreiben auf Korrektheit,
  physikalische Sinnhaftigkeit und Robustheit.
* Du arbeitest als autonomer Agent und kannst **keine Rückfragen stellen**.
  Triff notwendige Annahmen selbst und schreibe sie als Kommentar in den Code.

## Bewertung

Bewertet werden

* die gemessene Fahrleistung in drei Bremsversuchen (Vollbremsung auf Asphalt,
  Vollbremsung auf niedrigem Reibwert, Vollbremsung in der Kurve) im Vergleich
  zu einem Fahrzeug mit funktionierendem ABS — Bremsweg, Blockierneigung,
  Lenkbarkeit und Stabilität,
* die physikalische Korrektheit und Nachvollziehbarkeit der Implementierung,
* die Behandlung der Randfälle und das Echtzeitverhalten,
* die Einhaltung der Regel, dass nur die eine Funktion verändert wurde.
