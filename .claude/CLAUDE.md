# PACT Driving Assistant — Project Instructions

Add-on for the racing **simulator** "Live for Speed" (LFS). Adds ADAS-style driver
assistance (collision warning, blind spot, PDC, adaptive lights, HUD, automatic
gearbox), AI city traffic, and cop-roleplay features on top of the game.

Docs are written in English; the codebase mixes German and English comments — keep
each file's existing language when editing it.

---

## 1. The three rules that shape every code decision

**1. This is soft-real-time embedded software, not a script.**
Treat it as if it ran on an ECU in a real car. All assistance systems share **one**
`process()` pass, executed every `assistance_refresh_rate` ms (default 100, range
50–200). The UI pass runs every `ui_refresh_rate` ms (default 50). Everything in a
pass must finish well inside its budget **on a mid-range laptop**, with up to ~40
vehicles on track. Consequences:
- No per-cycle allocation storms, no O(n·m) scans over route/layout data in the hot
  path, no `print()` in `process()`, no blocking I/O (file, `winsound`, network).
- Precompute in `__init__` or on event, not per cycle. Cache. Use squared distances.
- If you add work to a `process()` method, say what it costs per cycle.

**2. LFS is a simulation, so the physics must be right.**
Kamm circle, tyre friction limits, wheel speeds, load transfer, real braking
distances. A warning threshold or braking calculation must be defensible physically,
not tuned until it "feels ok". State assumptions (µ, reaction time, safety buffer)
explicitly in code comments.

**3. The target is a robust, idiot-proof product.**
There are currently many small bugs, crash paths and unfriendly behaviours; removing
them is ongoing work with high priority. Every change should leave the app harder to
break: no unguarded dict/index access on packet data, no assumption that LFS state is
sane, no silent thread death. See `reference/known-issues.md`.

---

## 2. Hard constraints

| Constraint | Detail |
|---|---|
| **Python ≤ 3.11** | `pyinsim/core.py` uses `asyncore`, removed in Python 3.12. Do not "modernise" this casually — it means rewriting the transport layer. |
| **Windows only** | `winsound`, `vjoy`, `pyautogui`, LFS itself. |
| **LFS must be running** | with InSim enabled on port 29999 (`/insim 29999`, normally from `autoexec.lfs`) and **OutGauge enabled in `cfg.txt`** on 30000, OutSim on 29998. Without OutGauge every assistance system silently does nothing — `reference/lfs-setup.md`. |
| Third-party deps | `psutil, pyautogui, pynput, pygame, shapely, numpy, scipy, matplotlib` (see `requirements.txt`) |

Run with: `python main.py` (from project root).

---

## 3. Architecture in 10 lines

```
LFS  ──InSim/TCP──►  pyinsim ──► LFSConnector ──┐
     ──OutGauge/UDP─►                           ├──► EventBus ──► everything else
     ──OutSim/UDP───►                           ┘
```
- `main.py` builds every component and owns the lifecycle.
- **`EventBus` (`core/event_bus.py`) is the only interface between components.**
  Never let a subsystem hold a reference to another subsystem. Publish/subscribe only.
- `ThreadManager` runs one thread *per distinct interval*; all tasks sharing an
  interval run sequentially in that thread.
- Assistance systems subclass `AssistanceSystem` and implement
  `process(own_vehicle, vehicles) -> dict`. `AssistanceManager` calls them all.
- The main thread is blocked in `pyinsim.run()` (asyncore loop), so **InSim packet
  handlers run on the main thread while `process()` runs on worker threads** —
  shared state is genuinely concurrent.

Details: `reference/architecture.md`.

---

## 4. Where to look — routing table

Read **only** what the task needs. Do not read the whole `reference/` folder.

| If the task involves… | Read |
|---|---|
| Adding/changing an assistance system, event wiring, threading, lifecycle | `reference/architecture.md`, `reference/events.md` |
| Any coordinate, angle, speed, distance or unit maths | `reference/conventions.md` **(read this before touching geometry — it prevents the most common bug class)** |
| InSim packets, buttons, lights, sending commands, pyinsim internals | `reference/insim.md` |
| Behaviour/tuning of a specific assistance feature | `reference/systems.md` |
| AI traffic, routes, MapBuilder, `track_data/*.json`, layouts | `reference/ai-traffic.md` |
| HUD, menus, button IDs, settings keys, translations | `reference/ui.md` |
| **Which LFS screen are we on**, when buttons may be drawn, when key injection must be blocked | `reference/ui.md` §1 |
| App does not connect / connects but nothing happens / `cfg.txt`, OutGauge, `autoexec.lfs`, setup wizard | `reference/lfs-setup.md` |
| Fixing bugs / hardening / "why is this broken" | `reference/known-issues.md` |
| Writing or running tests | `reference/testing.md` |
| Raw LFS protocol truth (when pyinsim seems wrong/incomplete) | `C:\LFS\docs\InSim.txt`, `OutSimPack.txt`, `Commands.txt` — see `reference/insim.md` §6 |

`kontext_prompt` (project root) is the author's original hand-written briefing. It is
superseded by these docs but kept for context.

---

## 5. Working agreements

- **Modularity is non-negotiable.** New functionality = new module + event contract.
  If you need data from another component, subscribe to an event; if it does not
  exist, add it and document it in `reference/events.md`.
- **Think about side effects before editing.** This app is highly coupled through the
  bus. Before changing an event payload or name, grep for every emitter and
  subscriber. Report interactions you notice even outside the current task's scope.
- **Do not silently widen scope.** Fix what was asked; list other defects you spot.
- **Never re-enable automatic braking intervention** without being asked — it is
  deliberately disabled (`assistance/collision_warning.py`, `controller_emulator`).
- **`pyautogui` keypresses are global OS input.** Any code that injects keys must be
  blocked while `text_entry` or `dialog` is set, while the user holds **Shift** (LFS
  binds SHIFT+key commands), while not `on_track`, and when LFS is not the foreground
  window. `reference/ui.md` §1.4 has the full table — today only `AutoHold` guards at
  all, and only partially.
- **LFS is many screens, not one.** Buttons must not be drawn on the main menu or the
  multiplayer list, behave differently on the entry screen and in the pit/garage, and
  vanish by themselves in dialogs and text entry. `ISS_VISIBLE` and `IS_CIM` are the
  correct signals. `reference/ui.md` §1.
- **Never key a lookup table on `CName` without a safe fallback.** LFS vehicle mods
  produce arbitrary car names; derive car-specific parameters at runtime or calibrate
  them. `reference/conventions.md` §4.
- Prefer failing loudly at startup over failing silently in the loop.

---

## 6. Keep these instructions up to date — required

**Whenever you learn something that would have saved you time at the start of this
session, update these files before finishing.** This is part of the task, not an
extra.

Update when:
- a new module, assistance system, or event is added or renamed → `events.md`,
  `architecture.md`, `systems.md`
- a convention, unit, coordinate or protocol detail turns out to be different than
  documented → `conventions.md` / `insim.md`
- LFS screen/state behaviour or a button quirk is discovered → `ui.md` §1
- an LFS-side configuration requirement changes → `lfs-setup.md`
- a known issue is fixed, or a new systemic defect is found → `known-issues.md`
- an architectural or design decision is made → the relevant file, with the *why*

Do **not** write:
- a changelog or step-by-step log of what you did (git history covers that)
- entries for trivial or one-off edits
- anything that duplicates what the code already says plainly

Keep `CLAUDE.md` under ~200 lines — it is loaded into every session. Detail belongs
in `reference/`.
