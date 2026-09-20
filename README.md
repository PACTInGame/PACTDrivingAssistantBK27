# ABS-Benchmark (Branch `ki-benchmark`)

Dieser Branch enthält die Benchmark-Aufgabe **T7 — Antiblockiersystem**: ein
KI-Modell implementiert in genau einer Funktion ein ABS, das anschließend
deterministisch offline und danach im Fahrversuch in *Live for Speed* bewertet
wird.

Alles liegt in **[`t7/`](t7/README.md)** — das Verzeichnis ist bereits ein
vollständiges Task-Verzeichnis im Format von `AI-Benchmarks/tasks/` und wird
dorthin kopiert.

```
t7/task.yaml      Aufgabenstellung und Ausführungspolitik
t7/checks.py      deterministischer Prüfer (ohne LFS, ~2 s)
t7/rubric.md      Punkteschema und Fragen für den Judge
t7/workspace/     wird an das Modell geklont
t7/fahrversuch/   Prüfstand am LFS-Rechner (nicht Teil des KI-Workspace)
```

Einbau, LFS-Einrichtung, Szenario und Stand der Erprobung stehen in
[`t7/README.md`](t7/README.md).

> Die erste Fassung des Prüfstands lag flach im Wurzelverzeichnis
> (`lfs_link.py`, `abs_regelung.py`, `harness/`, `AUFGABE.md`). Sie ist in
> `t7/` aufgegangen und hier entfernt; nachlesbar bleibt sie in der Historie ab
> Commit `61b51bc`.
