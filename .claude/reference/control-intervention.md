# Taking control away from the driver

Design reference for any feature that actuates the car rather than only warning:
automatic emergency braking, cruise control / ACC, lane keeping, auto-hold, the
automatic gearbox.

**Automatic braking intervention is currently disabled on purpose** and must not be
re-enabled without being asked (`CLAUDE.md` §5). This file records what it would take to
do it properly, so the next attempt does not repeat the first one's mistakes.

Treat everything here with the ECU mindset from `CLAUDE.md` §1: an intervention that
works 95 % of the time is not a feature, it is a hazard.

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

## 2. Wheel / joystick — the working approach

This is the approach that actually works and is implemented (though disabled) in
`assistance/controller_emulator.py` + `Controls/wheel.py`:

1. A **virtual joystick (vJoy)** publishes the desired brake/throttle value.
2. `/axis <vjoy_axis> brake` switches LFS's brake input to the virtual device.
3. While engaged, the assistant writes the required force to the virtual axis.
4. When the **driver's own brake input exceeds the virtual one**, control is handed back
   with `/axis <user_axis> brake`.

Why it works: the arbitration is a plain `max()` on an analog value, the handback is a
single command, and if our process dies the worst case is that LFS is left pointing at a
stale virtual axis — which is exactly why the handback must also fire on shutdown.

**Its one hard requirement: we must know both axis numbers** — the user's real axis and
the vJoy axis. These live in settings (`user_axis_brake`, `user_axis_throttle`,
`user_axis_steering`, `user_axis_clutch`, `vjoy_axis_1`) and are **set by the user**. If
the user never sets them, the whole feature silently does the wrong thing: `/axis`
switches to an axis that is not the vJoy device, and the driver may lose brake control.

Consequences for the implementation:
- The axis numbers must be **validated before the feature can be enabled**, not assumed.
  A calibration flow (like the gearbox's) that asks the user to move each axis and reads
  back which one changed is the right shape.
- The feature must **refuse to arm** when the configuration is unverified. Failing loudly
  at setup beats failing silently at 120 km/h.
- The handback `/axis` must be issued on every exit path, including shutdown and
  exceptions — see `known-issues.md` #2, shutdown is currently a no-op.
- `/control wheel_js` selects the controller type, `/invert` handles inverted axes and
  `/axis -1 <function>` unassigns. See `C:\LFS\docs\Commands.txt`.

## 3. Mouse and keyboard — substantially harder

Keyboard and mouse brake input is **digital**, so there is no analog value to arbitrate
on, and the current mechanism is `pyautogui` key injection.

### 3.1 We must know the user's binding

An injected `s` only brakes if `s` is what the user has bound to brake in LFS. The
`user_*_key` settings are the app's *guess* at that binding and nothing verifies it.

**LFS can be told the binding instead of asked**: `/key <key> <function>`, e.g.
`/key Q handbrake`. Valid keys are `A–Z`, `0–9`, `F1–F12`, `up/down/left/right`,
`space/enter/esc/tab`, `less/more/minus/plus`. Pushing our configured key into LFS makes
the two definitionally consistent and removes the "user forgot to set it" failure mode
entirely. This is unused today and is the single highest-value fix in this area.

### 3.2 The key-release trap

This is the defect that makes naive keyboard intervention unsafe.

Injected key events and the physical keyboard are two independent state machines that
LFS merges into one. The assistant can *press* a key, but its *release* is
indistinguishable from the user releasing it.

```
user physically holds  "s"        (wants to brake)
AEB engages, injects   keyDown s  (no-op, LFS already sees it down)
hazard clears, AEB     keyUp   s  ← LFS now believes brake is RELEASED
user is still physically holding "s"  → but the car is no longer braking
```

The driver is pressing the brake and the car will not brake, with no feedback explaining
why, until they release and press again. That is worse than having no assistant.

Rules that follow:

- **Never issue a `keyUp` for a key the user may be holding.** Track real hardware state
  with a `pynput` listener (a global hook sees genuine hardware events) and suppress our
  release while the physical key is down. LFS's state then still matches reality, and the
  real release arrives when the user lets go.
- **The inverse case needs a decision too**: the user releases while we are still
  intervening. For emergency braking, continuing to brake is correct. For cruise
  control, it is not. Decide per feature and write the decision down.
- Injection must additionally be blocked by all the guards in `ui.md` §1.4 —
  `text_entry`, `dialog`, held Shift, window focus, `on_track`.
- Prefer **not** to inject at all: a keyboard/mouse user could be routed through a vJoy
  axis like a wheel user, with the app reading their physical key via `pynput` and
  writing `max(user, assist)` to the axis. That removes the up/down state problem
  completely — but it puts our process in the only control path, which violates §1's
  fail-safe rule. Do not do this without a watchdog that restores `/axis` on our death.

### 3.3 Steering is a separate, larger problem

Everything above concerns a single scalar (brake). Steering intervention additionally
needs continuous blending, torque-conflict handling with a force-feedback wheel, and a
stability strategy — nothing in this project addresses it. Do not attempt lane keeping
by extending the brake mechanism.

## 4. Checklist before any intervention feature is armed

- [ ] Input path verified at runtime (axis numbers probed, or binding pushed with `/key`)
- [ ] Arbitration rule written down; the assistant can only add braking, never remove it
- [ ] Handback tested, including from an exception and from process shutdown
- [ ] Physical key state tracked if keys are injected; no release while physically held
- [ ] All guards from `ui.md` §1.4 applied
- [ ] The driver is told, visibly, that an intervention is active
- [ ] Behaviour defined for spectating / wrong PLID (`conventions.md` §5.2) — never
      actuate based on a car that is not the one being driven
- [ ] Deceleration request is physically defensible (`conventions.md` §7)
