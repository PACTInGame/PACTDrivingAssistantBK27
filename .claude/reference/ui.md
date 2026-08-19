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
It is the single reliable signal for whether our UI is actually on screen, and it is
**not currently read anywhere in the project**. Prefer it over inferring visibility from
`ISS_GAME`/`ISS_FRONT_END`/`ISS_DIALOG` combinations.

`lfs/lfs_state.py` currently derives only `on_track = ISS_GAME and not ISS_FRONT_END`,
plus `text_entry` and `dialog`. Its `in_game_interface` and `submode_interface` fields
are placeholders that are **initialised to 0 and never updated** — they were clearly
meant for `IS_CIM`. Wiring `ISP_CIM` in `LFSConnector` and filling those two fields
(then widening the `state_data` payload) is the intended fix.

`IS_CIM` needs no `ISF_*` flag: LFS sends it whenever the local connection's interface
mode changes. pyinsim already has the `IS_CIM` class and every `CIM_*` / `NRM_*` /
`GRG_*` / `FVM_*` constant — only the binding is missing.

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
  to take over the screen.
- The menu (IDs 20–40, drawn at x 0–50, y 70–120) sits **inside** the reserved area —
  that is correct for a menu, and is why it is drawn only while on track.
- `INST_ALWAYS_ON` (128, the `Inst` byte of `IS_BTN`) makes a button visible in *all*
  screens including garage and options. `LFSConnector.send_button()` already accepts an
  `inst` argument but nothing passes it. Use it sparingly and only at the screen edges,
  as `InSim.txt` warns.

### 1.4 Input injection safety — hard rules

`AutoHold` and `Gearbox` inject global keypresses with `pyautogui`. These are real OS
keystrokes: they go wherever focus is. Any code path that injects a key **must** be
blocked when:

| Condition | Source | Why |
|---|---|---|
| `ISS_TEXT_ENTRY` is set | `state_data['text_entry']` | the keystroke is typed into the LFS chat instead of acting as a control |
| `ISS_DIALOG` is set | `state_data['dialog']` | the keystroke operates the open dialog |
| the user is holding **Shift** | **not available via InSim** — must be tracked locally with `pynput` | LFS binds many SHIFT+key shortcuts (SHIFT+B / SHIFT+I buttons, SHIFT+U free view, …). An injected key while Shift is held becomes a command |
| LFS is not the foreground window | Win32 / `pygetwindow` | otherwise we type into the user's browser |
| not `on_track` | `state_data['on_track']` | no control input is meaningful |

`AutoHold` currently checks `dialog` and `text_entry` only. `Gearbox` checks **nothing**.
Neither checks Shift or focus. See `known-issues.md` #11.

There is no InSim packet reporting modifier-key state — `IS_BTC.CFlags` carries
`ISB_SHIFT`/`ISB_CTRL` but only for button clicks. Global Shift state must come from a
local `pynput` listener; `misc/key_binder.py` already depends on `pynput` and is the
natural place to expose it.

### 1.5 The user can clear our buttons — and we never notice

`IS_BFN` is bidirectional. Two subtypes arrive **from** LFS and neither is bound:

- `BFN_USER_CLEAR` (2) — the user pressed SHIFT+B and cleared this InSim instance's
  buttons. Our UI is gone until something redraws it.
- `BFN_REQUEST` (3) — the user pressed SHIFT+B / SHIFT+I asking for buttons back. This
  is the signal to redraw everything.

Because the HUD is repainted every 50 ms it recovers by itself, but the menu, PDC and
notification buttons do not. Binding `ISP_BFN` and forcing a full redraw on both
subtypes is the correct handling.

Conversely, `BFN_CLEAR` (1) sent **to** LFS clears every button this instance created in
a single packet — the right replacement for the current 239-packet delete loop
(`known-issues.md` #10).

## 2. Button ID allocation — respect this map

IDs are global and collide silently. `UIManager` owns the authoritative comment block;
this is the same map:

| Range | Owner | Contents |
|---|---|---|
| 1–10 | `UIManager` | HUD: `1` speed, `2` rpm, `3` gear. Also `1` is reused for the "PACT Driving Assist Active" banner while **off** track. |
| 11–12 | `UIManager` | Forward collision warning *(reserved; the warning currently repaints the HUD instead)* |
| 13–14 | `UIManager` | Blind spot warning: `13` left, `14` right |
| 20–40 | `MenuSystem` | `20` floating "Main Menu" opener, `21` title, `22`–`31` entries, `40` close/cancel |
| 41–60 | `UIManager` | PDC display: `41–43`/`44–46`/`47–49` front green/yellow/red, `51–53`/`54–56`/`57–59` rear, `60` "PDC" label |
| 61 | `UIManager` | Notification line |
| 62–63 | `UIManager` + `LightAssists` | `62` Siren, `63` Strobe (cop mode) |
| 100–101 | `UIManager` | Debug readouts (deceleration, distance) — subscribers commented out |

When adding UI, claim a free range here and update both this table and the comment in
`ui/ui_manager.py`.

**Buttons 62/63 have two independent click handlers** — `UIManager._handle_button_click`
tracks the visual state and `LightAssists._handle_button_click` performs the action.
Both subscribe to `button_clicked`, so their two booleans can drift apart.

## 3. HUD and warnings — `ui/ui_manager.py`

- `update_hud()` runs every `ui_refresh_rate` ms (default 50) and only draws while
  `hud_active` and `on_track`.
- Position comes from settings `hud_width` (**horizontal / L**) and `hud_height`
  (**vertical / T**) — the names are misleading, they are coordinates, not sizes.
  Everything else (PDC, notifications, siren buttons) is positioned relative to them.
- Warning presentation is a repaint of the HUD itself: FCW replaces speed/rpm with
  `^1- - -` and alternates `ISB_DARK`/`ISB_LIGHT` at level ≥ 2 to flash. CTW does the
  same with `^1< < <` / `^1> > >`, but only when FCW is not active (FCW has priority).
- Leaving the track (`state_data.on_track == False`) deletes button IDs `0…238` in one
  burst and shows the idle banner. That is 239 packets — see `known-issues.md` #10.
- Notifications: `notification` events queue in a list; one is shown for 3 s on ID 61.
  A burst therefore drains at 3 s each.
- Audio: `UIManager` emits `play_audio` (three times, to lengthen the tone) when a
  warning crosses level 2. `AudioPlayer` suppresses repeats of `fcw` within 3 s.

## 4. Menus — `ui/menu_system.py`

A flat state machine: `current_menu ∈ {'none','main','driving','parking','system','cop','ai_traffic','keys','await_key'}`.
Each `open_*` method clears IDs 20–40 and redraws its own button list. Button `40`
closes (from `main`) or returns to `main`.

Toggling a setting immediately re-opens the same menu so the `^1`/`^2` colour prefix
reflects the new state — that is the established pattern, keep it.

Adding a menu entry:
1. Pick a free ID in 21–39 within that menu's `open_*` list.
2. Handle it in `_handle_menu_click` under the matching `current_menu` branch.
3. Add the label to `misc/language.py` for **all 8 languages**.
4. If it maps to a setting, add the default in `SettingsManager._defaults`.

Key rebinding: menu emits `await_keybinding` → `Keybinder` starts `pynput` listeners in
a thread → first key/click emits `new_keybinding` → `MenuSystem._rebind_key` persists
it. The left mouse button needs two presses (the first is consumed by the click that
opened the prompt).

## 5. Settings — `core/settings_manager.py`

`settings.json` next to the executable/project root (`misc.helpers.resolve_path`).
`SettingsManager._defaults` is the **authoritative list of every setting key**; a key
missing there cannot be enabled (this is why `sat_nav` never runs).

Groups:

| Group | Keys |
|---|---|
| Assistance toggles | `forward_collision_warning`, `blind_spot_warning`, `cross_traffic_warning`, `automatic_gearbox`, `auto_hold`, `adaptive_lights`, `high_beam_assist`, `park_distance_control`, `cop_assistance`, `ai_traffic` |
| Thresholds/modes | `collision_warning_distance` (0 early/1 normal/2 late), `cross_traffic_warning_distance` (same), `automatic_emergency_brake` (0 off/1 warn/2 warn+brake — **inert**), `park_distance_control_mode` (0 off/1 visual/2 visual+audio), `parking_emergency_brake` |
| Presentation | `language` (`de` default), `unit` (`metric`/`imperial`), `hud_active`, `hud_height`, `hud_width` |
| Timing | `ui_refresh_rate` (50), `assistance_refresh_rate` (100 — **also becomes the InSim MCI `Interval`**) |
| Input | `user_handbrake_key`, `user_shift_up_key`, `user_shift_down_key`, `user_clutch_key`, `user_ignition_key`, `user_brake_key`, `user_axis_*`, `vjoy_axis_1`, `own_control_mode` (0 mouse/1 keyboard/2 joystick) |

`get(key, default)` falls back to `_defaults`, so old `settings.json` files keep
working when you add a key. `set()` writes the whole file **synchronously on every
call** and prints — fine for menu clicks, never call it from `process()`.

`assistance_refresh_rate` is read once at startup for both the scheduled task and the
InSim interval; changing it at runtime has no effect and would also invalidate the
acceleration calculation (`conventions.md` §3).

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
