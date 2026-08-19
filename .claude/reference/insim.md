# InSim, OutGauge, OutSim and the pyinsim fork

## 1. The three LFS interfaces

| | InSim | OutGauge | OutSim |
|---|---|---|---|
| Transport | **TCP** (also UDP for MCI/NLP if `UDPPort` set) | UDP | UDP |
| Port here | 29999 | 30000 | 29998 |
| Direction | **bidirectional** | game → app only | game → app only |
| Rate | event-driven + polled | high (per physics/graphics tick) | high |
| Scope | everything: game state, all cars, players, chat, layout, UI buttons, commands, AI control | **own car only**: speed, rpm, gear, fuel, pedals, dashboard lights | **own car physics**: position, velocity, acceleration, G-forces, per-wheel data |
| Enabled by | `/insim 29999` in game (or `autoexec.lfs`) | `cfg.txt` OutGauge settings | `cfg.txt` OutSim settings |

**~99 % of this project runs on InSim.** OutGauge supplies the few high-rate own-car
signals (speed, rpm, gear, pedals, dash lights) that MCI does not carry or does not
carry fast enough. OutSim is connected (`LFSConnector.start_outsim`) and emits
`outsim_data`, but **nothing subscribes to it yet** — it is the intended source if a
system ever needs G-forces, per-wheel loads or slip.

The first-run setup wizard (`core/setup_wizard.py`) writes the required `cfg.txt`
entries (`OutSim Mode 2`, `OutSim Opts 1ff`, ports, `OutGauge Mode 2`) and optionally
appends `/insim 29999` to `data/script/autoexec.lfs`.

## 2. How an InSim packet reaches your code

Three ways LFS sends a packet:

1. **On change** — e.g. `IS_STA` when the game state changes, `IS_NPL` when a player
   joins, `IS_AXM` when the layout is edited. Just bind and wait.
2. **On interval** — set `Interval` and the relevant `ISF_*` flag in the `IS_ISI`
   handshake. This project requests `ISF_MCI`, so `IS_MCI` arrives every
   `assistance_refresh_rate` ms.
3. **On request** — send `IS_TINY` with a `TINY_*` subtype, e.g.
   `TINY_SST` (state), `TINY_NPL` (all players), `TINY_AXM` (full layout).

Pattern:

```python
insim = pyinsim.insim(b'127.0.0.1', 29999, Admin=b'', Prefix=b'$',
                      Flags=pyinsim.ISF_MCI | pyinsim.ISF_AXM_LOAD |
                            pyinsim.ISF_AXM_EDIT | pyinsim.ISF_LOCAL,
                      Interval=100)
insim.bind(pyinsim.ISP_MCI, handler)        # handler(insim, packet)
insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_SST)
pyinsim.run()                               # blocking asyncore loop
```

In this project all of that lives in `lfs/connector.py`; **add new packet bindings
there** and re-publish them as events rather than binding elsewhere.

**Multi-packet responses.** `IS_MCI` carries at most `MCI_MAX_CARS = 16` cars and
`IS_AXM` at most 60 objects. With more cars/objects, LFS sends **several packets per
request**. Never assume one packet = complete picture. `CompCar.Info` carries
`CCI_FIRST` (64) and `CCI_LAST` (128) bits marking the packet set boundaries — these
are the reliable way to reassemble a frame. `VehicleManager` currently reassembles by
counting cars against `len(self.players)` instead, which is fragile
(`known-issues.md` #6).

## 3. Packets actually used here

Bound in `LFSConnector._setup_handlers`:

| Packet | Meaning | Re-emitted as |
|---|---|---|
| `ISP_STA` | game state (on track, dialogs, text entry, camera, track name) | `game_state_changed` → `StateHandler` → `state_data` |
| `ISP_MCI` | multi car info: position, speed, heading of every car | `vehicle_data_received` |
| `ISP_NPL` | new player (also fired when leaving the pits) | `player_joined` |
| `ISP_PLL` | player left | `player_left` |
| `ISP_BTC` | button clicked | `button_clicked` |
| `ISP_MSO` | chat/system message received | `message_received` |
| `ISP_AXM` | autocross layout added/removed/cleared | `layout_received` |

Sent by this project:

| Packet | Used for |
|---|---|
| `ISP_BTN` | draw a UI button (the only way to render anything in LFS) |
| `ISP_BFN` | delete a button by `ClickID` |
| `ISP_TINY` | request state (`TINY_SST`), players (`TINY_NPL`), layout (`TINY_AXM`) |
| `ISP_SMALL` + `SMALL_LCL` | **local car lights** — indicators, low/high beam, fog, extra |
| `ISP_SMALL` + `SMALL_LCS` | **local car switches** — siren, horn, flash |
| `ISP_MST` | send a command to LFS (e.g. `/axload AI_Traffic`, `/restart`) |
| `ISP_MSL` | send a local-only message into the chat area |
| `ISP_AIC` | **AI control** — drive an LFS AI car (see `ai-traffic.md`) |
| `ISP_AII` | AI info response (rpm, gear, physics) — received |

**Not bound but important — `ISP_CIM`.** It reports which LFS screen the connection is
on (entry screen, garage and its submenus, options, car/track select, SHIFT+U) and is
the only reliable way to distinguish them. pyinsim already implements `IS_CIM` and every
`CIM_*` / `GRG_*` / `FVM_*` constant; `lfs/lfs_state.py` even has `in_game_interface`
and `submode_interface` fields waiting for it. It needs no `ISF_*` flag — LFS sends it
whenever the mode changes. See `ui.md` §1.

Also not bound: `ISP_BFN` **inbound** (`BFN_USER_CLEAR` / `BFN_REQUEST` — the user
clearing or re-requesting our buttons, `ui.md` §1.5).

Other useful ones not yet used: `ISP_CON` (car contact), `ISP_OBH` (object hit),
`ISP_HLV` (hotlap validity), `ISP_NCN`/`ISP_CNL` (online connections),
`ISP_PLC` (allowed cars), `ISP_SLC` (connection selected a car),
`ISP_MAL` (mods allowed on a host).

### Key constants

```python
# IS_STA Flags (bit test them, do not compare)
ISS_GAME 1  ISS_REPLAY 2  ISS_PAUSED 4  ISS_SHIFTU 8  ISS_DIALOG 16
ISS_SHIFTU_FOLLOW 32  ISS_SHIFTU_NO_OPT 64  ISS_SHOW_2D 128
ISS_FRONT_END 256 (entry screen)  ISS_MULTI 512  ISS_MPSPEEDUP 1024  ISS_WINDOWED 2048
ISS_SOUND_MUTE 4096  ISS_VIEW_OVERRIDE 8192
ISS_VISIBLE 16384 (InSim buttons visible — the authoritative UI-visibility signal)
ISS_TEXT_ENTRY 32768
# on_track  ==  (Flags & ISS_GAME) and not (Flags & ISS_FRONT_END)

# IS_CIM Mode — which LFS screen the connection is on
CIM_NORMAL 0  CIM_OPTIONS 1  CIM_HOST_OPTIONS 2  CIM_GARAGE 3
CIM_CAR_SELECT 4  CIM_TRACK_SELECT 5  CIM_SHIFTU 6
# SubMode for CIM_NORMAL : NRM_NORMAL/WHEEL_TEMPS/WHEEL_DAMAGE/LIVE_SETTINGS/PIT_INSTRUCTIONS (F9-F12)
# SubMode for CIM_GARAGE : GRG_INFO/COLOURS/BRAKE_TC/SUSP/STEER/DRIVE/TYRES/AERO/PASS
# SubMode for CIM_SHIFTU : FVM_PLAIN 0 / FVM_BUTTONS 1 / FVM_EDIT 2

# IS_BFN SubT — bidirectional
BFN_DEL_BTN 0 (→LFS: delete one button or a ClickID range)
BFN_CLEAR 1   (→LFS: clear every button made by this InSim instance, one packet)
BFN_USER_CLEAR 2 (←LFS: user pressed SHIFT+B and cleared our buttons)
BFN_REQUEST 3    (←LFS: user pressed SHIFT+B / SHIFT+I asking for buttons back)

# IS_BTN Inst
INST_ALWAYS_ON 128 (button visible in ALL screens, incl. garage and options)

# IS_MSO UserType
MSO_SYSTEM 0   MSO_USER 1   MSO_PREFIX 2 (a message starting with the InSim Prefix, '$')   MSO_O 3

# IS_AXM PMOAction
PMO_LOADING_FILE 0  PMO_ADD_OBJECTS 1  PMO_DEL_OBJECTS 2  PMO_CLEAR_ALL 3  PMO_TINY_AXM 4 …

# SMALL_LCL — lights. UVal = SET_flag | (MASK_flag if on else 0)
LCL_SET_SIGNALS 0x01   LCL_SET_LIGHTS 0x04   LCL_SET_FOG_REAR 0x10
LCL_SET_FOG_FRONT 0x20 LCL_SET_EXTRA 0x40
LCL_Mask_Left 0x00010000  LCL_Mask_Right 0x00020000  LCL_Mask_Signals 0x00030000
LCL_Mask_SideLight 0x00040000  LCL_Mask_LowBeam 0x00080000  LCL_Mask_HighBeam 0x000C0000
LCL_Mask_FogRear 0x00100000  LCL_Mask_FogFront 0x00200000  LCL_Mask_Extra 0x00400000

# SMALL_LCS — switches
LCS_SET_SIREN 0x10   LCS_Mask_Siren 0x300000 (0 off / 1 fast / 2 slow)

# OutGauge ShowLights (dashboard) — DL_*
DL_SHIFT 1  DL_FULLBEAM 2  DL_HANDBRAKE 4  DL_PITSPEED 8  DL_TC 16
DL_SIGNAL_L 32  DL_SIGNAL_R 64  DL_OILWARN 256  DL_BATTERY 512  DL_ABS 1024
DL_ENGINE 2048  DL_FOG_REAR 4096  DL_FOG_FRONT 8192  DL_DIPPED 16384
```

**Do not send raw `SMALL_LCL` packets from a system.** Emit
`send_light_command` with `{'light': <0-8>, 'on': bool}`; `LFSConnector.send_light_command`
owns the flag/mask mapping. Light IDs: `0` sidelight, `1` low beam, `2` high beam,
`3` fog front, `4` fog rear, `5` extra, `6` indicator left, `7` indicator right,
`8` hazards.

## 4. The UI is buttons — all of it

LFS has no overlay API. Everything this add-on draws is `IS_BTN` packets.

- Screen space is **0…200 on both axes**, origin top-left.
  `T` = top (vertical), `L` = left (horizontal), `W` = width, `H` = height.
- **Recommended area: `L` 0…110, `T` 30…170.** Buttons inside it make LFS keep that
  rectangle clear of its own UI; buttons outside it get no space reserved and simply
  overlap. This is the cause of the "LFS menu disappears" effect on the entry screen —
  see `ui.md` §1.3.
- Up to 240 buttons, `ClickID` 0…239. `ISF_LOCAL` (set by this project) puts them in the
  local button area so they cannot collide with a host control system's buttons and the
  user toggles them with SHIFT+B rather than SHIFT+I.
- `ClickID` identifies the button. Re-sending the same `ClickID` replaces it;
  `IS_BFN` with that `ClickID` deletes it. Re-sending with `W = H = 0` updates only the
  **text** of an existing button, without needing to know its position.
- `Inst = INST_ALWAYS_ON` (128) makes a button visible in every screen including garage
  and options. Use only at screen edges. `LFSConnector.send_button()` accepts `inst`
  but no caller passes it.
- Styles: `ISB_LIGHT` (light background), `ISB_DARK` (dark), `ISB_LMB` (transparent),
  `| ISB_CLICK` to make it clickable → produces `IS_BTC` with that `ClickID`.
  `ISB_LEFT` / `ISB_RIGHT` control text alignment.
- Text colours are inline prefixes: `^0` black, `^1` red, `^2` green, `^3` yellow,
  `^4` blue, `^5` magenta, `^6` cyan, `^7` white.

In this project always go through `MessageSender.create_button(id, x, y, w, h, text, style)`
(note the argument order: **x = L, y = T**) and `remove_button(id)`. The button ID
allocation map is in `reference/ui.md` — respect it, IDs collide silently.

## 5. The pyinsim fork (`pyinsim/`)

Forked from pyinsim 2.1.0 (Alex McBride, LGPL) and extended locally because upstream is
stale. **It is project source, not a vendored dependency — edit it when LFS gains
features.**

| File | Contents |
|---|---|
| `insim.py` (~2280 lines) | Every packet class and constant. Each class is a plain object with a `struct.Struct` and `pack()` / `unpack()`. |
| `core.py` | Transport: `_TcpSocket` / `_UdpSocket` on `asyncore`, `_InSim` (bind/send/dispatch), `_OutSim`, `run()`, `closeall()`. |
| `func.py` | Helpers: `stripcols`, `stripenc`, etc. for LFS-encoded strings |
| `strmanip.py` | LFS codepage string handling |

Local additions beyond upstream: `IS_AIC` / `AIInputVal` / `IS_AII` (AI control,
InSim 0.7F+), `SMALL_LCS` / `SMALL_LCL` constants, `ISP_MAL`/`ISP_PLH`/`ISP_IPB`
enum entries, `INSIM_VERSION = 10`, and the 0.7A `Size = bytes/4` packet framing.

Things to know before editing:
- Packet **size byte is bytes / 4** (InSim 0.7A+). `IS_AIC.pack()` computes
  `Size = 1 + len(inputs)`; get this wrong and LFS drops or desyncs the stream.
- `_handle_insim_packet` auto-replies to the `TINY_NONE` keep-alive. Do not break it.
- OutGauge/OutSim packets are identified purely by **datagram length**
  (`_OUTGAUGE_SIZE = (92, 96)`, `_OUTSIM_SIZE = (64, 280)`). If `cfg.txt` OutSim Opts
  differ from `1ff`, the size changes and the packet is silently ignored.
- `asyncore` is removed in Python 3.12 — see `CLAUDE.md` §2.

## 6. When pyinsim is not enough: the LFS docs

Authoritative protocol definitions ship with the game:

| File | Use it for |
|---|---|
| `C:\LFS\docs\InSim.txt` | **The reference.** Every packet struct, every flag and enum, byte-exact layouts, and a changelog per LFS version. Consult it whenever adding a packet to `pyinsim/insim.py` or when a field looks wrong. |
| `C:\LFS\docs\OutSimPack.txt` | OutSim packet layout and the `OSO_*` option bits that decide which blocks are present. |
| `C:\LFS\docs\Commands.txt` | All in-game `/` commands — needed when sending `ISP_MST` (e.g. `/axload`, `/restart`, `/axis`). |

**Caveat:** the installed `InSim.txt` documents `INSIM_VERSION = 9` (LFS 0.7E) and does
**not** contain `IS_AIC` / `IS_AII`. Our fork targets version 10 (0.7F+). For AI control
the code in `pyinsim/insim.py` and `AI_Control.py` is the reference; if the local LFS
install is updated, re-read `InSim.txt` and reconcile.
