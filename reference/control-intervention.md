# Taking control away from the driver

Design reference for any feature that actuates the car rather than only warning:
automatic emergency braking, cruise control / ACC, lane keeping, auto-hold, the
automatic gearbox.

Treat everything here with the ECU mindset from `AGENTS.md` §1: an intervention that
works 95 % of the time is not a feature, it is a hazard.

**Who may ask for braking.** `EmergencyBrake` is the single actuator; three warning
systems feed it a deceleration demand over `needed_deceleration_update` and none of them
touches an input device (`events.md`). Adding a fourth means adding a `source`, putting
the system in front of `aeb` in `AssistanceManager._init_systems`, and answering the
question below — nothing else.

**Braking is not automatically the safe answer, and every source has to say why it is.**
The physics is in `assistance/path_conflict.py`; the decision is not.

| Source | We are… | Braking… | So when the conflict can no longer be avoided |
|---|---|---|---|
| `forward_collision` | running into somebody ahead | always reduces our impact speed | brake anyway (mitigation) |
| `cross_traffic` | driving into somebody's path | keeps us out of it, while there is room | brake anyway |
| `blind_spot` | being overtaken from behind | only helps *before* we enter their lane | **stop asking** — once we are in their corridor, braking lengthens their approach and raises the speed they arrive with |

That last row is the reason `BlindSpotWarning` drops its demand at `free_distance == 0`
while `CrossTrafficWarning` escalates there. Getting it the wrong way round turns an
assistant into the cause of the crash.

---

## 1. The fundamental problem

LFS accepts driver input from a device the user configured. We have no API to say
"apply 0.4 brake" to the player's car — `IS_AIC` exists but works **only on AI cars**.
So every intervention has to *impersonate an input device*, and then two independent
sources drive one car. That raises three questions that must be answered explicitly for
every feature:

1. **Arbitration** — when the driver and the assistant disagree, who wins?
2. **Handover / handback** — how does control transfer, and how does the driver take it
   back instantly?
3. **Fail-safe** — what happens if our process hangs, crashes, or is killed mid-intervention?

The safe default for all three: **the assistant may only ever *add* braking, never
reduce what the driver commands, and losing our process must leave the driver with full
normal control.** Any design where our app sits in the *only* path between the user's
hardware and LFS fails that test — if we die while holding the brake axis, the driver
has no brakes.

That is why every injected key is released from a place the shutdown path reaches.
The emergency brake does it in `KeyBrakeOutput.release()`; the *short* presses of
`AutoHold` and `Gearbox` do it through `misc/key_tap.py` — the tapper holds the key on
its own thread, and `release_all()` (called from both systems' `shutdown()`, idempotent
because the tapper is shared) drops whatever is still down. A clutch or handbrake left
pressed when the process ends stays pressed for LFS.

---

## 2. What LFS actually accepts — measured, not assumed

Established live against LFS 0.8C26 (`cfg.txt` line 1; wheel and keyboard both tried, InSim probe plus
in-game confirmation). These facts decide the whole design; do not re-derive them from
intuition.

### 2.1 The control mode gates which input LFS listens to

`/control [mouse_kb|wheel_js]` mirrors the user's choice in *Options -> Controls*, and
it is not cosmetic:

| Function | `mouse_kb` | `wheel_js` |
|---|---|---|
| throttle, brake | **key** (`/key`) or mouse | **axis only — keys are ignored** |
| steering | key / mouse | axis |
| clutch, handbrake | key | **user-selectable per function: axis *or* key** |
| shift_up/down, horn, lights, ... | key | key (works in both modes) |

The `wheel_js` row is the important one: **a key bound to `brake` is silently ignored in
`wheel_js` mode.** Verified directly — `/key B brake` sent while in `wheel_js` did
nothing; the same binding started working the moment the user switched to `mouse_kb`.
LFS accepts and stores the command in both modes, it just does not read the key. There
is no error and no feedback.

Consequence: **there are two disjoint actuation paths, and both must exist.** Key
injection covers `mouse_kb`; `wheel_js` needs a virtual analog axis. Neither one covers
the other mode.

`IS_NPL.Flags` tells us which mode is active without asking the user: `PIF_MOUSE` ->
mouse, `PIF_KB_NO_HELP | PIF_KB_STABILISED` -> keyboard, neither -> wheel. Mouse and
keyboard are the *same* LFS control mode (`mouse_kb`); the PIF flags distinguish them
within it. `VehicleManager._get_control_mode()` does exactly this and is correct — a
wheel user really does report `Flags=0x0201` (`SWAPSIDE|AUTOCLUTCH`) with no
input-mode bit set. If actuation appears dead, the mode detection is not the suspect.

**The flags follow a mode change immediately** — no pit stop, no rejoin. Measured on
one driver switching *Options -> Controls* while sitting on track::

    wheel/joystick   Flags=0x0201  SWAPSIDE,AUTOCLUTCH
    mouse+keyboard   Flags=0x0649  SWAPSIDE,AUTOGEARS,HELP_B,AUTOCLUTCH,MOUSE

So a driver who changes control mode mid-session gets the right actuation path on the
next `IS_NPL`, and nothing needs to be cached across sessions.

**Corrected 2026-09-19.** The extra bits in that second line are not part of the mode.
`PIF_AUTOGEARS` / `PIF_HELP_B` / `PIF_AUTOCLUTCH` are the **driving-help level**, which
the driver cycles with SHIFT+G and which LFS announces with `IS_PFL`, not `IS_NPL`
(`insim.md` §7 has the probe and the three measured levels). The driver on that day had
simply switched helps on as well. The operational conclusion is unchanged and still
matters — a `mouse_kb` driver very often has LFS's brake help running, so **anything
measuring achieved deceleration has to expect LFS to already be helping** — but it is
their choice, not something the mode switch does for them. Two consequences for code:
bind `ISP_PFL`, and never conclude anything about the helps from the control mode.

**One key per function, always.** LFS keeps exactly one key binding per function; a
second `/key` for the same function replaces the first. So an "our own private key
*in addition to* the driver's" design is impossible, and §3.1's arbitration is not
optional. Some keys are also reserved by LFS and cannot be bound at all (`N` shows
player names), and `/key` accepts them silently without taking effect — another
reason the binding has to be pushed from a key the user picked, and a candidate for
validation if the list of reserved keys is ever established.

### 2.2 Commands that matter (`C:\LFS\docs\Commands.txt`)

```
/control  [mouse_kb/wheel_js]   controller type
/axis     [axis] [function]     steer, combined, throttle, brake, clutch, handbrake, ...
/invert   [0/1]  [function]
/key      [key]  [function]     throttle, brake, shift_up, shift_down, clutch, handbrake, ...
/button   [button] [function]   same function names, for controller buttons
/press    [key]                 momentary key press - a tap, cannot hold. Useless for braking.
```

> ### `/axis N <function>` DESTROYS whatever axis N was doing before
>
> LFS holds **one function per axis and one axis per function**. Assigning an axis to
> `brake` therefore does not only take `brake` away from the axis that had it — it also
> takes the *old function* away from axis N. `/axis 8 brake` on a wheel whose axis 8 is
> the steering leaves that driver **with no steering**, silently, with no way to notice
> until they try to drive.
>
> Learned the hard way: an automated sweep of `/axis 0 brake` … `/axis 31 brake`, run to
> discover which axis was the brake pedal, wiped the driver's entire axis configuration —
> steering, throttle, clutch, everything. Only the last axis touched kept a function.
>
> **The safe rule: only ever assign an axis to the function it already has, or an axis
> that has no function at all.** The runtime handover obeys it (the vJoy axis has no other
> job, and the driver's axis is already `brake`). Anything that wants to *search* does
> not, and must not be written.

`/axis -1 <function>` and `/key -1 <function>` unassign. Keys valid for `/key`: `A-Z`,
`0-9`, `space`, `up/down/left/right`, `pgup`, `pgdn`, **`mousel`, `mouser`, `mousem`,
`wheelu`, `wheeld`** — mouse buttons are bindable as keys, which is how a mouse driver
is covered by the same mechanism as a keyboard one.

`/key` writes the binding whatever the current control mode is, so it can be pushed at
startup regardless of how the user is driving.

**Mouse buttons work as a full actuation path — measured end to end.** With
`user_brake_key = mouser` and `user_throttle_key = mousel`, `KeyBrakeOutput`
pushed `/key mouser brake`, armed, and stopped an FZ5 from 65 km/h without
contact in `simulation_tests` scenario `05_fcw_rear_end_keyboard` (run
`20260919-104215`; the same recording hit the lead car at 78.5 km/h closing
speed without the add-on). Achieved deceleration 8.7–8.9 m/s², which is where a
road car on dry tarmac belongs. `misc/physical_keys.py` already tracks
`VK_LBUTTON/RBUTTON/MBUTTON` through the mouse hook, so the key-release trap is
covered for buttons exactly as it is for keys.

Two caveats that came out of the same measurement, both logged in
`known-issues.md`: the **throttle cut did not stay effective** (#46 — LFS went
back to reading the held left button despite `/key -1 throttle`), and the
**automatic gearbox upshifted into the stop** (#47, fixed 2026-09-19). Neither
prevented the stop, but neither is the behaviour the design above assumes.

Note what #46's measurement rested on: the cut "cost no deceleration only
because the clutch happened to be open" — and that open clutch was #47's
doing. With the gearbox no longer holding it, **#46 has to be re-measured**;
the drivetrain is now closed during an intervention, which is the condition
under which a leaked throttle actually fights the brake.

### 2.3 OutGauge closes the loop

`OutGaugePack.Brake` is the **actual brake pedal position 0..1 as LFS sees it**, after
LFS has merged every input source. That makes intervention verifiable instead of
hopeful: we can always tell whether what we sent arrived. Use it to

- confirm an injected key or a virtual axis actually produced braking,
- **probe axis numbers automatically** (§3.2) instead of asking the user for them,
- detect that an intervention is not working and refuse to arm rather than pretend.

Caveat from `conventions.md` §5: OutGauge describes the car the *camera* is on. Gate
everything on `own_vehicle.is_local_driver` before believing it.

---

## 3. The two actuation paths

### 3.1 mouse_kb — key injection, bound to the driver's own key

**The binding must be pushed, not guessed.** An injected `s` only brakes if `s` is what
LFS has bound to brake. `/key <our configured key> brake` at startup makes the app's
setting and LFS definitionally consistent and removes the "user forgot to set it"
failure mode entirely.

**Bind our injected key to the *driver's own* brake key — never to a private key.** A
tempting alternative is to move LFS's brake onto a key only we press and forward the
driver's input to it, giving us total control. **Rejected: it breaks §1's fail-safe.**
If our process dies, hangs, or is killed, the driver has no brake at all, and every
normal brake application picks up a pynput -> SendInput -> LFS latency on the way. With
the binding on the driver's own key we are never in the path: the hardware reaches LFS
directly, and our death changes nothing for the driver.

**Injected and physical key events are distinguishable.** Windows sets
`LLKHF_INJECTED (0x10)` in `KBDLLHOOKSTRUCT.flags` for everything coming from
`SendInput`/`keybd_event`; genuine hardware never has that bit. Measured:

```
injected keydown  flags=0x10   injected keyup  flags=0x90
physical keydown  flags=0x00   physical keyup  flags=0x80
```

pynput's high-level `on_press`/`on_release` do **not** expose this — you must use
`keyboard.Listener(win32_event_filter=...)`, whose `data` argument is the raw
`KBDLLHOOKSTRUCT`. This is what makes the arbitration below possible at all.

**The key-release trap** is the defect that makes naive keyboard intervention unsafe.
Injected events and the physical keyboard are two state machines that LFS merges into
one, so our *release* is indistinguishable from the driver's:

```
driver physically holds "s"        (wants to brake)
AEB engages, injects   keyDown s   (no-op, LFS already sees it down)
hazard clears, AEB     keyUp   s   <- LFS now believes brake is RELEASED
driver is still physically holding "s"  -> but the car is no longer braking
```

Rules that follow, all resting on the physical-state tracking above:

- **Never issue a `keyUp` while the physical key is down.** Suppress it; the real
  release arrives when the driver lets go, and LFS's state stays truthful.
  **This is a rule about the brake, not about injection in general.** The same
  keystroke has opposite meanings on the two functions: an injected release
  takes away *braking the driver commanded* — forbidden — or *throttle the
  driver commanded*, which is precisely what a throttle cut is for. The
  throttle path therefore does exactly what this line forbids, on purpose, and
  presses the input again on handback only if the driver never let go
  (`Controls/throttle_cut.py`, `known-issues.md` #46). Before copying either
  rule to a new function, work out which way its sign points.
- **Re-press when the driver releases during an intervention.** The inverse case: the
  driver lets go while AEB still wants brake — LFS gets the genuine keyup, so we must
  inject a fresh keydown immediately. For emergency braking, continuing to brake is
  correct; for cruise control it is not. Decide per feature and write it down.
- Injection must additionally be blocked by all the guards in `ui.md` §1.4 —
  `text_entry`, `dialog`, held Shift, window focus, `on_track`.
- The result is digital, so modulation needs duty cycling; verify the achieved
  deceleration through `OutGaugePack.Brake` rather than assuming full braking.

### 3.2 wheel_js — a virtual analog axis (vJoy)

Keys do not work in this mode (§2.1), so an analog axis is the only option:

1. A **virtual joystick (vJoy)** publishes the desired brake value.
2. `/axis <vjoy_axis> brake` switches LFS's brake input to the virtual device.
3. While engaged, the assistant writes the required force to the virtual axis.
4. When the **driver's own brake input exceeds the virtual one**, control is handed back
   with `/axis <user_axis> brake`.

The order inside each step matters. On engage the value is written **first** — the vJoy
write is local and instant, the `/axis` command is a TCP round trip away, so the correct
value is already waiting when LFS switches. On release LFS is handed back **first**, so
if anything afterwards fails the driver already has their pedal.

**Measured, and it decides the design** (LFS 0.8C26, vJoy 2.1.9, `misc/vjoy_device.py`):

```
LFS axis numbers found by differential probing:  driver pedal 12, vJoy 15
response curve, inverted and linear:
    vjoy 0.00 -> LFS brake 1.000     vjoy 0.75 -> LFS brake 0.222
    vjoy 0.25 -> LFS brake 0.778     vjoy 1.00 -> LFS brake 0.000
    vjoy 0.50 -> LFS brake 0.500
feeder gone, axis left at full brake:
    after ResetVJD        -> LFS brake 1.000   (no effect)
    after RelinquishVJD   -> LFS brake 1.000   (no effect)
    2 s later             -> LFS brake 1.000
```

Two consequences, both non-obvious:

**The polarity is per-machine and must be measured, never assumed.** Here raw 0 is *full
brake*. Getting the sign wrong writes full braking where idle was meant. The two raw
endpoints are therefore stored as measured values (`vjoy_raw_no_brake`,
`vjoy_raw_full_brake`) and `AxisBrakeOutput` refuses to run until
`vjoy_brake_calibrated` says somebody measured them.

**vJoy holds the last value it was fed, forever.** Neither resetting the device nor
relinquishing it moves the axis, and an unfed vJoy axis that has never been written sits
at centre — which on this calibration is 50 % brake. So a process that dies *while
engaged* leaves LFS reading a braking value from a device nobody feeds, with the driver's
own pedal no longer assigned to brake. The car brakes until they fix it in the LFS
options. **No in-process measure can close this**: a dead process cannot send `/axis`,
and no polarity trick helps either, because the value frozen in is by definition the one
we were commanding.

The implementation narrows the window — the swap lasts only the intervention, the axis
is parked at "no brake" the instant we hand back, and every exit path hands back — and
then closes it from outside with **`guardian.py`**, a separate process:

```
main app  --spawns-->  guardian  --waits on PID-->  /axis <axis> brake
```

It is started as soon as the axis path arms (not at the first intervention — by then it
is too late to pay for process creation), speaks just enough InSim to send one command,
and imports nothing from the main app, so a half-torn-down package cannot break it.

Two details that make it safe rather than merely present:

- **A handover marker file decides whether it acts at all.** `AxisBrakeOutput` writes
  `brake_axis_held.marker` when it takes the axis and deletes it when it hands back.
  Present after the main process is gone means "died holding". Without that check a
  clean shutdown would force `brake` onto whatever number the settings hold — and if
  that number were wrong, every exit would break a working configuration.
- **The axis number comes from the marker**, with `settings.json` as the fallback. It
  differs per user and can be recalibrated; the marker carries the number that was in
  force when the swap happened, which is the only one that restores what was taken.

Note the `Size` field if you ever touch its packets: since InSim v9 it is the byte count
**divided by four** (IS_ISI is 44 bytes and carries 11, IS_MST is 68 and carries 17).
Sending the byte count makes LFS drop the connection without a word.

**Why vJoy and not something else.** A device LFS sees over DirectInput must be
enumerated by Windows as a HID device, which requires a **kernel-mode bus driver**. No
user-mode API can create one, so "emulate the controller ourselves" means writing and
Microsoft-signing our own driver. The only two realistic products are vJoy and ViGEmBus
(`pip install vgamepad`, which bundles its installer).

**ViGEmBus was evaluated and rejected**: its virtual pad exists only while the creating
process lives. That means LFS must be started after us, and — the deciding point — a
restart of PACT during a running LFS session makes the pad vanish and reappear, after
which LFS's brake-axis assignment is silently wrong. vJoy's devices are created by the
driver at boot and are always present, so axis numbering is stable and the start order
is free. Robustness (`AGENTS.md` §3) beats the slightly nicer install.

**The axis numbers cannot be discovered by sweeping — verify them, do not search.**
They live in settings (`user_axis_brake`, `vjoy_axis_1`) and hand-entering a wrong number
means `/axis` points at something that is not the vJoy device and the driver can lose
brake control. The obvious fix — walk `/axis N brake` over all 32 candidates and watch
`OutGaugePack.Brake` — **works and must never be used**: it identifies both axes
unambiguously *and* destroys every other axis assignment on the way (see the box in §2.2).

Two things were learned doing it, both worth keeping:

- **Compare by difference, never by absolute reading.** Looking for "brake > 0.5 while
  the pedal is held" produced 29 hits out of 32. An axis LFS knows about that sits at
  its centre reads 0.5 and one at its maximum reads 1.0, so the absolute value says
  nothing about which axis the driver is moving. Sweeping twice — pedal held vs
  released, vJoy at min vs max — and taking the largest delta gave exactly one axis each
  time, both matching the configured numbers.
- **That comparison is still legitimate for *verification*, which touches only `brake`.**
  Point `brake` at the candidate, ask the driver to press their pedal, and read OutGauge.
  If the number is right, `brake` returns to the axis that already had it and nothing
  else is disturbed; if it is wrong, the driver is asked again rather than left with a
  silently broken configuration.

So the flow is: the user reads both numbers off LFS's own controls screen, the app
**verifies** them non-destructively, and **refuses to arm** while unverified. Failing
loudly at setup beats failing silently at 120 km/h — and beats wiping their wheel
configuration to save them one number.

**The user's number can be pre-filled from disk instead of typed.** LFS stores the axis
assignment for every function in `data\misc\<Device>.csf`, and that file has been decoded
— `lfs-config-files.md`. Reading it touches nothing and needs no InSim connection. Two
limits decide how it may be used:

- The stored value is a **device-local** index, not the number `/axis` takes. On this
  install every function is stored exactly one below its `/axis` number (`brake` 11 vs
  12), but the offset depends on LFS's device enumeration and is not yet computable for
  a machine with several controllers.
- So the file yields a **candidate**, and the non-destructive verification above still
  runs on it. It replaces "ask the user for a number they may read wrong" with "propose
  the right number and confirm it" — it does not replace the confirmation.

The same file answers, with no offset problem, whether `brake`, `clutch` and `handbrake`
are on an axis at all — which is the question behind #42.

#### Open problem: installing vJoy wipes the driver's LFS controller setup

This, not the driver install itself, is the real reason users call vJoy unfriendly.
When a device LFS has not seen before appears, LFS treats it as "the user plugged in
new hardware and wants to reconfigure", and **discards the existing controller
assignments** — wheel axes, pedals, button mapping, force feedback, all of it. A
wheel user pays for our feature by rebuilding their entire control setup by hand.

It happens exactly once, when vJoy is installed, because vJoy's devices then exist
permanently (§3.2). That makes it fixable in principle, and the shape of the fix is a
backup/restore around the install:

- LFS keeps the configuration in `C:\LFS\cfg.txt` (`Control Mode`) and one binary file
  per device under `C:\LFS\data\misc\<Device Name>.csf` (`.con` is the same format at an
  older version and 0.8C26 no longer writes it). Each file is a **complete** snapshot of
  the whole control setup, not that device's slice. Decoded in `lfs-config-files.md`.
- LFS writes them **on exit**, and only for devices whose configuration changed — not
  when a `/key` or `/axis` command arrives. Measured: a `/axis` sweep left every file
  byte-identical to a copy taken beforehand, for hours.
- So: copy `cfg.txt` and `data\misc\*.csf`/`*.con` **before** installing vJoy, let LFS
  start once so it registers the new device, then restore the driver's own files.

The restore is now **more likely to work than it looked**: the numbers inside are
device-local indices, not positions in the enumerated device list, so growing the device
set should not invalidate them (`lfs-config-files.md` §4.1 — inferred from the 2006
preset files Scavier shipped, not proved). Two things are still unverified: whether LFS
re-reads a restored file for a device it already knows, and whether the numeric suffix in
the **filename** (`FANATEC_Wheel_3`) shifts when a device is added — if it does, the
restore has to follow the rename. `lfs-config-files.md` §7 names the experiment. Until
someone runs it, the honest thing to tell a wheel user is that installing vJoy will cost
them their LFS controller settings once.

One useful side effect of the write-on-exit behaviour: after an accident like the `/axis`
sweep, the good configuration is **still on disk** until LFS exits. Copying the folder is
the first move, before closing the game.

### 3.3 Steering is a separate, larger problem

Everything above concerns a single scalar (brake). Steering intervention additionally
needs continuous blending, torque-conflict handling with a force-feedback wheel, and a
stability strategy — nothing in this project addresses it. Do not attempt lane keeping
by extending the brake mechanism.

---

## 4. Checklist before any intervention feature is armed

- [ ] The right path for the active control mode (§2.1) — key injection for `mouse_kb`,
      virtual axis for `wheel_js`; never assume one covers both
- [ ] Input path verified at runtime: axis numbers probed through OutGauge, or binding
      pushed with `/key`
- [ ] Arbitration rule written down; the assistant can only add braking, never remove it
- [ ] Handback tested, including from an exception and from process shutdown
- [ ] Physical key state tracked via `LLKHF_INJECTED`; no release while physically held
- [ ] All guards from `ui.md` §1.4 applied
- [ ] The driver is told, visibly, that an intervention is active
- [ ] Behaviour defined for spectating / wrong PLID (`conventions.md` §5.2) — never
      actuate based on a car that is not the one being driven
- [ ] Deceleration request is physically defensible (`conventions.md` §7)
