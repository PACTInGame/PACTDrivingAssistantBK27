# Refactoring plan — PACT Driving Assistant

Goal of this plan: after all ten work packages are implemented, the add-on has **the
same feature set as today**, but without the current crash paths, logic errors and
usability traps. No new features. No re-enabling of automatic braking intervention.

Ten self-contained work packages (WP). Each WP is one agent session. Each WP names the
files it owns, so several agents can work in parallel without colliding.

**Constraint that shaped this plan:** implementation happens in a Linux cloud container
without LFS and without Windows. Therefore every WP is chosen so that it can be
*verified* by reasoning plus `pytest` on pure logic — not by running the game. Anything
whose only possible verification is "start LFS and look at it" is deliberately out of
scope (Tkinter wizard, vJoy, audio playback, MapBuilder plots).

---

## Order and dependencies

```
WP1  (test harness + portable imports)      ← do this first, everything else builds on it
 ├── WP2  (error isolation, logging, lifecycle)
 ├── WP3  (InSim output path)               ← WP5 depends on the button registry from WP3
 │     └── WP5  (screen context + UI lifecycle)
 ├── WP4  (vehicle data model + identity)   ← WP7, WP8, WP10 read its snapshot contract
 ├── WP6  (settings + menu)
 ├── WP7  (FCW physics)
 ├── WP8  (BSW / CTW / PDC)
 ├── WP9  (actuation: keys + lights)
 └── WP10 (AI traffic + dead code)
```

Everything except WP5→WP3 is independent. If WPs run in parallel, WP1 must be merged
before the others start, because they all add tests to `tests/`.

## File ownership

| WP | Owns (may edit freely) | Must not edit |
|---|---|---|
| 1 | `tests/**`, `pytest.ini`, `misc/platform_shim.py` (new), `requirements-dev.txt` | anything else beyond the minimal import moves listed in WP1 |
| 2 | `core/event_bus.py`, `core/thread_manager.py`, `core/connection_test.py`, `main.py`, `misc/logging_setup.py` (new), `assistance/manager.py` | assistance systems' internals |
| 3 | `lfs/connector.py`, `lfs/message_sender.py`, `pyinsim/core.py` (send path only) | `ui/**` |
| 4 | `vehicles/**`, `lfs/connector.py` (NPL/packet binding only — coordinate with WP3) | assistance systems |
| 5 | `lfs/lfs_state.py`, `ui/ui_manager.py`, `ui/menu_system.py` | `assistance/**` |
| 6 | `core/settings_manager.py`, `ui/menu_system.py` (settings-related handlers), `misc/language.py` | assistance systems' logic |
| 7 | `assistance/collision_warning.py`, `vehicles/vehicle.py` (acceleration only) | other systems |
| 8 | `assistance/blind_spot_warning.py`, `assistance/cross_traffic_warning.py`, `assistance/park_distance_control.py`, `misc/pdc_beep.py`, `misc/spacial_hash_grid.py` | other systems |
| 9 | `assistance/auto_hold.py`, `assistance/gearbox.py`, `assistance/adaptive_lights.py`, `misc/input_guard.py` (new), `misc/key_binder.py` | `lfs/connector.py` internals |
| 10 | `assistance/AI_Driver.py`, `AI_Control.py`, `assistance/navigation.py`, `AI_Cheatsheet.py`, `test.py`, `vehicles/VehicleInfo.py` | everything else |

Where two WPs must touch the same file, the later one rebases; the table says who is the
primary owner.

---

# WP1 — Test harness and platform-portable imports

**Why:** there are zero automated tests (`known-issues.md` #23), and today the codebase
cannot even be imported outside Windows: `assistance/gearbox.py` and
`assistance/auto_hold.py` import `pyautogui` at module level, `misc/pdc_beep.py` imports
`winsound`, `misc/vjoy.py` uses ctypes bindings, `core/setup_wizard.py` imports
`tkinter`. That makes every later WP unverifiable. This package creates the foundation
the other nine build on; it changes no behaviour.

**Scope**
1. Move Windows-only imports behind a small shim (`misc/platform_shim.py`): lazy accessor
   functions for `pyautogui`, `winsound`, `vjoy`, `tkinter`, with a no-op/recording
   fallback when the module is unavailable. Import sites become `from misc.platform_shim
   import get_keyboard()` style — the app must behave exactly as before on Windows, and
   must merely *import* cleanly elsewhere.
2. `pytest.ini` + `requirements-dev.txt` (`pytest`, `psutil`, `shapely`, `numpy`).
3. `tests/conftest.py` with the fixtures the other WPs will need:
   - a real `EventBus` plus a recorder helper that captures `(event, payload)`,
   - `make_settings(**overrides)` returning a `SettingsManager` backed by a tmp file,
   - `make_vehicle(plid, x, y, heading, speed, ...)` and `make_own_vehicle(...)`
     factories that take **metres and degrees** and convert to LFS units internally,
   - fake InSim/OutGauge packet builders (simple namespace objects with the fields the
     handlers read) — enough to drive `VehicleManager` and `StateHandler` without a socket.
4. First real tests, chosen so they already catch existing defects:
   - `misc/helpers.py`: `calc_polygon_points`, `point_in_rectangle` (rotated rectangles,
     degenerate cases),
   - `misc/language.py`: every key resolves in all 8 languages, unknown key falls back,
   - **every `AssistanceSystem` name passed to `super().__init__` exists in
     `SettingsManager._defaults`** — this test must fail today (`sat_nav`, see WP6) and be
     made to pass by WP6, so mark it `xfail(strict=False)` with a comment naming WP6,
   - `misc/language.py` strings are encodable as latin-1 (fails today for `tr`, fixed in
     WP3 — same `xfail` treatment).
5. `reference/testing.md`: replace "Status: not implemented" with what actually exists.

**Acceptance**
- `python -m pytest` runs green on Linux with only `requirements-dev.txt` installed.
- `python -c "import main"` fails only on the missing socket, not on imports.
- No behavioural change on Windows: every shimmed call site does the same thing it did.

---

# WP2 — Error isolation, logging and lifecycle

**Why:** `known-issues.md` #1 and #2. Exception handling is commented out in all three
loops — `EventBus.emit` (`core/event_bus.py:32-38`), `ThreadManager._run_cycle`
(`core/thread_manager.py:57-63`) and `AssistanceManager.process_all_systems`
(`assistance/manager.py:76-83`). One malformed packet kills a worker thread permanently
and the app keeps running with a silently dead assistance or UI loop. Shutdown
(`main.py:135-139`) is a `pass`, so buttons stay on screen in LFS and the socket is never
closed.

**Scope**
1. Per-subscriber isolation in `EventBus.emit`: one failing subscriber must not stop the
   others and must not propagate into the packet handler / asyncore loop.
2. Per-task isolation in `ThreadManager._run_cycle` and per-system isolation in
   `AssistanceManager.process_all_systems`, so a broken system disables itself rather than
   killing the thread. Suggested policy: log the first N failures per source, then
   rate-limit (e.g. one message per source per 30 s), and after k consecutive failures
   disable that system with a user-visible notification.
3. A real logging setup (`misc/logging_setup.py`): `logging` with a console handler and a
   rotating file handler next to the executable. Replace the `print()` calls in the hot
   paths and packet handlers — in particular `lfs/connector.py:126` (`print(mso.Msg)` on
   **every chat message**), `core/settings_manager.py:65` (print on every settings write),
   `assistance/park_distance_control.py` AXM prints, `ui/ui_manager.py:118`
   (prints the whole notification list). Keep `print()` only for the startup banner.
   **No logging call inside `process()` at default level** — the real-time rule stands.
4. Watchdog: `ThreadManager` records `last_execution` per task (the field exists and is
   only written, never read). Add a check that warns if a task has not run for >5×
   its interval, and log when a cycle overruns its budget.
5. Clean shutdown: remove all buttons the app created, send `ISP_TINY/TINY_CLOSE`,
   `pyinsim.closeall()`, join threads with a timeout, and install a `SIGINT` path that
   works while blocked in `pyinsim.run()`.
6. Startup: `core/connection_test.py` blocks forever in `pyinsim.run()` when LFS never
   answers, so the retry loop in `main.py:38-51` can never run a second attempt (the same
   already-closed dispatcher is reused). Give the test a timeout and rebuild the object per
   attempt. The two backoff loops in `main.py` should also print *why* the app is waiting.

**Acceptance**
- A subscriber that raises does not stop other subscribers, does not kill the emitting
  thread, and is reported once (not once per cycle).
- Killing a system with an induced exception leaves the rest of the app running.
- Tests: subscriber-raises, task-raises, system-raises, rate-limiter, connection test
  timeout (with a fake pyinsim).
- Update `reference/known-issues.md` (#1, #2) and `reference/architecture.md`.

---

# WP3 — InSim output path: thread safety, button registry, text encoding

**Why:** three defects that all live between the app and the LFS socket.

1. **The send buffer is not thread-safe.** `pyinsim/core.py:316` does
   `self._send_buff += data` while the asyncore thread does
   `self._send_buff = self._send_buff[sent:]` (`:322-323`). Buttons are sent from the UI
   worker thread and assistance threads, packets are drained on the main thread — an
   append that interleaves with the slice assignment is silently lost. Because InSim is a
   length-prefixed stream, a partial loss desynchronises the protocol; losing the keepalive
   reply (`pyinsim/core.py:562`) makes LFS drop the connection after ~70 s. This is the most
   likely cause of "it just disconnects sometimes".
2. **Buttons are re-sent unconditionally.** `MessageSender.create_button`
   (`lfs/message_sender.py:27-32`) sends an `IS_BTN` every call, and `UIManager.update_hud`
   redraws the HUD, PDC (up to 18 buttons), siren buttons and notifications **every 50 ms**.
   That is a continuous packet storm for content that rarely changes.
3. **Text encoding breaks non-latin-1 languages.** `LFSConnector.send_button`
   (`lfs/connector.py:186-190`) tries latin-1 and falls back to UTF-8 on failure; LFS then
   renders mojibake. 53 of the shipped translation strings are affected — all Turkish, plus
   a few others. LFS's own code-page escapes (`^E`, `^T`, `^C`, …, see `reference/insim.md`)
   are the correct mechanism.

**Scope**
- Thread-safe outbound path: a lock or `queue.Queue` around the send buffer (patch inside
  `_TcpSocket`, the smallest correct place), so `send()` from any thread is atomic with the
  drain. Document the decision in `reference/insim.md`.
- A **button registry** in `MessageSender`: `(id → last sent style/pos/size/text)`.
  `create_button` sends only on change; `remove_button` only deletes ids that are actually
  live; add `remove_all()` and `remove_range()` built on the live set — this also fixes
  `known-issues.md` #10 (`UIManager._state_change` sending 239 delete packets,
  `ui/ui_manager.py:202`). Keep the public method names so WP5 can build on them.
- A single `encode_button_text()` helper: latin-1 where possible, LFS code-page escapes
  otherwise, plus width-aware truncation so a long notification cannot overflow its button.
- Optional but recommended: a send-rate counter exposed for tests ("HUD idle for 1 s emits
  ≤ N packets").

**Acceptance**
- Tests: concurrent `send()` from 8 threads loses no bytes and produces a byte stream that
  parses back into exactly the packets sent; unchanged button → no packet; changed text →
  one packet; `remove_button` for an unknown id → no packet; every translation string
  round-trips through `encode_button_text` and back to the same visible text.
- Update `reference/insim.md` (encoding + thread safety) and `known-issues.md` (#10).

---

# WP4 — Vehicle data model, identity and MCI reassembly

**Why:** the layer every assistance system reads from is the least trustworthy part of
the app.

1. **Frame reassembly is guesswork.** `VehicleManager._handle_vehicle_data`
   (`vehicles/vehicle_manager.py:82`) decides a frame is complete when
   `received_cars_count == len(self.players)`. With >16 cars MCI splits into several
   packets, and if the `players` dict is stale by one entry, `vehicles_updated` **never
   fires again** and every assistance system freezes on old data with no error.
   `CompCar.Info` carries `CCI_FIRST`/`CCI_LAST` for exactly this (`reference/insim.md` §2).
2. **Concurrent mutation.** Packet handlers mutate `self.vehicles` on the main thread while
   assistance systems iterate it on worker threads (`known-issues.md` #12). Only
   `ParkDistanceControl` works around it with `.copy()`. `RuntimeError: dictionary changed
   size during iteration` is reachable in FCW, BSW, CTW, AIDriver.
3. **Own PLID comes from OutGauge, so it follows the camera.**
   `OwnVehicle.update_outgauge_data` (`vehicles/own_vehicle.py:42`) sets
   `data.player_id = packet.PLID`, which is the *viewed* player. Pressing TAB repoints the
   whole own-vehicle object at someone else's car (`known-issues.md` #30). `IS_NPL` carries
   `UCID`/`PType`, which identify the local driver camera-independently, and
   `_handle_player_joined` (`vehicles/vehicle_manager.py:108-117`) throws both away.
4. **Control mode is parsed from a binary string.** `_get_control_mode`
   (`vehicles/vehicle_manager.py:100-106`) does `bin(npl.Flags)[2:]` and indexes with
   negative offsets. `bin()` drops leading zeros, so the bit positions shift depending on
   the value. Use the documented `PIF_*` masks.
5. **bytes/str is ad hoc everywhere.** `CName`/`PName`/`Track` stay bytes and are converted
   with repr hacks: `str(cname)[2:-1]` (`assistance/gearbox.py:87-88, 105-106`),
   `str(self.current_track[:2])[2:4]` (`assistance/AI_Driver.py:402`), and
   `LightAssists._on_player_name_changed` does `str(player_name)` on bytes so the cop-tag
   check `'[cop]' in ...` runs against `"b'[COP] Name'"`. Decode once at ingress.

**Scope**
- Reassemble MCI with `CCI_FIRST`/`CCI_LAST`, with a timeout fallback; emit
  `vehicles_updated` with an **immutable snapshot** (a fresh dict, values not mutated
  afterwards) so consumers never see a half-updated world.
- Decode `PName`/`CName`/`Track` to `str` once, in `VehicleManager`, and keep the raw bytes
  alongside if something needs them. Update every consumer named above.
- Bind `IS_NPL`/`IS_PLL` properly: store `UCID`, `PType` (AI flag, bit 1), `Flags`; derive
  the **local driver's PLID** from `UCID == 0` (local InSim, `ISF_LOCAL`) and keep OutGauge
  only as the source of *gauge* data. Expose `own_vehicle.is_local_driver` so WP9 can gate
  actuation on it (`reference/conventions.md` §5).
- Add `is_ai` from `PType` and expose it — WP10 replaces its name-substring heuristic with it.
- Fix `_get_control_mode` with `PIF_*` masks.
- Guard every packet-field access: MCI/NPL packets from a modded or older LFS must not raise.

**Acceptance**
- Tests: split MCI (17 cars in 2 packets) produces exactly one `vehicles_updated` with 17
  cars; a missing player in `players` does not stall updates; snapshot is not mutated by a
  subsequent packet; control mode parsed correctly for the documented flag combinations;
  own PLID stays put when OutGauge reports a different PLID (camera change); AI flag from
  `PType`.
- Update `reference/conventions.md` §4/§5, `reference/events.md` (payload of
  `vehicles_updated`, new fields), `known-issues.md` (#6, #12, #30, #31).

---

# WP5 — Screen context and UI lifecycle

*Depends on the button registry from WP3.*

**Why:** `known-issues.md` #25, #26, #27 plus a set of UI bugs visible in the code.

1. `StateHandler` (`lfs/lfs_state.py`) derives only `on_track`, `text_entry`, `dialog`. It
   never reads `ISS_VISIBLE` (the authoritative "our buttons are on screen" flag), never
   binds `ISP_CIM`, and its `in_game_interface` / `submode_interface` fields are
   initialised to 0 and never written (`lfs/lfs_state.py:14-16`) although they are part of
   the `state_data` payload that seven components consume.
2. `IS_BFN` is not bound at all, so when the user presses SHIFT+B (clear buttons), the menu,
   PDC and notification buttons are gone until something else redraws them.
3. **Button ID 1 is used twice**: as the HUD speed field (`ui/ui_manager.py:238`) and as the
   off-track "PACT Driving Assist Active." banner (`ui/ui_manager.py:209`). Leaving the
   track draws the banner over the HUD id; entering the track deletes it again. The comment
   block at the top of `ui_manager.py` claims 1-10 are HUD.
4. That banner is drawn **whenever `on_track` is false** — including the main menu and the
   multiplayer server list, where `reference/ui.md` §1.1 says nothing must be drawn.
5. `hud_width`/`hud_height` are freely adjustable in 2-unit steps (`ui/menu_system.py`
   HUD Up/Down/Left/Right) with no constraint, so the HUD can be moved into the reserved
   area `L 0…110, T 30…170` and make LFS's own entry-screen menus vanish (`ui.md` §1.3).
   The siren buttons and PDC display are positioned relative to the HUD and can end up
   off-screen or overlapping the menu.
6. The HUD's "redline" is simply the highest RPM ever seen (`ui/ui_manager.py:224`), so the
   RPM readout turns red at every new maximum and never recovers after a car change; it is
   also never reset except on leaving the track.
7. Flash logic is broken: `update_hud` toggles `collision_warning_color` for level ≥2 and
   then immediately resets it for level ≤2 (`ui/ui_manager.py:230-237`), so level 2 never
   actually flashes.
8. Notifications are shown one every 3 s from an unbounded list (`ui/ui_manager.py:116-131`);
   a burst (e.g. the gearbox calibration sequence) queues messages for a minute, and the
   queue is never cleared when leaving the track.

**Scope**
- Extend `StateHandler`: parse `ISS_VISIBLE`, `ISS_SHIFTU`, `ISS_MULTI`; bind `ISP_CIM` in
  `LFSConnector` and fill `in_game_interface`/`submode_interface`; widen `state_data` with a
  single derived `ui_visible` flag (and document it). Keep the existing keys — seven
  subscribers depend on them.
- Bind `IS_BFN` and treat `BFN_USER_CLEAR`/`BFN_REQUEST` as "our button set is gone":
  invalidate the WP3 registry and repaint the currently valid screen.
- One central button-ID map (a module-level `IntEnum` or constants block) replacing the
  comment block; fix the id-1 collision; make `hide_hud`/`remove_pdc_display` use the WP3
  live set instead of hardcoded ranges.
- Draw nothing on the main menu / server list; draw the banner only on the entry screen.
- Clamp HUD position so the whole HUD, PDC block, siren buttons and notification stay on
  screen and out of the reserved rectangle (or warn in the menu when the user moves into it).
- Fix flashing (one owner of the blink phase, driven by a timer, not by call order), fix the
  redline heuristic (use the gearbox calibration value when present, otherwise no red at
  all), and bound the notification queue (max N, drop oldest, clear on track exit).

**Acceptance**
- Tests against fake `IS_STA`/`IS_CIM`/`IS_BFN` packets: each documented screen maps to the
  expected context; no button is created in main-menu context; after `BFN_USER_CLEAR` the
  next UI pass repaints; HUD clamp keeps every element inside 0…200/0…200 and outside the
  reserved rectangle; level-2 warning alternates styles across successive passes; the
  notification queue never exceeds its cap.
- Update `reference/ui.md` §1 and `known-issues.md` (#25, #26, #27).

---

# WP6 — Settings: migration, validation, and menu consistency

**Why:** the settings layer silently disables features.

1. `SettingsManager.load` (`core/settings_manager.py:71-82`) replaces `_settings` with the
   file contents. Keys added in a later version are **never merged in**, and
   `get(key, default)` returns the caller's default whenever it is not `None`
   (`core/settings_manager.py:60-62`). `AssistanceSystem.is_enabled` calls
   `self.settings.get(name, False)` (`assistance/base_system.py:42-44`) — so for any user
   with an older `settings.json`, every newly added system is **off forever** and the menu
   shows it as off with no way to know why.
2. `sat_nav` has no settings key at all (`known-issues.md` #18), so `NavigationSystem` is
   dead code that is still constructed and iterated every cycle.
3. No validation: `assistance_refresh_rate` / `ui_refresh_rate` are used as the InSim MCI
   `Interval` and as thread periods without range checks; a hand-edited `0` busy-loops the
   app. `park_distance_control` and `park_distance_control_mode` are two settings for one
   concept and can contradict each other (`ui/menu_system.py` parking handler sets both).
   `own_control_mode` is overwritten from NPL flags on every player-name change
   (`ui/menu_system.py:42-44`) so the user's own choice never survives.
4. `save()` rewrites the file on **every** `set()` (and `set` prints). Menu clicks therefore
   do blocking file I/O from the packet thread.
5. Language: `MenuSystem` caches `self.set_language` at construction and updates it only in
   `change_language`, while every other component reads `settings.get('language')` live —
   a language change made through the chat command path would not reach the menu.

**Scope**
- Merge defaults on load (deep, per key), keep unknown keys, write atomically
  (tmp + replace), debounce saves, and make `get()` fall back to the built-in default
  when the key is missing — the caller-supplied default must not shadow it. Add a schema:
  type, range, allowed values, with clamping and a logged warning on repair.
- Version the file (`"_version"`) and add a migration hook for future renames.
- Decide and implement one representation for PDC (recommendation: keep
  `park_distance_control_mode` 0/1/2 only, derive the boolean) and make the menu reflect it.
- Remove `sat_nav` from `AssistanceManager._init_systems` and mark `NavigationSystem` as
  not-wired (WP10 decides its fate), or add the settings key — pick one and document it.
- Stop overwriting `own_control_mode` from NPL; expose the detected mode separately.
- Enable the WP1 test "every system name has a defaults entry" (remove the `xfail`).
- Menu usability while you are in there: the current handler dispatch is `if
  current_menu == ... and button_id == ...` over reused ids 20–40 (`ui/menu_system.py:
  365-533`); convert it into a per-menu table of `(id → action)` so an id can never be
  interpreted by the wrong menu, and so a new entry cannot silently shadow another.

**Acceptance**
- Tests: settings file from an older version gains the new keys and keeps user values;
  corrupt JSON falls back to defaults and does not crash; out-of-range values are clamped
  and logged; `get('unknown_key')` returns `None`, `get('known_key', False)` returns the
  default, not `False`; every `AssistanceSystem` name resolves to a defaults key; menu
  action tables cover every button drawn by that menu (test derives it from the button list).
- Update `reference/ui.md` §settings and `known-issues.md` (#18).

---

# WP7 — Forward collision warning: physics and warning logic

**Why:** FCW is the flagship feature and currently has four defects that make it fire at
the wrong times.

1. **Heading wrap-around disables the system.** `reversing` is computed as
   `heading - direction > 10000 or < -10000` (`assistance/collision_warning.py:22`) on raw
   0…65535 units. Driving straight with `heading = 100` and `direction = 65500` (a 0.6°
   difference across the wrap point) gives `-65400` → "reversing" → **FCW switches itself
   off** in one heading sector. The identical expression sits in
   `assistance/adaptive_lights.py:141` and disables the adaptive brake light there.
   The correct form is a signed modular difference.
2. **Sign confusion in the braking result.** `_calculate_needed_braking` returns
   `abs(req_accel)` (`:169`), and the panic branch returns `20` with the comment
   "(negative)". A *positive* required acceleration (we do not need to brake at all) comes
   back as a large positive "needed braking" and can raise a warning level.
3. **The warning level latches.** The level-3 and level-2 branches accept
   `needed_braking > 0 and self.current_warning_level > 2` (`:53-56`), and
   `current_warning_level` is only written when the level changes — so once level 3 is
   reached, any non-zero required braking keeps it at 3 until the required braking hits
   exactly 0. Hysteresis is wanted; a latch is not.
4. **Acceleration is scaled by a hardcoded timestep.** `Vehicle.update_position`
   (`vehicles/vehicle.py:46`) computes `(Δ km/h) * 2.778`, which is only m/s² if MCI
   packets arrive exactly every 100 ms. `assistance_refresh_rate` is user-configurable
   (50–200 ms) and drives the MCI `Interval`, so every acceleration — and thus FCW's
   braking maths and the adaptive brake light threshold — is silently scaled wrong
   (`known-issues.md` #13). Use the real elapsed time between packets, and smooth it.

**Scope**
- Fix 1–4. Keep the physical model documented in comments: µ assumption, reaction-time
  buffer (currently a flat 0.2 s applied only when closing, `:129`), safety buffer (0.5 m),
  and state where they come from.
- Add the cheap rejection the hot path is missing: squared-distance gate and a heading gate
  before the polygon test, and skip the `dist_debug` emission entirely (`:132-134`) — its
  subscriber is commented out (`known-issues.md` #15).
- Car length currently comes from `park_distance_control.get_vehicle_size(cname)`, which
  returns `(4.5, 1.8)` for every mod (`known-issues.md` #28). Take a fallback strategy that
  degrades safely (e.g. use the largest plausible length so the warning is early rather
  than late) and note it in `reference/conventions.md` §4.
- Do not touch the disabled braking intervention. `needed_deceleration_update` keeps its
  contract.

**Acceptance**
- Tests with hand-computed expectations: closing at 100 km/h on a stationary car 50 m
  ahead → required deceleration matches `v²/2d` within tolerance; lead car slower but not
  braking → 0; inside the buffer → panic value; a vehicle 3 m to the side is not "ahead";
  driving north across the heading wrap is not detected as reversing; warning level rises
  and **falls** with the situation (no latch); identical inputs twice emit only once.
- Update `known-issues.md` (#13, #15) and `reference/systems.md` (FCW thresholds).

---

# WP8 — Blind spot, cross traffic and PDC: logic and hot-path cost

**Why:** these three run every cycle over every vehicle and each has both a logic bug and
a performance problem.

**Blind spot warning** (`assistance/blind_spot_warning.py`)
- `_create_rectangles_for_blindspot_warning` (`:40-50`) builds a `shapely.Polygon` for
  **every** car on track — up to 40 per cycle — before any cheap rejection
  (`known-issues.md` #7). Add a squared-distance and side gate first, and build the own-car
  rectangles once per cycle instead of per call.
- The trigger condition `rectangle[1] < (rectangle[0] - own_speed + 5) * 1.2` (`:82-84`)
  compares a **distance in metres** against a **speed difference in km/h**. For any car
  slower than or equal to us the right-hand side is ≤ 0, so a car sitting in the blind spot
  at constant speed can never trigger a warning — which is the single most common real case.
  Replace with an explicit geometric condition (is it inside the blind-spot polygon) plus a
  relative-speed based hold time.
- `_normalize_angle` (`:22-25`) returns `abs(angle)`, mirroring negative angles instead of
  normalising them.

**Cross traffic warning** (`assistance/cross_traffic_warning.py`)
- Gated on `own_vehicle.gear <= 1` (`:110`), i.e. no warning in neutral or reverse and none
  at all for a car whose gear is not reported. Rethink the gate (speed + direction, not gear).
- Vehicles are treated as points: `_find_intersection` uses ray/ray with a ±0.5 s arrival
  tolerance (`:96`, `:160-166`), so a long vehicle crossing slowly is missed. Add a
  size-aware arrival window.
- The comment block in `_direction_vector`/`_compute_side` (`:14-19`, `:69-73`) states that
  LFS's Y axis grows south and headings are clockwise. Per `InSim.txt` the system is
  right-handed with anticlockwise headings; the code is right, the comment is wrong and will
  mislead the next change (`known-issues.md` #16). Fix the comments, keep the behaviour, and
  pin it with a test.

**PDC** (`assistance/park_distance_control.py`, `misc/pdc_beep.py`)
- `PDCBeepController.beep()` spawns a **thread per beep** running blocking `winsound.Beep`
  (`misc/pdc_beep.py:53-55`, `known-issues.md` #14). Replace with one long-lived beeper
  thread fed by state, so a fast approach cannot spawn dozens of threads.
- The AXM object id is built by string-concatenating index and coordinates
  (`assistance/park_distance_control.py:270`, `:284`) — `int(str(idx)+str(abs(X))+...)`.
  Different objects collide (e.g. `X=1, Y=23` vs `X=12, Y=3`), so deleting one object can
  evict another. Use a tuple key or a proper hash.
- `create_rectangle_for_object` scales layout coordinates by 4096 with a `TODO check if
  correct for 65536 scale` (`:190-192`). Verify against `reference/conventions.md` and
  remove the doubt — if it is wrong, every static PDC obstacle is at the wrong place.
- PDC only runs below 10 km/h (`:308`) but the display is refreshed from the UI thread every
  50 ms regardless; with the WP3 registry that becomes free, but also make the sensor result
  emit only on change (it already compares, `:349`) and make the -1/0 "inactive vs clear"
  distinction consistent with what `UIManager._update_pdc` expects.

**Acceptance**
- Tests: a car of the same speed in the blind spot warns; a car 60 m behind does not; the
  polygon count built per cycle is bounded by the pre-filter (assert on a counter);
  perpendicular paths at a junction produce a warning with the correct side (left/right
  pinned by test); parallel paths produce none; two AXM objects with colliding old-style
  ids are stored and removed independently; the beeper spawns at most one thread.
- Update `known-issues.md` (#7, #14, #16) and `reference/systems.md`.

---

# WP9 — Actuation: key injection and light commands

**Why:** everything in this package reaches out of the app — into the OS keyboard or into
the car's lights — and none of it is properly guarded.

1. **`pyautogui` key injection is nearly unguarded** (`known-issues.md` #11).
   `Gearbox._execute_shift` (`assistance/gearbox.py:180-187`) presses clutch and shift keys
   with **no guard at all**; `AutoHold` (`assistance/auto_hold.py:41-47`) checks only
   `dialog`/`text_entry`. Neither checks whether LFS is the foreground window (so the app
   types into the user's browser), nor whether the user is holding **Shift** (LFS binds
   SHIFT+key commands, so an injected `s` becomes a command), nor whether the own vehicle is
   actually the local driver's car (WP4's `is_local_driver`), nor the control mode.
   `reference/ui.md` §1.4 has the full table.
2. **Rebinding does not take effect.** `Gearbox` caches the key settings in `__init__`
   (`assistance/gearbox.py:60-63`), so a key rebound through the menu is only used after a
   restart, while `AutoHold` reads the setting live — inconsistent and confusing.
3. **Calibration UX is hostile.** Each gearbox calibration step is a blind 12-second wait
   (`assistance/gearbox.py:243-270`) with no countdown, no confirmation, no cancel; the only
   abort is moving the car. `max_gears` is stored as the raw gear index and displayed as
   `max_gears - 1` in some places and not others.
4. **High-beam assist spams the connection and overrides the driver.**
   `LightAssists.process` (`assistance/adaptive_lights.py:157-170`) emits a light command
   **every cycle** — 10 per second — regardless of whether anything changed, and
   unconditionally forces low beam on whenever no lights are on, so the user can never
   drive without lights. It also has no day/night notion at all. Emit only on change, and
   let the user opt out.
5. **Siren/strobe state is duplicated** (`known-issues.md` #17): `UIManager`
   (`ui/ui_manager.py:66-75`) and `LightAssists` (`assistance/adaptive_lights.py:80-88`)
   each keep their own `siren_active`/`strobe_active`, both driven by the same
   `button_clicked` event and both hardcoding ids 62/63. The chat command path
   (`$siren`, `$strobe`) toggles only the `LightAssists` copy, so the button caption and the
   real state drift apart. One owner, one event, UI subscribes.
6. `disable_siren()` (`assistance/adaptive_lights.py:130-135`) switches the **low beam on**
   as a side effect of turning the siren off, and the strobe pattern advances one step per
   `process()` call, so its speed changes with `assistance_refresh_rate`.

**Scope**
- A single `misc/input_guard.py` used by every key-injecting call site: `may_inject()`
  returning a reason when it refuses (on-track, no dialog/text entry, no Shift held —
  `OutGaugePack.Flags & OG_SHIFT`, LFS in foreground, own car is the local driver,
  control mode compatible). Log/notify the reason once, never per cycle.
- Read key settings live; subscribe to setting changes.
- Calibration: countdown notifications, explicit cancel, sane storage of `max_gears`.
- Light commands: dedupe (only emit on change), never fight the user's manual light input,
  make the strobe pattern time-based rather than cycle-based, remove the low-beam side
  effect from `disable_siren`.
- Siren/strobe: `LightAssists` owns the state and publishes `siren_state_changed` /
  `strobe_state_changed`; `UIManager` only renders. Document both events in
  `reference/events.md`.
- Do **not** re-enable braking intervention; `ControllerEmulator` stays commented out.

**Acceptance**
- Tests with a fake keyboard: no keypress while dialog/text entry/Shift/off-track/foreground
  is wrong; a keypress in the allowed state; a rebind takes effect without restart;
  high-beam assist emits one command per state change, not per cycle; strobe timing is
  independent of the refresh rate; toggling the siren through the chat command updates the
  button caption.
- Update `reference/ui.md` §1.4, `reference/events.md`, `known-issues.md` (#11, #17, #22).

---

# WP10 — AI traffic: hot-path cost, robustness, start/stop UX, and dead code

**Why:** AI traffic is the heaviest thing in the cycle and the least defensive.

1. **O(cars × route length) per cycle.** `_drive_vehicle` calls
   `get_closest_index_on_route` (`assistance/AI_Driver.py:84-110`), which scans the entire
   route point list for every controlled car, every cycle, and then runs
   `analyze_upcoming_track` **twice** (normal + 120 m long-straight lookahead,
   `:988-1005`). With a full traffic grid this dominates the 100 ms budget
   (`known-issues.md` #9). Cache the last index per vehicle and search a local window
   around it (with a full-scan fallback when the vehicle is off-route); cache the
   long-straight analysis per (route, index-bucket).
2. **AI cars are detected by substring.** `_is_local_ai_vehicle` (`:645-656`) returns true
   for `'AI' in pname`, so a human called MAIK, RAID or CAIN is taken over by the traffic
   controller and driven by it (`known-issues.md` #31). Use `PType` bit 1 from WP4.
3. **Starting traffic restarts the session without asking.** `_on_start` sends
   `/axload AI_Traffic` and `/restart` (`:428-429`), discarding whatever layout the user had
   loaded, with no confirmation and no way back. Add a confirmation step in the menu and
   tell the user what will happen.
4. **Track handling is repr-hacked**: `str(self.current_track[:2])[2:4]` (`:402`, `:459`) —
   replace with WP4's decoded track string. `_on_state_data` (`:382-390`) compares the track
   on every `IS_STA` and clears `self.routes` as a side effect.
5. Route files are loaded with no validation (`load_routes_from_file`, `:68-81`): a malformed
   `track_data_*.json` raises inside a packet handler. Validate the shape and report a
   readable error.
6. Per-cycle `print()` calls in the driving path and the AI-info timeout path (`:682`, `:699`)
   go through WP2's logger instead.
7. **Dead weight to remove** (`known-issues.md` #19, #20, #21, #5/#18):
   - `AI_Cheatsheet.py` — a 476-line stale duplicate of `AI_Control.py`, imported by nothing.
   - `test.py` — not a test, a live capture script for `MapBuilder`; move to
     `tools/capture_layout.py` so pytest never collects it.
   - `vehicles/VehicleInfo.py` — a parallel vehicle container nothing constructs.
   - `assistance/navigation.py` — 591 lines, inert (no settings key), dozens of `print()`
     per cycle and a per-cycle full scan of every road segment. **Recommendation: delete it**
     and keep the idea in `reference/systems.md`; resurrecting it later from git history is
     cheaper than carrying it. If the owner wants it kept, it must at least stop being
     constructed every cycle.

**Acceptance**
- Tests: the windowed route search returns the same index as a full scan for a car moving
  along the route, including across the wrap point, and falls back correctly when the car is
  teleported; a car named "MAIK" is not adopted; malformed route JSON produces an error, not
  an exception in the packet handler; a benchmark test asserts `_process_active` with 20
  vehicles on a 2000-point route stays under a stated per-cycle budget (relative regression
  guard, not an absolute promise).
- Update `reference/ai-traffic.md`, `reference/systems.md`, `known-issues.md`
  (#5, #9, #19, #20, #21, #31).

---

## Cross-cutting rules for every work package

These apply to all ten and are repeated in the agent prompt:

- **No new features.** Feature parity with today, minus the bugs.
- **Never re-enable automatic braking intervention** (`reference/control-intervention.md`).
- **The hot path stays cheap.** If a `process()` method gains work, the commit message and
  the docs say what it costs per cycle.
- **Every event change is a contract change** — grep every emitter and subscriber first and
  update `reference/events.md`.
- **Keep each file's existing comment language** (German files stay German).
- **Tests are part of the package**, not an optional extra. They must run on Linux without
  LFS.
- **Update the reference docs the change touches** and remove fixed entries from
  `known-issues.md`. Do not write a changelog — git history covers that.

---

## Points that cannot be verified without LFS

Implementation happens on Linux without LFS and without Windows, so `pytest` is the only
feedback loop. Anything a work package changed that only the running game can prove is
listed here, per WP, as a manual check list for the author. **Every WP agent appends its
own subsection before finishing** — one line per item: what to do in the game, and what
the correct outcome looks like. Remove an item once it has been checked in-game.

### WP1 — test harness and platform-portable imports

The Windows-only modules are now imported lazily through `misc/platform_shim.py`. The
call sites make the same calls with the same arguments, but only real Windows proves that
the accessor hands out the real module everywhere it used to be imported.

- **Auto-hold**: stop the car with the brake held; the handbrake must engage once and the
  "Auto Hold" notification must appear. (`get_keyboard().keyDown/keyUp`)
- **Automatic gearbox**: enable it and drive; up- and downshifts must happen as before,
  including the clutch key being held around the shift key.
- **PDC beep**: reverse towards an obstacle; the front/rear beep patterns must sound.
  (`get_sound().Beep`)
- **Warning sounds**: trigger a collision warning; the `.wav` must play.
  (`get_audio().mixer`)
- **Key rebinding**: bind a key and a mouse button in the menu; both must be captured and
  stored. (`get_input_listener()`, i.e. `pynput`)
- **Setup wizard**: run it on a fresh install (delete `.setup_done`); every step must
  appear as before. `tkinter` is now resolved per method instead of at import.
- **Startup on a machine with a missing dependency**: if one of those packages is not
  installed, the app no longer crashes at import — it logs a warning on first use and that
  feature silently does nothing. Confirm the warning is visible enough, or let WP2's
  logging/startup work make it a loud startup check.

### WP2 — error isolation, logging and lifecycle

Everything here is exercised by `tests/test_error_isolation.py` and
`tests/test_lifecycle.py`, but the loops it protects only exist while LFS is feeding
packets, and the shutdown only matters inside the game.

- **Shutdown leaves no trace**: start the app, join a track so the HUD and menu are on
  screen, then close the app (window close, Ctrl+C, or the console `X`). Every PACT
  button must disappear from LFS immediately, and LFS must report the InSim connection
  as closed — nothing left over, and no need to restart LFS before starting the app
  again.
- **Ctrl+C while driving**: the app must print "Shutting down LFS Assistant..." and exit
  within about a second, not hang in the asyncore loop.
- **Retry on startup**: start the app *before* LFS is up, or with InSim off. It must keep
  logging "No answer on InSim port 29999 …" every few seconds and then connect by itself
  once `/insim 29999` is typed — the old build hung on the first attempt forever.
- **The log file**: `pact_assistant.log` must appear next to the executable and contain
  the startup lines. Chat messages must **not** appear in it at default level (they used
  to be printed for every message).
- **A failing system disables itself**: hard to trigger deliberately; if any assistance
  system ever stops working, check for a `^1… disabled - see log` notification and the
  traceback in the log rather than assuming the app is fine.
- **Frame-rate impact**: with the error handling and the button registry in place, watch
  for any change in LFS's frame rate on a busy track (~40 cars). It should be the same or
  slightly better.

### WP3 — InSim output path

- **The disconnect that never happens**: drive a long session (>10 minutes) with the HUD,
  PDC and menu in use. The connection must not drop by itself — the unlocked send buffer
  losing the keep-alive reply was the most likely cause of that.
- **Turkish (and other) menus**: set the language to `tr` in the menu. Every label must
  read correctly — `Sürüş Ayarları`, `Çarpışma Uyarısı` — not as `SÃ¼rÃ¼Å` mojibake.
  Check `de`, `se`, `no`, `dk` and `fr` too, and the periodic chat tooltips as well as
  the buttons.
- **Button text capacity**: the truncation constant in `lfs/text_encoding.py`
  (`button_text_capacity`) is calibrated from one shipped string, not from an LFS font
  metric. Watch for any notification or menu label ending in `..` that used to be
  complete — if that happens the constant is too small and should be raised.
- **Repaint after SHIFT+B**: press SHIFT+B in game to clear all InSim buttons. The HUD
  must come back within one UI cycle. The menu, PDC and notification buttons will still
  stay away until the next state change — that part is WP5's.
- **Leaving the track**: drive out to the entry screen. The HUD must vanish and the
  "PACT Driving Assist Active." banner appear, with no visible flicker (this path used to
  send 239 delete packets in one go).
- **Colour codes**: the coloured warning labels (`^1`, `^3`, `^7`) must still be
  coloured — the encoder deliberately leaves carets alone.

### WP4 — vehicle data model, identity and MCI reassembly

The frame logic, the snapshot contract, the `PIF_*` control modes and the identity
rules are covered by `tests/test_vehicle_model.py`. What only LFS can prove:

- **More than 16 cars**: put 20+ AI cars on track (SO City or an oval). The HUD, FCW,
  BSW and PDC must keep updating smoothly — before, a stale `players` dict froze every
  assistance system on old data with no error. Watch for `MCI frame still incomplete`
  in `pact_assistant.log`: it should never appear.
- **TAB / spectating**: drive, then press TAB to look at an AI car. The HUD gauges
  (rpm, gear) follow the viewed car as before, but warnings must keep referring to
  *your* car and must not jump. Back on your own car, nothing should have shifted.
- **Own PLID in the garage**: enter the pits/garage, where OutGauge stops. The app must
  still know your PLID (it now comes from `IS_NPL`), so nothing needs a track re-entry
  to recover.
- **Multiplayer as a guest**: join someone else's host. `UCID` 0 is the *host* there,
  not you — the local driver is identified by `PType` instead. Check that the HUD and
  the cop-mode siren UI still attach to your own car and not to the host's.
- **Cop tag**: rename yourself to `[COP] Something`. The siren/strobe buttons must
  appear. Before WP4 the check ran against the string `"b'[COP] Something'"`, so it
  worked by accident; make sure it still works now that the name is decoded properly.
- **Gearbox calibration file**: calibrate a car, then check
  `data/gearbox_calibrations.json`. The key must be the plain car code (`XFG`). An
  entry written by an older build uses the same key, so old calibrations keep working —
  confirm one does.
- **AI traffic adoption**: start AI traffic with a human on track whose name contains
  "AI" (e.g. MAIK). That car must **not** be taken over by the traffic controller any
  more, while the real AI cars still are.
- **A modded car**: drive a vehicle mod. Nothing may raise; PDC falls back to the
  default size as before.

### WP5 — screen context and UI lifecycle

`tests/test_screen_context.py` drives the state machine from fake `IS_STA`/`IS_CIM`/
`IS_BFN` packets, but only the game shows what LFS actually sends.

- **Main menu and the multiplayer server list**: start the app there, or leave a race
  back to the main menu. **No PACT button may appear** — in particular the
  "PACT Driving Assist Active." banner, which is now restricted to the single-player
  entry screen.
- **Entry screen**: the banner must still appear there, at the bottom left as before.
- **Garage / options / car select / track select**: `IS_CIM` should be arriving. Check
  the log at debug level for `IS_CIM: Mode=…`. In the garage our buttons stay; in
  options and car/track select LFS hides them and the app now stops sending them.
- **SHIFT+B then SHIFT+I**: the HUD, the menu, the PDC display and the notification line
  must all come back — the menu is the one that used to stay away until the next state
  change.
- **Level-2 collision warning**: it must now actually flash. Level 2 never flashed
  before. The rate is a 0.25 s toggle (about 2 Hz) driven by a timer, so it no longer
  changes with `ui_refresh_rate` — confirm it looks right and is not too fast or slow.
- **The rpm readout**: it turns red from LFS's own shift light instead of "highest rpm
  ever seen". Rev a car with a shift light: red must appear at the same point the
  in-car shift light does, and must go away again. A car without a shift light shows
  no red at all — confirm that is acceptable.
- **HUD position**: move the HUD to each screen edge with the menu arrows. Nothing —
  PDC column, siren buttons, notification line — may leave the screen. While the HUD
  sits inside LFS's reserved rectangle the menu's "HUD Position" label is red; check
  in the garage and on the entry screen whether LFS's own menus are being pushed
  around, and decide whether the shipped default (90, 119) should move out of it.
- **Notification burst**: run the gearbox calibration, which emits several messages in
  a row. They must appear one at a time, and the queue caps at 8 — with more than that
  the oldest are dropped and logged. Leaving the track must clear the queue.

### WP6 — settings, migration and menu consistency

`tests/test_settings.py` and `tests/test_menu.py` cover the storage, the schema and the
menu tables, but only the game shows the menu itself and only a real installation has an
old `settings.json`.

- **An existing installation keeps its settings**: start the app with the `settings.json`
  you already have. Every switch in the menu must show the state it showed before, the
  file must gain the missing keys and a `"_version": 1`, and no key you had must be gone
  — except `park_distance_control`, which is now derived from
  `park_distance_control_mode`.
- **PDC out of the box**: delete `settings.json`, start, drive slowly towards a wall.
  The PDC column must now actually appear (default mode 1 = visual). Before, the menu
  said PDC was on and nothing was ever drawn. Decide whether the shipped default should
  be 2 (visual + audio) instead.
- **The PDC menu**: switch PDC off and on again — the mode (Visual / Visual & Audio)
  must come back as you left it, and switching it off must make the PDC column vanish
  immediately, not at the next state change.
- **A hand-broken settings file**: set `"assistance_refresh_rate": 0` by hand. The app
  must start, log a clamp warning, and run at 50 ms — not freeze. Put `{ garbage` in the
  file: the app must start on defaults and leave the old content in
  `settings.json.corrupt`.
- **Menu clicks no longer write per click**: click the HUD arrows quickly ten times.
  There must be no stutter in LFS, and about half a second after the last click the file
  must hold the final position. Close the app right after a click — the click must still
  be in the file.
- **Language**: switch the language in the menu; every label must change immediately, as
  before. The menu now reads the setting live, so if a language is ever changed from
  somewhere else it must follow without reopening the menu.
- **Own control mode**: set your input device in LFS, then change your player name or
  car. `own_control_mode` in `settings.json` must **not** change any more.
- **Nothing lost with `sat_nav` gone**: `NavigationSystem` is no longer constructed. It
  never ran (no settings key), so nothing should look different — confirm no menu entry,
  notification or HUD element disappeared.

### WP8 — blind spot, cross traffic and PDC

`tests/test_blind_spot.py`, `tests/test_cross_traffic.py` and `tests/test_pdc.py`
compute every expectation by hand, but the zones, the sound and the layout data only
exist in the game.

- **The blind spot that never warned**: get a car to sit beside you at exactly your
  speed, one lane over, roughly level with your rear bumper. The blind-spot arrow must
  light up now — this was the case the old condition could never trigger.
- **How far back the warning reaches**: the corridor is 85 m long. Let a car close on
  you from far behind in the next lane. The warning should come roughly 3.5 s before it
  reaches you, and a car cruising 60 m back at your speed must stay silent. If the
  warning still feels too early, the corridor length (`_CORRIDOR_MULTIPLIERS`, 85 m) is
  the number to shorten — that is a product decision, not a bug.
- **Left really is left**: drive with traffic on both sides and confirm the arrow side
  matches. The other car's outline was mirrored for every heading below 16384 (roughly
  north through west), so this used to be wrong in one quadrant.
- **Frame rate with traffic**: with ~40 cars, BSW now builds shapely polygons only for
  cars that pass three comparisons. Watch for an improvement, never a regression.
- **Cross traffic in neutral**: roll up to a junction in neutral (or coast with the
  clutch in). The warning must now appear — the old `gear <= 1` gate switched the whole
  system off. Reversing must still suppress it.
- **A long vehicle crossing slowly**: in a layout with AI traffic, let a long car cross
  in front of you at walking pace. It must now raise a warning; the old fixed ±0.5 s
  arrival tolerance treated every vehicle as a point and missed it.
- **PDC after deleting a layout object**: in the layout editor, place several objects
  close together, then delete one. Only that object may disappear from the PDC — before,
  the id collision could evict a different one, leaving an obstacle the sensors no
  longer see. Reversing into the remaining objects must still beep.
- **PDC object positions**: park next to armco or a post and check that the sensor rings
  light at the right distance. The AXM→MCI factor of 4096 was marked with a `TODO`; it is
  correct per `InSim.txt`, but only the game shows whether the objects sit where the
  sensors expect them.
- **The beep**: reverse quickly towards a wall so the pattern goes 1 → 2 → 3. The tone
  must speed up smoothly and stop the moment PDC switches off or you exceed 10 km/h. It
  is now one thread; listen for stuck or overlapping tones.

### WP7 — forward collision warning

`tests/test_collision_warning.py` computes every expectation by hand, but the wedge, the
warning levels and the acceleration input only meet reality in the game.

- **The heading sector that used to be dead**: drive a long straight and note the
  compass direction where FCW used to go quiet (heading near 0/65535, i.e. due north on
  most tracks). Approach a stopped car on that heading — the warning must now come
  exactly as it does on any other heading.
- **Reversing still suppresses the warning**: reverse towards a car. No forward
  collision warning may appear.
- **No warning when the car ahead pulls away**: follow a car that accelerates harder
  than you. There must be no warning at all — that case used to be able to raise one.
- **The warning falls again**: approach a slower car until level 3 (red), then lift off
  and let the gap open. The warning must step down 3 → 2 → 1 → off as the situation
  relaxes. Before, it stayed at 3 until the required braking hit exactly zero.
- **Refresh rate does not change the warning any more**: set
  `assistance_refresh_rate` to 50 and then to 200 and repeat the same approach at the
  same speed. The warning must come at roughly the same distance both times. Before, the
  acceleration input was scaled by 2× in each direction.
- **A modded car ahead**: approach a vehicle mod. The warning must come slightly earlier
  than for a standard car of the same length, never later — its length falls back to
  5.0 m.
- **Frame rate on a busy track**: with ~40 cars, FCW now rejects almost all of them with
  two number comparisons before building a polygon. Watch for a small improvement, never
  a regression.
- **The debug readout is gone**: `dist_debug` is no longer emitted. Its subscriber was
  already commented out, so button 101 was never drawn — confirm nothing on screen
  changed.

### WP9 — actuation: key injection and light commands

`tests/test_actuation.py` drives the guard table, the calibration and the light dedupe,
but only the game has a foreground window, a real keyboard binding and real car lights.

- **The foreground check**: start the app, drive, then alt-tab to a browser while the
  car is stopped with the brake held. **Nothing may be typed into the browser.** Back in
  LFS, auto-hold must engage again. This is the one check that decides whether the
  window-title / process test (`Live for Speed`, `LFS*`) recognises your LFS build at
  all — if auto-hold and the gearbox stop working entirely, it does not, and
  `misc/input_guard.py::lfs_has_focus` needs the real title. Log level `DEBUG` shows
  `Key injection refused: lfs_not_focused`.
- **Chat and dialogs**: press T and type while stopped on the brake, and open the ESC
  menu. No handbrake key and no shift may be injected; the chat line must stay clean.
- **Shift held**: hold SHIFT (e.g. before SHIFT+U) while the automatic gearbox would
  shift. No shift may be injected. This reads `OutGaugePack.Flags & OG_SHIFT`, which
  only the game produces — confirm it really arrives (debug log, `modifier_held`).
- **TAB while stopped on the brake**: with the camera on another car, auto-hold must not
  press anything; back on your own car it must engage.
- **Key rebinding without a restart**: rebind the handbrake and the shift keys in the
  menu and use them immediately. Both must take effect at once — the gearbox used to
  need a restart.
- **Gearbox calibration**: run it. Each step must announce itself and count down at 6 s
  and 3 s; pressing the calibration entry again must cancel; finishing in neutral must
  be refused instead of storing a gearbox that never shifts. Check the notification
  queue keeps up (8 entries, 3 s each).
- **An old calibration keeps working**: with a `data/gearbox_calibrations.json` written
  by an older build (`max_gears`), the car must shift exactly as before; after a fresh
  calibration the file holds `forward_gears` = the number of forward gears, and the
  "Max gear set to" message shows the same number.
- **Driving without lights is possible again**: with `high_beam_assist` on and all
  lights off, drive at night. The app must **not** switch the low beam on. Switch the
  low beam on by hand: the assist must then raise the high beam when nothing is ahead
  and dip it when a car appears — one change at a time, no flicker. Dip by hand
  afterwards: it must stay dipped until the traffic situation changes.
- **Light packet rate**: the light commands are now sent only on change. Watch for any
  light that gets *stuck* (e.g. high beam staying on after a car appears) — that would
  mean the OutGauge light flags do not report what we assume (does LFS set `DL_DIPPED`
  as well as `DL_FULLBEAM` on high beam?). This is the one assumption in the dedupe.
- **Strobe speed**: as a `[COP]`, switch the strobe on and set `assistance_refresh_rate`
  to 50 and then to 200. The blink pattern must look the same at 50 and 100 ms; at
  200 ms it can only be half as fast (the cycle is the ceiling). Decide whether 0.1 s
  per step is the right speed now that it is no longer tied to the refresh rate.
- **Turning the siren off no longer switches your lights on**: with the siren/strobe on,
  switch it off. Fog, extra and hazard lights go out, and the head lights must stay
  exactly as you had them.
- **Siren caption**: toggle the siren with the button, then with `$siren` in chat, then
  again with the button. The caption must follow every time (`^4Siren` when on), and the
  real LFS siren must match it.
- **Adaptive brake light heading sector**: brake hard on a heading near north
  (0/65535). The hazards must flash there too — the old subtraction treated that sector
  as reversing. Reversing hard must still not flash them.
- **Non-cop players get no light commands**: with `cop_assistance` on but a plain player
  name, join a track and change your name. No light may change by itself.
