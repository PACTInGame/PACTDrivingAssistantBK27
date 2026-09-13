# `_temp/` — scratch space for modified tracers

Put **copies of `insim_trace.py`** here when a test needs measurements the base
tracer does not produce, and run them with:

```
python simulation_tests/run_scenario.py 04_drive_and_stop \
    --tracer _temp/autohold_tracer.py
```

Rules:

- Copy, never edit `insim_trace.py` itself.
- Never edit anything under `scenarios/` — a scenario is a fixed reference.
- Everything here is disposable and git-ignored. Nothing may import from here.

If a change here turns out to be generally useful (a new derived field, a packet
worth logging by default), move it into `packet_dump.py` / `config.py` properly
and delete the copy.
