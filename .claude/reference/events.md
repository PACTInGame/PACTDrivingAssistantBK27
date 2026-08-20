# Event catalog

The `EventBus` is the only interface between components. Event names are plain strings,
so a typo produces a silently dead event. **Check this table before emitting or
subscribing, and update it whenever you add, rename or change an event.**

Emission is synchronous and runs in the emitter's thread — see `architecture.md` §3.

## Packet ingress (emitted by `LFSConnector`, main thread)

| Event | Payload | Subscribers |
|---|---|---|
| `lfs_connected` | `None` | `main.LFSAssistantApp` |
| `game_state_changed` | `IS_STA` packet | `lfs.lfs_state.StateHandler` |
| `vehicle_data_received` | `IS_MCI` packet | `VehicleManager` |
| `outgauge_data` | `OutGaugePack` | `VehicleManager`, `UIManager` |
| `outsim_data` | `OutSimPack` | *(none — see known-issues #3)* |
| `player_joined` | `IS_NPL` packet | `VehicleManager` |
| `player_left` | `IS_PLL` packet | `VehicleManager` |
| `button_clicked` | `IS_BTC` packet | `UIManager`, `MenuSystem`, `LightAssists` |
| `message_received` | `IS_MSO` packet | `ChatCommandHandler` |
| `layout_received` | `IS_AXM` packet | `ParkDistanceControl` |
| `buttons_cleared` | `{sub_type: BFN_USER_CLEAR\|BFN_REQUEST}` | `MessageSender`, `UIManager`, `MenuSystem` |
| `interface_mode_changed` | `IS_CIM` packet | `lfs.lfs_state.StateHandler` |
| `AI_Controller_initialized` | `AICarController` instance | `AIDriver` |

## Derived state

| Event | Payload | Emitter | Subscribers |
|---|---|---|---|
| `state_data` | see below | `StateHandler` | `AssistanceManager`, `UIManager`, `MenuSystem`, `AutoHold`, `LightAssists`, `ChatCommandHandler`, `AIDriver` |
| `vehicles_updated` | `Dict[plid, Vehicle]` (excludes own car) — a **fresh dict per MCI frame** | `VehicleManager` | `AssistanceManager`, every `AssistanceSystem` via the base class |
| `own_vehicle_updated` | `OwnVehicle` | `VehicleManager` | `AssistanceManager`, every `AssistanceSystem` via the base class |
| `player_name_changed` | `{player_name: str (decoded), control_mode}` | `VehicleManager` | `LightAssists`, `ChatCommandHandler`, `MenuSystem`, `ControllerEmulator`\* |
| `player_data_updated` | `Dict[plid, {PName, CName, PNameBytes, CNameBytes, UCID, PType, Flags, IsAI, IsRemote, ControlMode}]` | `VehicleManager` | *(none — dead)* |
| `assistance_results` | `{system_key: result_dict}` | `AssistanceManager` | *(none — dead)* |

`buttons_cleared` is emitted when LFS tells us the user wiped our buttons (SHIFT+B,
`BFN_USER_CLEAR`) or asked for them back (`BFN_REQUEST`). `MessageSender` drops its
button registry on it so the next repaint really sends. `UIManager` redraws the idle
banner (everything else it owns is repainted every UI pass anyway) and `MenuSystem`
redraws the menu page that is currently open. Anything new that only draws on change
must subscribe too.

`vehicles_updated` carries an **immutable snapshot**: the dict is freshly built for
each MCI frame and every `Vehicle.data` object in it is replaced, never mutated, by
the next frame. A consumer may iterate it on a worker thread while packets keep
arriving. `own_vehicle_updated` does **not** give this guarantee — it hands out the
live `OwnVehicle`, which OutGauge keeps writing to (`known-issues.md` #12).

### `state_data` payload

| Key | Type | Meaning |
|---|---|---|
| `on_track` | bool | `ISS_GAME` and not `ISS_FRONT_END` |
| `text_entry` | bool | `ISS_TEXT_ENTRY` — chat is open, block key injection |
| `dialog` | bool | `ISS_DIALOG` |
| `track` | **`str`** | `IS_STA.Track`, decoded (`SO6R`) — was raw bytes before WP4/WP5 |
| `in_game_cam` | int | `IS_STA.InGameCam` |
| `in_game_interface` | int | `IS_CIM.Mode` — `CIM_NORMAL/OPTIONS/HOST_OPTIONS/GARAGE/CAR_SELECT/TRACK_SELECT/SHIFTU` |
| `submode_interface` | int | `IS_CIM.SubMode` — `NRM_*` / `GRG_*` / `FVM_*` |
| `select_type` | int | `IS_CIM.SelType` |
| `screen` | str | derived context: `main_menu`, `entry`, `garage`, `options`, `shiftu`, `game` (constants in `lfs/lfs_state.py`) |
| `ui_visible` | bool | `ISS_VISIBLE` — LFS is showing InSim buttons |
| `shift_u` | bool | `ISS_SHIFTU` |
| `multiplayer` | bool | `ISS_MULTI` |
| `buttons_allowed` | bool | **the one flag the UI needs**: false on `main_menu` and `options`, where LFS shows no normal buttons |

`state_data` is the widest-reaching event in the app and is now emitted from two
sources — `IS_STA` **and** `IS_CIM`. Changing its shape touches seven subscribers —
grep before editing. Keys may be added, never removed or renamed.

## Assistance system outputs

| Event | Payload | Emitter | Subscribers |
|---|---|---|---|
| `collision_warning_changed` | `{level: 0..3}` | `ForwardCollisionWarning` | `UIManager` |
| `cross_traffic_warning_changed` | `{level: 0..2, side: 'left'\|'right'\|None}` | `CrossTrafficWarning` | `UIManager` |
| `blind_spot_warning_changed` | `{left: bool, right: bool}` | `BlindSpotWarning` | `UIManager` |
| `pdc_changed` | `Dict[0..5, int]` — sensor → `-1` inactive, `0` clear, `1..3` near…nearest | `ParkDistanceControl` | `UIManager`, `PDCBeepController` |
| `needed_deceleration_update` | `{deceleration: float}` m/s² | `ForwardCollisionWarning` | `ControllerEmulator`\* |
| `ai_traffic_state_changed` | `{active: bool}` | `AIDriver` | `MenuSystem` |

PDC sensor index order: `0,1,2` = front left/middle/right, `3,4,5` = rear left/middle/right.

Warning-output events are emitted **only on change**, not every cycle. Keep that
contract — the UI relies on it and the bus is synchronous.

## Commands and actuation (app → LFS / hardware)

| Event | Payload | Emitters | Subscriber |
|---|---|---|---|
| `send_light_command` | `{light: 0..8, on: bool}` | `LightAssists` | `LFSConnector` |
| `siren_state_changed` | `{siren_active: bool}` | `LightAssists` | `LFSConnector` |
| `request_axm_update` | `{}` | `ParkDistanceControl` | `LFSConnector` |
| `send_command_to_lfs` | **`str`** — e.g. `"/axload AI_Traffic"` | `AIDriver` | `MessageSender` |
| `send_local_message_to_lfs` | **`str`** — chat line, may contain `^n` colours | `ChatCommandHandler` | `MessageSender` |
| `send_lfs_command` | **`{command: str}`** | `ControllerEmulator`\* | `UIManager` |

Light IDs: `0` sidelight, `1` low beam, `2` high beam, `3` fog front, `4` fog rear,
`5` extra, `6` indicator left, `7` indicator right, `8` hazards.

`send_command_to_lfs` and `send_lfs_command` do almost the same thing with different
payload shapes and different subscribers. This is a trap — see `known-issues.md` #4.

## UI and user interaction

| Event | Payload | Emitters | Subscriber |
|---|---|---|---|
| `notification` | `{notification: str}` (may carry `type`, `icon` from navigation) | ~27 call sites across most systems, plus `ThreadManager` / `AssistanceManager` when they disable a failing task or system | `UIManager` |
| `play_audio` | `{audio_file: str}` — basename without `.wav`, resolved under `audio/` | `UIManager` | `AudioPlayer` |
| `show_siren_ui` | `{ui: bool}` | `LightAssists` | `UIManager` |
| `siren_toggle_requested` | `{}` | `ChatCommandHandler` | `LightAssists` |
| `strobe_toggle_requested` | `{}` | `ChatCommandHandler` | `LightAssists` |
| `ai_traffic_start` / `ai_traffic_stop` | `{}` | `MenuSystem` | `AIDriver` |
| `gearbox_calibrate` | `{}` | `MenuSystem` | `Gearbox` |
| `await_keybinding` | `{setting: str}` | `MenuSystem` | `Keybinder` |
| `new_keybinding` | `{button: str, setting: str}` | `Keybinder` | `MenuSystem` |

Notifications are queued in `UIManager.notifications` and displayed one at a time for
3 s on button ID 61. A burst of notifications therefore takes `3 × n` seconds to drain.

## Debug-only

| Event | Payload | State |
|---|---|---|
| `dist_debug` | `{distance: float}` | emitted per vehicle per cycle by FCW; subscriber in `UIManager` is commented out — **live emission in the hot path** |
| `decel_debug` | `{deceleration: float}` | subscriber commented out, never emitted |

\* `ControllerEmulator` is not instantiated — it is commented out in
`AssistanceManager._init_systems`. Events only it consumes are currently inert.
