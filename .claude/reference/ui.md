# UI, settings and localisation

Everything visible is an InSim button. Mechanics and styles: `insim.md` §4.

## 1. Screen context — where buttons may appear, and when input must be blocked

LFS is not one screen. Buttons behave differently on each, and injected keypresses are
dangerous on several. **Determining the current context correctly is a prerequisite for
both, and the project currently does it only crudely.**

### 1.1 The contexts

| Context | `IS_STA` signature | Buttons | Notes |
|---|---|---|---|
| Main menu / multiplayer server list | neither `ISS_GAME` nor `ISS_FRONT_END` | **must not be drawn** | LFS shows none of our buttons here anyway; drawing is pointless and clutters the button set |
| Single-player **entry screen** | `ISS_FRONT_END` (256) | **should be drawn** | see the space-clearing quirk in §1.3 |
| **Pit / garage view** | `ISS_GAME`, plus `IS_CIM` `Mode = CIM_GARAGE` (3) | **should be drawn** | many submodes — `IS_CIM.SubMode` distinguishes them (`GRG_INFO`, `GRG_COLOURS`, `GRG_BRAKE_TC`, `GRG_SUSP`, `GRG_STEER`, `GRG_DRIVE`, `GRG_TYRES`, `GRG_AERO`, `GRG_PASS`) |
| **In game / on track** | `ISS_GAME` and not `ISS_FRONT_END` | **always drawn** | the normal case |
| ESC menu / any dialog | `ISS_DIALOG` (16) | LFS hides them | our buttons disappear on their own; do not fight it |
| Text entry (chat) | `ISS_TEXT_ENTRY` (32768) | LFS hides them | **and keyboard injection must stop — see §1.4** |
| SHIFT+U free view | `ISS_SHIFTU` (8) | drawn | `IS_CIM.SubMode` = `FVM_PLAIN` (no buttons), `FVM_BUTTONS`, `FVM_EDIT` |
| Options / host options / car select / track select | `IS_CIM` `Mode` 1 / 2 / 4 / 5 | normal buttons hidden | only `INST_ALWAYS_ON` buttons survive here |

Per `InSim.txt`, LFS displays normal buttons in exactly four screens: **main entry
screen, race setup screen, in game, and SHIFT+U mode.**

### 1.2 `ISS_VISIBLE` is the authoritative answer

`IS_STA.Flags & ISS_VISIBLE` (16384) means *"InSim buttons are visible right now"*.
`StateHandler` publishes it as `state_data['ui_visible']`.

**What the UI should actually branch on is `state_data['buttons_allowed']`**, not
`ui_visible`: it is derived from the screen context and is false exactly where LFS
shows no normal buttons (main menu / server list and the options / car-select /
track-select screens). `ui_visible` is kept as raw information, but gating drawing on
it would blank the whole UI the moment LFS reports it for a reason we did not predict.

`StateHandler` (`lfs/lfs_state.py`) derives, from `IS_STA` **and** `IS_CIM`:

| Field | Source |
|---|---|
| `on_track` | `ISS_GAME` and not `ISS_FRONT_END` |
| `text_entry` / `dialog` | `ISS_TEXT_ENTRY` / `ISS_DIALOG` |
| `ui_visible` / `shift_u` / `multiplayer` | `ISS_VISIBLE` / `ISS_SHIFTU` / `ISS_MULTI` |
| `in_game_interface` / `submode_interface` / `select_type` | `IS_CIM.Mode` / `.SubMode` / `.SelType` |
| `screen` | derived — one of `main_menu`, `entry`, `garage`, `options`, `shiftu`, `game` |
| `buttons_allowed` | `screen` is neither `main_menu` nor `options` |

`IS_CIM` needs no `ISF_*` flag: LFS sends it whenever the local connection's interface
mode changes. It is bound in `LFSConnector._handle_interface_mode` and republished as
`interface_mode_changed`; `StateHandler` re-emits `state_data` on it, so `state_data`
now has **two** sources. A CIM packet arriving before the first `IS_STA` is stored but
not published — publishing a screen context from flags we have never seen would be a
guess.

### 1.3 The entry-screen space-clearing quirk

`InSim.txt` defines a recommended button area:

```
IS_X_MIN 0    IS_X_MAX 110
IS_Y_MIN 30   IS_Y_MAX 170
```

**Buttons inside this area cause LFS to keep the area clear** — it moves or hides its
own UI so the two do not overlap. Buttons *outside* the area get no space reserved and
simply overlap whatever LFS is drawing.

That is the "LFS UI disappears depending on button position" behaviour seen on the entry
screen: it is intentional (it lets an InSim program own the screen), but it makes LFS's
own menus unusable if our HUD happens to sit in that rectangle. Since `hud_width` /
`hud_height` are user-configurable, a user can drag the HUD into the reserved area and
break the entry screen without understanding why.

Practical rules:
- Keep persistent HUD elements **outside** `0–110 × 30–170` unless the intent really is
  to take over the screen. `ui/ui_manager.py` exposes `hud_overlaps_reserved_area()`
  for this; the system menu's "HUD Position" label turns `^1` red while the HUD sits
  inside the rectangle. It is deliberately **not** clamped out of it — the shipped
  default (90, 119) is inside, so enforcing it would relocate every existing user's
  HUD (`known-issues.md` #27).
- `clamp_hud_position()` **is** enforced: it keeps the whole block — HUD, PDC column,
  siren buttons and notification line, i.e. `x-3 … x+29` by `y-6 … y+13` — inside
  `0…200`. Every draw site goes through `UIManager.hud_origin()`, so a hand-edited
  `settings.json` cannot push anything off screen.
- The menu (IDs 20–40, drawn at x 0–50, y 70–120) sits **inside** the reserved area —
  that is correct for a menu, and is why it is drawn only while on track.
- `INST_ALWAYS_ON` (128, the `Inst` byte of `IS_BTN`) makes a button visible in *all*
  screens including garage and options. `LFSConnector.send_button()` already accepts an
  `inst` argument but nothing passes it. Use it sparingly and only at the screen edges,
  as `InSim.txt` warns.

### 1.4 Input injection safety — hard rules

`AutoHold` and `Gearbox` inject global keypresses with `pyautogui`. These are real OS
keystrokes: they go wherever focus is. **Every such call site goes through
`misc/input_guard.py`** — `InputGuard.may_inject(own_vehicle)` returns `None` when the
keystroke may be sent and a reason string when it may not:

| Condition | Reason | Source | Why |
|---|---|---|---|
| not `on_track` | `off_track` | `state_data['on_track']` | no control input is meaningful |
| `ISS_TEXT_ENTRY` is set | `text_entry` | `state_data['text_entry']` | the keystroke is typed into the LFS chat instead of acting as a control |
| `ISS_DIALOG` is set | `dialog` | `state_data['dialog']` | the keystroke operates the open dialog |
| the user is holding **Shift** or **Ctrl** | `modifier_held` | `OutGaugePack.Flags & OG_SHIFT` (1) / `OG_CTRL` (2) | LFS binds many SHIFT+key shortcuts (SHIFT+B / SHIFT+I buttons, SHIFT+U free view, …). An injected key while Shift is held becomes a command |
| OutGauge describes another car | `not_local_driver` | `own_vehicle.is_local_driver` | TAB moves OutGauge to a spectated car; shifting on its rpm actuates *our* car (`conventions.md` §5.2) |
| LFS drives the car itself | `ai_controlled` | `own_vehicle.data.is_ai` | our keys would fight the AI |
| LFS is not the foreground window | `lfs_not_focused` | Win32 `GetForegroundWindow` | otherwise we type into the user's browser |

Rules that shaped the implementation:

- **The guard is asked only where a key would really be pressed** — once per auto-hold
  engagement, once per gear change — never once per cycle. The conditions are ordered
  cheapest first; the Win32 call is last.
- **A refusal is logged at debug level, once per reason per 30 s.** It is normal
  operation, not an error, and must never become one message per cycle.
- **The foreground check fails *open*.** Off Windows, and on any Win32 error, it returns
  `True`: refusing because we could not ask would silently kill both features. What it
  accepts is decided by `looks_like_lfs(title, process_name)`: the **process** is
  authoritative (stem `lfs` / `lfs_dbg`, i.e. `LFS.exe`), and the window title is only a
  fallback for a renamed executable — and then it must contain the full `live for
  speed`. A bare `lfs` title substring is deliberately *not* enough: it matches a browser
  tab on the LFS forum and a file manager in a folder called LFS, which are exactly the
  windows the check exists to keep keystrokes out of.
- **A stale Shift reading does not block.** OutGauge only streams on track in an
  internal view (`conventions.md` §5.3) — exactly when injection is allowed at all — so
  the flags are fresh whenever they matter; a reading older than 1 s is treated as
  unknown rather than as "held", or one lost packet would disable the feature.
  `misc/key_binder.py` already depends on `pynput` and is the natural place for a local
  fallback listener if a broader guarantee is ever wanted. (`IS_BTC.CFlags` also has
  `ISB_SHIFT`/`ISB_CTRL`, but only for button clicks.)
- **The input mode is deliberately not a condition.** `own_control_mode` /
  `vehicle.data.control_mode` (mouse / keyboard / joystick) says nothing about whether
  the *keyboard* binding works — a wheel user still has one. Gating on it would remove
  the feature for those users.
- **Key settings are read live**, at the moment of the keypress. A key rebound in the
  menu takes effect immediately; `Gearbox` used to cache them in `__init__`, so a rebind
  only arrived after a restart.

Also note: whichever key the injector presses must be the key **the user has actually
bound in LFS**. The `user_*_key` settings are the app's guess at that binding and
nothing verifies it — a wrong binding means the injection does nothing, or does
something else entirely. `/key <key> <function>` would let us push our binding *into*
LFS instead of guessing; see `control-intervention.md` §3.1.

### 1.5 The user can clear our buttons — and we never notice

`IS_BFN` is bidirectional. Two subtypes arrive **from** LFS, both bound:

- `BFN_USER_CLEAR` (2) — the user pressed SHIFT+B and cleared this InSim instance's
  buttons.
- `BFN_REQUEST` (3) — the user pressed SHIFT+B / SHIFT+I asking for buttons back.

Both are republished as `buttons_cleared`. `MessageSender` drops its registry (so the
next repaint really sends), `UIManager` redraws the idle banner and `MenuSystem`
redraws the menu page that is open. Everything else `UIManager` owns — HUD, PDC, siren
buttons, the notification line — is repainted every UI pass and comes back on its own;
the registry makes those repeats free on the wire. **A new UI element that only draws
on change must subscribe to `buttons_cleared` itself.**

Conversely, `BFN_CLEAR` (1) sent **to** LFS clears every button this instance created in
a single packet — the right replacement for the current 239-packet delete loop
(`known-issues.md` #10).

## 2. Button ID allocation — respect this map

IDs are global and collide silently. `UIManager` owns the authoritative comment block;
this is the same map:

The constants live at the top of `ui/ui_manager.py` (`BTN_*`, `*_RANGE`) — import them
rather than writing literals.

| Range | Owner | Contents |
|---|---|---|
| 1–10 | `UIManager` | HUD: `1` speed, `2` rpm, `3` gear, `4` the "PACT Driving Assist Active." idle banner (its own slot since WP5 — it used to share id `1` with the speed field) |
| 11–12 | `UIManager` | Forward collision warning *(reserved; the warning currently repaints the HUD instead)* |
| 13–14 | `UIManager` | Blind spot warning: `13` left, `14` right |
| 20–40 | `MenuSystem` | `20` floating "Main Menu" opener, `21` title, `22`–`31` entries, `40` close/cancel |
| 41–60 | `UIManager` | PDC display: `41–43`/`44–46`/`47–49` front green/yellow/red, `51–53`/`54–56`/`57–59` rear, `60` "PDC" label |
| 61 | `UIManager` | Notification line |
| 62–63 | `LightAssists` (state) + `UIManager` (drawing) | `62` Siren, `63` Strobe (cop mode) |
| 100–101 | `UIManager` | Debug readouts (deceleration, distance) — subscribers commented out |

When adding UI, claim a free range here and update both this table and the comment in
`ui/ui_manager.py`.

**Buttons 62/63 have exactly one click handler**: `LightAssists._handle_button_click`.
It owns the siren/strobe state and publishes `siren_state_changed` /
`strobe_state_changed`; `UIManager` subscribes to those and only repaints the caption.
The button ids are therefore spelled out in both files (`BTN_SIREN` / `BTN_STROBE`) and
must stay in step. Before WP9 both classes kept their own boolean off the same
`button_clicked` event, so the `$siren` chat command moved one of them and the caption
lied.

## 3. HUD and warnings — `ui/ui_manager.py`

- `update_hud()` runs every `ui_refresh_rate` ms (default 50) and only draws while
  `hud_active` and `on_track`.
- Position comes from settings `hud_width` (**horizontal / L**) and `hud_height`
  (**vertical / T**) — the names are misleading, they are coordinates, not sizes.
  Everything else (PDC, notifications, siren buttons) is positioned relative to them.
- Position always comes from `UIManager.hud_origin()`, which clamps (§1.3).
- Warning presentation is a repaint of the HUD itself: FCW replaces speed/rpm with
  `^1- - -` and alternates `ISB_DARK`/`ISB_LIGHT` at level ≥ 2 to flash. CTW does the
  same with `^1< < <` / `^1> > >`, but only when FCW is not active (FCW has priority).
- **The blink phase has one owner**: `UIManager._advance_blink()`, a
  `WARNING_BLINK_INTERVAL_S` (0.25 s) timer. It used to be a toggle-per-call, so the
  rate depended on `ui_refresh_rate` and level 2 never flashed at all (the same pass
  toggled the colour and then reset it).
- **The rpm readout turns red from LFS's own shift light** (`OutGauge.ShowLights &
  DL_SHIFT`), not from a "highest rpm ever seen" heuristic. That is per car, correct
  for mods, and recovers after a car change. A car without a shift light simply never
  shows red.
- Leaving the track (`state_data.on_track == False`) removes every live button via the
  registry and shows the idle banner — but **only on the entry screen**; the main menu
  and the server list get nothing (§1.1).
- Notifications: `notification` events queue in a `deque(maxlen=MAX_QUEUED_NOTIFICATIONS)`
  (8); one is shown for 3 s on ID 61 and the queue is cleared on track exit. Overflow
  drops the oldest and logs a warning.
- Audio: `UIManager` emits `play_audio` (three times, to lengthen the tone) when a
  warning crosses level 2. `AudioPlayer` suppresses repeats of `fcw` within 3 s.

## 4. Menus — `ui/menu_system.py`

A flat state machine: `current_menu ∈ {'none','main','driving','parking','system','cop','ai_traffic','keys','await_key'}`.
Each menu has **two halves that must agree**:

- `_buttons_<name>()` returns its button list (`buttons_for(name)` is the public
  accessor), and `open_<name>()` clears IDs 20–40 and draws it;
- `_actions[<name>]` maps button ID → action (`actions_for(name)`).

`_handle_menu_click` looks the ID up in the active menu's table only, so an ID can
never be interpreted by a menu that does not draw it, and a new entry cannot silently
shadow an existing one. IDs `20` (open) and `40` (close/cancel) belong to no menu and
must not appear in any table. `tests/test_menu.py` checks both directions for every
menu.

Toggling a setting immediately re-opens the same menu so the `^1`/`^2` colour prefix
reflects the new state — that is the established pattern, keep it. `_toggle` and
`_cycle` do exactly that.

The menu reads `language` live through the `MenuSystem.language` property. It used to
cache the value at construction, so a language change from anywhere but the menu itself
never reached it.

Adding a menu entry:
1. Pick a free ID in 21–39 within that menu's `_buttons_*` list.
2. Add it to that menu's entry in `_build_actions`.
3. Add the label to `misc/language.py` for **all 8 languages**.
4. If it maps to a setting, add the `Setting` entry in `SettingsManager._SCHEMA`.

Key rebinding: menu emits `await_keybinding` → `Keybinder` starts `pynput` listeners in
a thread → first key/click emits `new_keybinding` → `MenuSystem._rebind_key` persists
it. The left mouse button needs two presses (the first is consumed by the click that
opened the prompt).

## 5. Settings — `core/settings_manager.py`

`settings.json` next to the executable/project root (`misc.helpers.resolve_path`).
`SettingsManager._SCHEMA` is the **authoritative list of every setting key**, with its
default, type and allowed range; `_defaults` is derived from it and `known_keys` adds
the derived keys. A key that is in neither cannot be enabled — that is why `sat_nav`
never ran.

Rules the rest of the app can rely on:

- **`get(key)` always answers for a known key**: stored value, else schema default. The
  optional `default` argument only applies to keys the schema does not know — it used
  to shadow the built-in default, which is why every newly added system stayed off for
  users with an older `settings.json`.
- **Defaults are merged in on load**, unknown keys from a newer version are kept, and
  the file carries a `_version` (`SETTINGS_VERSION`) with a migration hook in
  `_migrate`.
- **Values are validated and repaired** against the schema (type, range, allowed
  values) with a logged warning. A hand-edited `assistance_refresh_rate: 0` is clamped
  to 50 instead of busy-looping the app. An unreadable file is moved aside as
  `settings.json.corrupt` rather than being silently overwritten.
- **Writes are debounced** (`SAVE_DEBOUNCE_S`, 0.5 s) and atomic (tmp + `os.replace`).
  `set()` therefore no longer does blocking file I/O on the packet thread. `flush()`
  forces a pending write out; the debounce timer is deliberately non-daemon so the last
  menu click still lands on disk at shutdown.
- **`park_distance_control` is derived, not stored.** The single stored value is
  `park_distance_control_mode` (0 off / 1 visual / 2 visual+audio); the boolean is
  `mode != 0`, and setting it moves the mode. Two values for one concept could
  contradict each other, and the shipped default did exactly that: the switch said on
  while the mode said off, so PDC computed every cycle and displayed nothing.

Groups:

| Group | Keys |
|---|---|
| Assistance toggles | `forward_collision_warning`, `blind_spot_warning`, `cross_traffic_warning`, `automatic_gearbox`, `auto_hold`, `adaptive_lights`, `high_beam_assist`, `cop_assistance`, `ai_traffic` (+ `park_distance_control`, **derived**) |
| Thresholds/modes | `collision_warning_distance` (0 early/1 normal/2 late), `cross_traffic_warning_distance` (same), `automatic_emergency_brake` (0 off/1 warn/2 warn+brake — **inert**), `park_distance_control_mode` (0 off/1 visual/2 visual+audio), `parking_emergency_brake` |
| Presentation | `language` (`de` default), `unit` (`metric`/`imperial`), `hud_active`, `hud_height`, `hud_width` |
| Timing | `ui_refresh_rate` (50), `assistance_refresh_rate` (100 — **also becomes the InSim MCI `Interval`**) |
| Input | `user_handbrake_key`, `user_shift_up_key`, `user_shift_down_key`, `user_clutch_key`, `user_ignition_key`, `user_brake_key`, `user_axis_*`, `vjoy_axis_1`, `own_control_mode` (0 mouse/1 keyboard/2 joystick) |

`own_control_mode` is the **user's** choice. It is no longer overwritten from the
control mode LFS reports in `IS_NPL` — that value is available separately as
`vehicle.data.control_mode` and in the `player_name_changed` payload
(`conventions.md` §5.4).

`assistance_refresh_rate` is read once at startup for both the scheduled task and the
InSim interval; changing it at runtime has no effect on either. It no longer
invalidates the acceleration calculation — that is measured from the real packet
interval since WP7 (`conventions.md` §3).

## 6. Localisation — `misc/language.py`

`LanguageManager.get(key, lang)` with an English key, falling back to English.
Supported: `en, de, it, fr, tr, no, dk, se`.

- **Never hardcode user-facing text.** Every menu label, notification and tooltip goes
  through the translator.
- When adding a key, fill in all eight languages. Machine-quality translations are
  acceptable; an empty entry breaks the UI.
- Notification strings may carry an LFS colour prefix, applied *outside* the
  translation (`'^1' + translator.get(...)`), so the tables stay colour-free.
- LFS renders `latin-1`; `LFSConnector.send_button` encodes as `latin-1` with a UTF-8
  fallback. Non-latin-1 characters in translations will not render correctly.

The `$help` command text in `assistance/chat_commands.py` is currently **English-only
and hardcoded** — it should be routed through the translator eventually.
