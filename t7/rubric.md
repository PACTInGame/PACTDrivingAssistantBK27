# Rubrik T7 – Antiblockiersystem

Die Aufgabe wird in **zwei Stufen** bewertet. Stufe 1 läuft im Benchmark selbst
und ist vollständig deterministisch. Stufe 2 ist der Fahrversuch in *Live for
Speed*; er braucht Windows, ein laufendes Spiel und rund drei Minuten Fahrzeit
und wird deshalb getrennt gefahren und nachgetragen.

---

## Stufe 1 — `checks.py`, deterministisch, 100 Punkte

Die Kandidatenfunktion läuft dafür in einem geschlossenen Regelkreis gegen ein
Längsdynamikmodell mit schlupfabhängigem Reibbeiwert (Burckhardt), das im Prüfer
steckt und dem Kandidaten nicht zugänglich ist. Drei Fahrbahnen: trockener
Asphalt, nass, sehr niedriger Reibwert. Verglichen wird jeweils mit zwei
Läufen desselben Modells:

* **ohne Regelung** — der unveränderte Durchreicher, Räder blockieren;
* **Referenzregler** — ein Zweipunktregler auf den Schlupf (auf bei 0,20, ab bei
  0,12), der dieselben Messgrößen hat wie der Kandidat und den Reibwert nicht
  kennt.

### Handwerk (40 Punkte)

| Prüfung | Punkte | Bedeutung |
|---|---:|---|
| `nur_rumpf_geaendert` | 8 | AST-Vergleich gegen das Original: außerhalb des Funktionsrumpfes ist nichts verändert |
| `anlage_unveraendert` | 4 | `lfs_link.py` und die übrigen Dateien sind Byte für Byte unverändert |
| `keine_verbotenen_konstrukte` | 3 | kein Import, kein `global`, kein `open`/`eval`/`print`/`sleep` im Rumpf |
| `implementierung_vorhanden` | 5 | der Rumpf enthält überhaupt eine Regelung und nicht nur das Durchreichen |
| `randfaelle` | 8 | 19 Randfälle ohne Ausnahme, ohne NaN, Ausgabe stets 0…100 |
| `kein_eingriff_ohne_bremswunsch` | 4 | ohne Bremswunsch des Fahrers wird nicht gebremst |
| `zustand_begrenzt` | 3 | `zustand` wächst über 4000 Takte nicht unbegrenzt |
| `echtzeit` | 3 | längster Aufruf unter 2 ms |
| `ausnahmefrei_im_kreis` | 2 | keine Ausnahme in den drei Bremsversuchen |

### Regelgüte (60 Punkte)

| Prüfung | Punkte | Bedeutung |
|---|---:|---|
| `regelt` | 5 | die Bremsanforderung wird auf mindestens zwei Fahrbahnen moduliert |
| `kein_dauerblockieren` | 5 | Zeit mit Schlupf ≥ 0,40 höchstens `max(1 s, 1,6 × Referenz)` |
| `schlupf_nicht_dauerhoch` | 5 | Median des Schlupfes unter 0,60 — das Rad läuft wieder hoch |
| `besser_als_ohne_regelung` | 15 | auf **jeder** Fahrbahn mindestens 5 % des erreichbaren Gewinns |
| `bremsweg_asphalt` | 10 | ≥ 60 % des Gewinns der Referenz |
| `bremsweg_nass` | 10 | ≥ 60 % des Gewinns der Referenz |
| `bremsweg_glatt` | 10 | ≥ 60 % des Gewinns der Referenz |

Der **Gewinnanteil** ist `(Weg_ohne − Weg_Kandidat) / (Weg_ohne − Weg_Referenz)`:
`0` heißt so gut wie gar keine Regelung, `1` so gut wie der Referenzregler.
Absolute Meterzahlen wären an dieses eine Modell gebunden, der Anteil ist es
nicht.

### Bestehen

`passed` ab **60 Punkten** *und* nur, wenn `nur_rumpf_geaendert` und
`anlage_unveraendert` erfüllt sind. Eine Lösung, die die ausdrücklich genannte
Beschränkung auf eine Funktion umgeht, hat eine andere Aufgabe gelöst; die
Punktzahl bleibt trotzdem stehen und sagt weiterhin, wie gut die Regelung war.

Gemessene Eichpunkte (Stand der Auslieferung):

| Lösung | Punkte | `passed` |
|---|---:|---|
| unverändertes Durchreichen | 35 | nein |
| Zweipunktregler mit zu langsamem Druckaufbau (auf Asphalt schlechter als gar keine Regelung) | 55 | nein |
| Zweipunktregler mit Druckgedächtnis und schnellem Wiederaufbau | 100 | ja |

---

## Stufe 2 — Fahrversuch, getrennt nachgetragen

`fahrversuch/run_fahrversuch.py` fährt das Szenario `90_abs_pfeiltasten` in LFS:
fünf Vollbremsungen, davon zwei mit Lenkeingriff. `fahrversuch/auswertung.py`
schneidet den 50-ms-Reglerschrieb an den Bremsanforderungen des Fahrers auf und
liefert je Bremsung Bremsweg, mittlere Verzögerung, Blockieranteil,
Schwimmwinkel, Kurswinkeländerung, Modulationsrate und Reglerlaufzeit.

Normiert wird zwischen zwei aufgezeichneten Basisläufen (`ohne_abs`,
`referenz`); Gewichte 60/25/15 geradeaus (Bremsweg / Blockieranteil /
Stabilität) und 35/40/25 mit Lenkeingriff (Bremsweg / Lenkbarkeit /
Stabilität). Abzüge: Dreher × 0,2, Reglerausnahmen × 0,8, Reglerlaufzeit über
10 ms × 0,9.

---

## Qualitative Beurteilung (LLM-as-a-Judge)

Bewertet wird **nur der Rumpf von `berechne_pedalwerte`**, anhand dieser Fragen:

1. **Physikalische Sinnhaftigkeit.** Wird der Bremsschlupf richtig gebildet —
   bezogen auf die Geschwindigkeit über Grund, nicht auf die Radgeschwindigkeit?
   Ist der angestrebte Schlupfbereich begründet und liegt er dort, wo ein Reifen
   sein Kraftmaximum hat (etwa 0,08 … 0,20)? Ist erkannt, dass die gemeldete
   Radgeschwindigkeit die der angetriebenen Hinterachse ist?
2. **Nachvollziehbarkeit.** Stehen Schwellen, Zeitkonstanten und Annahmen als
   begründete Kommentare im Code, oder sind es unerklärte Zahlen?
3. **Randfälle.** Werden ungültige Messwerte, Stillstand, Rückwärtsfahrt,
   Handbremse, der erste Aufruf und ausgefallene Takte ausdrücklich behandelt?
   Ist die Rückfallebene bei Sensorausfall die sichere (Fahrerwunsch gilt) und
   nicht die gefährliche (Bremse wird weggenommen)?
4. **Echtzeittauglichkeit.** Keine unbegrenzten Datenstrukturen, keine
   Schleifen über Historien, kein Zustand außerhalb von `zustand`.
5. **Regeltreue.** Wurde wirklich nur der Rumpf der einen Funktion angefasst?

Nicht bewertet wird: Länge, Stil, Kommentarsprache.
