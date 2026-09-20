# Reading LFS's controller configuration off disk

LFS stores the whole controller setup — axis assignments, button assignments, key
bindings, wheel turn angle — in small binary files under `C:\LFS\data\misc`. The format
is undocumented by Scavier and nothing was found published about it; everything below
was reverse engineered from the seven files on this install (LFS **0.8C26**).

**Why this document exists.** Two problems need this data and both had only destructive
answers before: finding the driver's brake axis (`control-intervention.md` §3.2) and
backing up the controller setup around a vJoy install (`known-issues.md` #41). Reading
a file touches nothing.

Each section separates **measured** (derived from bytes, cross-checked against live
values), **inferred** (consistent with everything seen, not proved) and **unknown**.
Do not promote an inference to a fact without running the experiment named in §7.

---

## 1. Which file holds what — measured

| File | Holds | Notes |
|---|---|---|
| `data\misc\<Device>.csf` | **the whole controller config** | format v7, written by 0.8C26. **This is the current one.** |
| `data\misc\<Device>.con` | the same, older layout | format v6 here, v1 for the 2006 shipped presets. Legacy — 0.8C26 no longer writes it. |
| `cfg.txt` line `Control Mode a b` | `mouse_kb` vs `wheel_js` | plain text, global, not per driver |
| `data\misc\*.ply` | driver name, licence, skin names, ~30 display flags | **no controller data at all** |

The `.ply` hypothesis is **disproved**: diffing the three `.ply` files on this install
shows they differ only in a length field, one flag byte and the skin names. Per-driver
control settings do not exist — LFS has one controller configuration for the whole
installation.

**Each device file is a complete configuration snapshot, not that device's slice.**
Every `.con`/`.csf` contains all 11 axis functions, all button functions *and* all key
bindings. LFS writes a copy per device name so that a device coming back gets its
mapping restored. So to read "the driver's brake axis" you must read the file of the
**device the driver actually uses**, not just any file in the folder.

Filenames are the DirectInput product name with spaces replaced by underscores, plus a
numeric suffix when the name is ambiguous (`FANATEC Wheel` -> `FANATEC_Wheel_3.csf`; two
devices on this machine report that same product name). What the suffix counts is
**unknown**, and it is the main hazard for a restore — see §7.

## 2. When LFS writes them — measured

- **Not** when an InSim `/axis` or `/key` command arrives. A `/axis` sweep run at ~11:49
  left every `.con` and `.csf` byte-identical to a copy taken at 11:48, and they were
  still identical hours later while LFS ran.
- On exit, and only for devices whose configuration changed. On this install the LFS
  session that ended at 11:25 rewrote `vJoy_Device.csf`, `drv.nam`, `views.bin` and the
  `.ply` files, and left `FANATEC_Wheel_3.csf` (dated weeks earlier) alone.
- **Consequence, and it cuts both ways.** A config change made over InSim is invisible on
  disk until LFS exits — so the file can be stale. But it also means a configuration
  damaged by a bad command is still intact on disk until LFS exits, which makes copying
  the folder the first move after any accident.

## 3. Container layout — measured

Same container for `.con` and `.csf`; only the version and the two table widths differ.
All integers little-endian. Verified on all seven files: the computed end offset equals
the file size exactly, so **there is no checksum and no trailing data**.

```
0x00  6 bytes   "LFSCON"          (also in .csf — the extension changed, the magic did not)
0x06  u8        0
0x07  u8        format version    1 = 2006 presets, 6 = .con, 7 = .csf
0x08  f32       wheel turn angle in degrees   (= /wheel_turn)
0x0c  u32 x 7   unknown, see below
0x28  u32       1 in every file seen
0x2c  u32       N = number of button/key functions   (52 in v1, 61 in v6, 77 in v7)
0x30  N x 4     button/key table   — u16 button, u16 key
      u32       M = number of axis functions        (11 in every version)
      M x 4|8   axis table   — 4 bytes in v1, 8 bytes in v6/v7
```

`wheel turn angle` is measured, not guessed: the four devices read 1080 (Fanatec),
720 (G25), 290 (MOMO), 270 (SideWinder) — each device's real range.

The seven `u32`s at `0x0c` are **unknown**. Observed: `[0]` is 1 everywhere except
`vJoy_Device.csf` (3); `[1]` is 0/1 and varies by device; `[3]` is `0x0A0A` in both
`.csf` files and 0 in every `.con`. Do not read anything from this block.

## 4. The axis table — measured, with one inferred step

`M = 11` entries, in exactly the order `Commands.txt` lists the `/axis` function names:

```
0 steer   1 combined   2 throttle   3 brake   4 lookh   5 lookp   6 lookr
7 clutch  8 handbrake  9 shiftx    10 shifty
```

v6/v7 entry, 8 bytes: `u16 axis, u16 invert, u16 p, u16 q`
v1 entry, 4 bytes: `u16 axis, u16 invert`

- `axis` = `0xFFFF` means unassigned (what `/axis -1 <fn>` writes).
- `invert` = 0/1, matching `/invert`. On this wheel all four pedal-ish functions are 1
  and `steer` is 0, which is what a DirectInput pedal set looks like.
- `p`, `q` are **unknown**. They take only three value pairs and those pairs are
  *identical across devices*, including for unassigned functions — so they are per
  *function*, not per device: `steer` and the three `look` axes `(100, 0)`, `clutch`
  `(65, 5)`, everything else `(95, 5)`. A calibration or deadzone percentage pair is the
  obvious reading; it is not confirmed and nothing should depend on it.

### Saved configuration can disagree with working live pedals

Observed in the axis-throttle-cut investigation: `FANATEC_Wheel_3.csf` stored
steer `0xFFFD`, combined `0xFFFE`, throttle `0xFFFF`, brake 11 and clutch 12,
while the user reported a working throttle pedal in the running game. The
older `.con` still stored throttle 8/invert 1. This does not prove that the
legacy value is the current live assignment; do not fall back to it automatically.
The user subsequently confirmed the live assignments for this installation:
steer 8/invert 0, throttle 9/invert 1, brake 12/invert 1, clutch 13/invert 1.
These agree with the older file after offset +1, but are not portable defaults
and do not validate choosing an older file automatically on other machines.
Confirmed after the user closed LFS normally: `FANATEC_Wheel_3.csf` was
rewritten with steer 7/invert 0 and throttle 8/invert 1; brake 11/invert 1
and clutch 12/invert 1 remained. The current reader resolves exactly the
user-confirmed live assignments (8, 9, 12, 13). Combined remains 0xFFFE.
This case was a stale saved snapshot; no legacy fallback or hardcoded axis
is needed. The user then confirmed in a live wheel/joystick test that the held throttle
pedal is suppressed during intervention and works normally after handback.
This validates this installation, not arbitrary device-enumeration offsets.
The meaning of `0xFFFD`/`0xFFFE` remains unknown. Never add an offset and pass
these values to `/axis`: the reader now excludes assignments outside 0..31 and
invalid polarity, and accepts only observed format versions 1, 6 and 7.

A missing throttle in the selected snapshot requires comparison with the live
Options -> Controls -> Axes page. Stop the add-on before exiting LFS to save
working assignments, then restart both; verify the newly saved file rather
than assuming that a restart rewrote the expected device snapshot.

### 4.1 The number stored is NOT the number `/axis` takes

This is the trap. `FANATEC_Wheel_3.csf` reads:

```
steer 7   combined 1   throttle 8   brake 11   clutch 12
```

The same functions measured live against this LFS install are `/axis` **8, 9, 12, 13** —
every one exactly **one higher**. Four independent values with a uniform offset is not a
coincidence, but the offset is not automatically 1 for everyone:

- **Inferred:** the stored number is a **device-local** axis index, and the number
  `/axis` wants is `1 + (axes of every device LFS enumerates before this one) + local`.
  The device-local reading is forced by the 2006 preset files — Scavier shipped
  `Logitech_G25_Racing_Wheel_USB.con` with `steer 0, throttle 1, brake 2, clutch 4`,
  which can only be the G25's own X/Y/Rz/slider, since he could not know what else a
  buyer would plug in. v1 and v6/v7 use the same table with the same semantics, only
  widened, so the modern files mean the same thing.
- **Unconfirmed, and this is the gap:** the offset for this install would put the wheel
  first with 14 axes ahead of vJoy (whose axis LFS numbers 15). Enumerating the machine's
  DirectInput devices read-only reports three — `FANATEC Wheel` (12 axes), a second
  `FANATEC Wheel` (8), `vJoy Device` (8) — and no ordering of those adds up to 14. So
  **LFS does not count axes the way SDL does**, and the base offset cannot yet be
  computed from outside. §7 names the experiment that settles it.

**So: treat the file as producing a candidate, never an answer.** `brake + 1` is the
right first guess for a driver with one wheel and it was right here, but the app must
still run the non-destructive verification from `control-intervention.md` §3.2 (point
`brake` at the candidate, ask the driver to press the pedal, watch `OutGaugePack.Brake`)
before arming anything. That step is what makes reading the file safe to use at all.

What the file *does* answer outright, with no offset problem:

- whether `brake` is assigned to an axis at all, and its `invert` flag;
- whether `clutch` and `handbrake` were on an axis when saved. `handbrake` = `0xFFFF`
  means no saved handbrake axis. AutoHold uses dashboard confirmation at runtime:
  these files may be stale until LFS exits, so they cannot verify a live key binding.

## 5. The button/key table — measured for indices 0..20

`N` entries of `u16 button, u16 key`, `0xFFFF` = unassigned in either column. The order
is the `/button` and `/key` function list from `Commands.txt`, and the first 21 are
confirmed:

```
 0 steer_left   1 steer_right   2 steer_fast   3 steer_slow
 4 throttle     5 brake         6 shift_up     7 shift_down
 8 clutch       9 handbrake    10 left_view   11 right_view
12 rear_view   13 horn         14 flash       15 reset
16 pit_speed   17 tc_disable   18 ignition    19 zoom_in     20 zoom_out
```

The anchor is the 2006 files: they have 52 entries where the modern ones have 61, and
diffing the key columns shows one entry inserted at index **18** with default key `I` —
`ignition`, a function LFS gained after 2006, at exactly the position `Commands.txt`
lists it. That pins the whole run 0..20.

**Beyond index 20 the mapping is not established.** Nine entries were added between v1
and v6 and only that one was located, so eight insertions are unaccounted for. The
entries carrying buttons on this wheel (21-28) are therefore *not* reliably `vr_click`,
`escape`, `virtual_kb`, `talk`, `reverse`, `gear_1..3` — do not act on those indices.

`key` is a **Windows virtual-key code**, not an LFS key name. Letters and digits are
their ASCII uppercase (`'B'` = 0x42), and the LFS names map onto VK codes:
`mousel` = 0x01, `mouser` = 0x02, `space` = 0x20. The rest of the `/key` list
(`up/down/left/right`, `pgup`, `pgdn`, `mousem`, `wheelu`, `wheeld`) is **not observed**
here — none of them appear in any of the seven files, so their codes are unverified.

**Whether the key column tracks the user's own bindings is unknown.** All seven files —
including the 2006 presets — carry an identical key column, so this user has never
changed a key binding and there is no evidence either way. If it does track them, index
5 gives the driver's brake key directly, which is what the `mouse_kb` path in
`control-intervention.md` §3.1 currently has to push with `/key`. See §7.

## 6. Practical rules

- Read `<Device>.csf` first, fall back to `<Device>.con`, and **check byte 7**: parse
  4-byte axis entries when it is <= 1, 8-byte entries otherwise. Never assume a file
  size — take both table lengths from their count fields.
- Never write into `C:\LFS`. Everything here is a read.
- Anything read from these files is a **candidate to verify**, never a value to arm on.
  A wrong axis number produces exactly the silent misconfiguration this project keeps
  fighting.
- Take a copy of `cfg.txt` and all of `data\misc\*.con` / `*.csf` before anything that
  can disturb the controller setup, and take it **while LFS is still running** — §2.

## 7. Open questions and the experiments that settle them

Each of these needs the user to make one change in LFS and then **fully exit LFS** (the
file is not written before that, §2), after which the file is diffed against a copy taken
beforehand.

1. **How the device-local index becomes the `/axis` number** (§4.1) — the one that
   actually blocks automatic use. Assign the brake to a *different* axis of the same
   wheel in Options -> Controls, note the number LFS shows, exit, diff. The stored value
   and the displayed value together give the base offset directly, and repeating it on
   the vJoy device gives the second device's base.
2. **Whether the key column follows user key bindings** (§5). Rebind one key with an
   obvious code — brake to `K` (0x4B) — in the options screen, exit, diff. If index 5's
   key column becomes 0x4B, the driver's brake key is readable and `/key` no longer has
   to be pushed blind. Also worth checking whether *all* device files change or only one.
3. **Whether a restored file is honoured** (`known-issues.md` #41). Copy the folder,
   change one binding, exit, restore the copy, start LFS: if the old binding is back, the
   backup/restore plan around a vJoy install works. Watch the **filename suffix**
   (`_3`) while doing it — if installing a device renames the wheel's file, restoring the
   old name is not enough and the restore has to follow the rename.
4. **`p`/`q` in the axis entry** (§4) — change a per-axis setting in the controls screen
   and see which of the two moves. Low value; nothing should depend on them.
