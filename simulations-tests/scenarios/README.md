# Recorded scenarios

`run.py record <name>` creates one subdirectory with replay.json, timeline.md and
monitor.py. No synthetic recordings are shipped: screen coordinates, bindings and
game content must match the actual LFS installation. See ../README.md for the
recording procedure and the proposed seven scenarios.

Once reviewed, commit each scenario as the immutable baseline. AI test runs write
their results and customized monitor scripts under ../_temp only.
