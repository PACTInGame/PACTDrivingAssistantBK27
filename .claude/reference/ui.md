# UI, settings and localisation

Everything visible is an InSim button. Mechanics and styles: `insim.md` §4.

## 1. Button ID allocation — respect this map

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

## 2. HUD and warnings — `ui/ui_manager.py`

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

## 3. Menus — `ui/menu_system.py`

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

## 4. Settings — `core/settings_manager.py`

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

## 5. Localisation — `misc/language.py`

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
