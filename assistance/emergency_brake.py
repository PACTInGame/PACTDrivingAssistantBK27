"""Automatic emergency braking -- the part that actually moves the pedal.

``ForwardCollisionWarning`` decides *whether* a collision is imminent and how
much deceleration it would take to avoid it; this system decides whether to act
on that and owns the actuation. The split is deliberate: FCW stays a pure
warning system that anyone can enable, and everything that takes control away
from the driver lives here, behind ``automatic_emergency_brake == 2``.

LFS has no API for "apply brake" on a human-driven car, so we impersonate an
input device, and *which* device depends on the driver's LFS control mode
(``reference/control-intervention.md`` §2.1):

===============  ==============================  ==========================
control_mode     LFS mode                        output
===============  ==============================  ==========================
0 mouse          ``mouse_kb``                    :class:`KeyBrakeOutput`
1 keyboard       ``mouse_kb``                    :class:`KeyBrakeOutput`
2 wheel/joystick ``wheel_js``                    :class:`AxisBrakeOutput`
===============  ==============================  ==========================

These are not interchangeable: in ``wheel_js`` LFS ignores keys for brake
completely, and in ``mouse_kb`` there is no axis to write to. An output that
cannot be armed says so once, in the log and on screen, instead of running and
achieving nothing -- that silent-failure mode is what this rewrite exists to
remove.

Arbitration is the rule from §1: **we only ever add braking.** The driver's own
input reaches LFS on its own path and is never reduced, cancelled or filtered
by us. Releasing our share is therefore always safe and always allowed, even
when :class:`~misc.input_guard.InputGuard` would refuse a fresh press.

The key path is digital -- a key is down or up -- so it brakes fully or not at
all. The axis path is analog and runs a feed-forward plus a proportional
correction on the deceleration the car actually achieved.

Cost per cycle: a handful of comparisons and settings reads while idle. The
output is touched only while an intervention is running.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

from Controls.brake_axis import AxisBrakeOutput
from Controls.brake_key import KeyBrakeOutput
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc import input_guard
from misc.input_guard import InputGuard
from misc.physical_keys import PhysicalKeyState
from misc.platform_shim import get_keyboard
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle

logger = logging.getLogger(__name__)

# ``vehicle.data.control_mode`` (conventions.md §5.4). Mouse and keyboard are
# the same LFS control mode; only the PIF flag differs.
CONTROL_MODE_MOUSE = 0
CONTROL_MODE_KEYBOARD = 1
CONTROL_MODE_WHEEL = 2
MOUSE_KB_MODES = (CONTROL_MODE_MOUSE, CONTROL_MODE_KEYBOARD)

# ``automatic_emergency_brake``: 0 = off, 1 = warn only, 2 = warn and brake.
AEB_MODE_BRAKE = 2

# Refusals from ``InputGuard`` that also apply to the analog path. The guard was
# written for *keystrokes*: a held Shift turns an injected key into an LFS
# shortcut, an open chat line swallows it, and a keystroke follows the focused
# window. None of that is true of a joystick axis, and refusing an emergency
# brake because the driver happens to be holding Shift would be a safety
# regression, not a safety measure. What still applies is "is this car ours and
# are we driving it at all" (reference/ui.md §1.4).
AXIS_BLOCKING_REFUSALS = frozenset((
    input_guard.REASON_OFF_TRACK,
    input_guard.REASON_NO_VEHICLE,
    input_guard.REASON_NOT_LOCAL_DRIVER,
    input_guard.REASON_AI_CONTROLLED,
))


class EmergencyBrake(AssistanceSystem):
    """Applies the brake when FCW says a collision can no longer be avoided."""

    # Thresholds are deceleration demands in m/s², as published by FCW.
    #
    # ENGAGE sits at 6.0 rather than at FCW's own level-3 threshold (7.5) so
    # that this system carries its own physical floor instead of inheriting
    # one: on dry tarmac a road car reaches roughly 9-10 m/s², so a demand of
    # 6 m/s² is already two thirds of everything the tyres have, and no
    # attentive driver brakes that hard for anything but an emergency. Below
    # it, full braking would be a bigger hazard than the situation.
    #
    # The output available today is digital -- a key is down or up -- so there
    # is no partial braking to ramp into. Modulation arrives with the analog
    # (vJoy) path; until then the honest description of this system is
    # "full brake or nothing", and the thresholds are chosen for that.
    ENGAGE_DECELERATION_MS2 = 6.0
    RELEASE_DECELERATION_MS2 = 3.0
    # A demand below RELEASE has to persist before we let go: one noisy cycle
    # in the middle of an intervention must not drop the brake.
    RELEASE_DEBOUNCE_CYCLES = 2

    # Below this the car is stopping anyway and auto-hold takes over; holding
    # a digital full brake to standstill would just lock the wheels.
    STOP_SPEED_KMH = 3.0
    # Same floor as FCW: below it, a collision is a parking manoeuvre and
    # belongs to PDC, not here.
    MIN_SPEED_KMH = 10.0
    # Runaway guard. Nothing legitimate needs a ten-second emergency stop; if
    # we are still asking after that, the demand is wrong, not the situation.
    MAX_ENGAGE_S = 10.0
    # How often a failed hook installation is retried, in seconds.
    HOOK_RETRY_S = 5.0

    # Analog path only. Deceleration a road car reaches with the pedal on the
    # floor, dry tarmac, mu ~ 1.0 - the denominator of the feed-forward term.
    FULL_BRAKE_DECELERATION_MS2 = 10.0
    # Correction per m/s² of shortfall between demanded and achieved
    # deceleration. Small on purpose: the measurement is differentiated from
    # speed samples ~100 ms apart and is noisy, so this closes the gap over a
    # few cycles rather than chasing every sample.
    BRAKE_GAIN_PER_MS2 = 0.05
    # Once engaged we never command *nothing*: a momentarily flattering
    # deceleration reading must not lift the pedal mid-intervention.
    MIN_BRAKE_FRACTION = 0.2

    def __init__(self, event_bus: EventBus, settings: SettingsManager,
                 physical_keys: Optional[PhysicalKeyState] = None,
                 guard: Optional[InputGuard] = None, clock=None):
        super().__init__("automatic_emergency_brake", event_bus, settings)

        self.clock = clock or time.monotonic

        self.physical_keys = physical_keys or PhysicalKeyState()
        self._owns_physical_keys = physical_keys is None
        self.guard = guard or InputGuard(event_bus)
        self.key_output = KeyBrakeOutput(event_bus, settings, self.physical_keys)
        self.axis_output = AxisBrakeOutput(event_bus, settings)

        self._wanted_deceleration = 0.0
        self._engaged = False
        self._engaged_since = 0.0
        self._below_release_cycles = 0
        # One complaint per distinct reason, not one per cycle.
        self._reported_reason: Optional[str] = None
        self._binding_mode: Optional[int] = None
        self._hooks_tried_at: Optional[float] = None
        self._hooks_pending = False

        self.event_bus.subscribe('needed_deceleration_update', self._on_deceleration)
        self.event_bus.subscribe('lfs_connected', self._on_connection_changed)
        self.event_bus.subscribe('new_keybinding', self._on_keybinding_changed)
        self.event_bus.subscribe('state_data', self._on_state_data)

    # ─── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> bool:
        """Install the input hooks this system needs. Safe to call twice."""
        if not self._owns_physical_keys:
            return self.physical_keys.is_running()
        return self.physical_keys.start()

    def shutdown(self):
        """Give the brake back, unconditionally.

        The last thing that must still work when everything else is being torn
        down: an intervention that outlives the process would leave the driver
        with a brake nobody is holding on purpose.
        """
        try:
            self.key_output.release()
        finally:
            try:
                self.axis_output.shutdown()
            finally:
                self._engaged = False
                if self._owns_physical_keys:
                    self.physical_keys.stop()

    # ─── Events ───────────────────────────────────────────────────────

    def _on_deceleration(self, data):
        self._wanted_deceleration = float(data.get('deceleration', 0.0) or 0.0)

    def _on_connection_changed(self, data=None):
        # LFS may have restarted; a binding we pushed into the old session
        # proves nothing about this one.
        self.key_output.binding_lost()
        self._binding_mode = None

    def _on_state_data(self, data):
        """Leaving the track ends any intervention immediately.

        ``AssistanceManager`` stops calling ``process()`` once ``on_track``
        drops, so this is the only place that can still let go -- otherwise a
        driver who hits Shift+P mid-intervention keeps a pressed brake key.
        """
        if isinstance(data, dict) and not data.get('on_track', False):
            self._disengage()

    def _on_keybinding_changed(self, data):
        if isinstance(data, dict) and data.get('setting') == 'user_brake_key':
            self.key_output.binding_lost()
            self._binding_mode = None

    # ─── Main pass ────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        """Braking is armed only in mode 2; mode 1 is FCW's warning alone.

        Stays True while a press of ours is outstanding, whatever the setting
        says. ``AssistanceManager`` skips a disabled system entirely, so a mode
        switched off in the middle of an intervention would otherwise leave the
        brake key held with nobody left to release it.
        """
        if self._engaged or self.key_output.holds_press()                 or self.axis_output.holds_axis():
            return True
        return self._armed()

    def _armed(self) -> bool:
        """The setting alone, without the "must still let go" exception."""
        return self.enabled and \
            self.settings.get('automatic_emergency_brake') == AEB_MODE_BRAKE

    def process(self, own_vehicle: OwnVehicle,
                vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        # Bind once: OutGauge writes into own_vehicle from the packet thread
        # (known-issues #12).
        own = own_vehicle.data

        # Not ``is_enabled()``: that one reports True while a press of ours is
        # outstanding, precisely so this pass still runs and can release it.
        if not self._armed():
            self._disengage()
            return {'active': False}

        # Binding first: ``_output_for`` refuses an output whose binding was
        # never pushed, so asking it first would refuse forever -- the push
        # sits behind the refusal that the push is supposed to clear.
        self._ensure_binding(own_vehicle, own.control_mode)

        output = self._output_for(own.control_mode)
        if output is None:
            self._disengage()
            return {'active': False}

        wanted = self._wants_brake(own)
        if wanted and not self._engaged:
            refusal = self._refusal_for(output, own_vehicle)
            if refusal is not None:
                self._disengage()
                return {'active': False, 'refused': refusal}
            self._engage()
        elif not wanted and self._engaged:
            self._disengage()

        if not self._engaged:
            return {'active': False}

        fraction = self._brake_fraction(own)
        output.apply(fraction)
        return {'active': True,
                'deceleration': self._wanted_deceleration,
                'brake': fraction}

    def _refusal_for(self, output, own_vehicle) -> Optional[str]:
        """May this output start an intervention right now?

        The guard's full table is about *keystrokes* (ui.md §1.4). For the
        analog path only the subset in ``AXIS_BLOCKING_REFUSALS`` is
        meaningful -- a joystick axis does not care about window focus, an
        open chat line or a held Shift, and refusing to brake for one of those
        would be a safety regression rather than a safety measure.
        """
        refusal = self.guard.may_inject(own_vehicle)
        if refusal is None:
            return None
        if output is self.axis_output and refusal not in AXIS_BLOCKING_REFUSALS:
            return None
        return refusal

    def _brake_fraction(self, own) -> float:
        """How hard to press, 0..1.

        Feed-forward plus a proportional correction on what the car actually
        achieved. Assumptions, stated because they have to be (``CLAUDE.md``
        §2): a road car on dry tarmac (mu ~ 1.0) reaches about
        ``FULL_BRAKE_DECELERATION_MS2``, and pedal travel maps roughly linearly
        to deceleration below lockup. Both are approximations, which is exactly
        why the measured deceleration corrects them instead of being trusted.

        ``own.acceleration`` is signed with negative meaning braking
        (``conventions.md`` §3), so the achieved deceleration is its negation.

        The digital key path ignores the value beyond "is it above zero" -- a
        key has no travel.
        """
        feed_forward = (self._wanted_deceleration
                        / self.FULL_BRAKE_DECELERATION_MS2)
        error = self._wanted_deceleration - (-own.acceleration)
        fraction = feed_forward + error * self.BRAKE_GAIN_PER_MS2
        return max(self.MIN_BRAKE_FRACTION, min(1.0, fraction))

    # ─── Decisions ────────────────────────────────────────────────────

    def _output_for(self, control_mode: int):
        """The actuation path for this control mode, or None with one report.

        A wheel driver has no path yet. That is a missing feature, and it is
        said out loud rather than swallowed -- the previous version simply did
        nothing here, which is how "I get a warning but it never brakes" came
        about in the first place.
        """
        if control_mode in MOUSE_KB_MODES:
            reason = self.key_output.unavailable_reason()
            if reason == 'no_physical_key_tracking':
                # Install the hooks on demand rather than at startup: they are
                # a system-wide side effect, and a driver who never turns
                # automatic braking on should never get them. Retried slowly,
                # because the setting can be switched on at any time.
                self._try_start_hooks()
                reason = self.key_output.unavailable_reason()
                if reason == 'no_physical_key_tracking' and self._hooks_pending:
                    # Still coming up. Not a fault worth shouting about -- it
                    # clears itself within a cycle or two, and reporting it
                    # would put a scary "AEB unavailable" on screen every
                    # single startup.
                    return None
            if reason is None:
                self._reported_reason = None
                return self.key_output
            self._report_unavailable(reason)
            return None

        reason = self.axis_output.unavailable_reason()
        if reason is None:
            self._reported_reason = None
            # Start the watchdog now, not at the first intervention: by then it
            # is too late to pay for process creation, and it only helps if it
            # is already running when we die.
            self.axis_output.start_guardian()
            return self.axis_output
        self._report_unavailable(reason)
        return None

    def _try_start_hooks(self):
        """Install the input hooks, at most once every HOOK_RETRY_S.

        Off the assistance thread: installing the two ``pynput`` listeners
        takes over 100 ms, which is a whole cycle budget (``CLAUDE.md`` §1).
        Done inline it produced a "100 ms cycle overran its budget: 141.0 ms"
        on the first pass. Nothing waits for the result -- the next cycle sees
        ``is_running()`` and arms then.
        """
        now = self.clock()
        if self._hooks_tried_at is not None and \
                now - self._hooks_tried_at < self.HOOK_RETRY_S:
            return
        self._hooks_tried_at = now
        self._hooks_pending = True
        threading.Thread(target=self._start_hooks_off_thread, name='aeb-hooks',
                         daemon=True).start()

    def _start_hooks_off_thread(self):
        try:
            # Warm pyautogui here too: importing it costs ~255 ms, and the
            # first ``is_available('pyautogui')`` would otherwise pay for it
            # on the assistance thread.
            get_keyboard()
            self.start()
        finally:
            self._hooks_pending = False

    def _wants_brake(self, own) -> bool:
        """Hysteresis around FCW's deceleration demand."""
        if own.speed < (self.STOP_SPEED_KMH if self._engaged
                        else self.MIN_SPEED_KMH):
            return False

        if not self._engaged:
            self._below_release_cycles = 0
            return self._wanted_deceleration >= self.ENGAGE_DECELERATION_MS2

        if self.clock() - self._engaged_since > self.MAX_ENGAGE_S:
            logger.warning("Emergency brake held for more than %.0f s - "
                           "releasing; the deceleration demand (%.1f m/s²) "
                           "is not plausible.",
                           self.MAX_ENGAGE_S, self._wanted_deceleration)
            return False

        if self._wanted_deceleration < self.RELEASE_DECELERATION_MS2:
            self._below_release_cycles += 1
            return self._below_release_cycles < self.RELEASE_DEBOUNCE_CYCLES

        self._below_release_cycles = 0
        return True

    def _ensure_binding(self, own_vehicle: OwnVehicle, control_mode: int):
        """Push ``/key <brake key> brake`` once we know whose car this is.

        Deliberately not done at startup: before IS_NPL there is no local
        driver and no known control mode, and rewriting a wheel driver's key
        bindings for a path they will never use is a side effect nobody asked
        for.
        """
        if control_mode not in MOUSE_KB_MODES:
            return
        if self._binding_mode == control_mode:
            return
        if not own_vehicle.local_plid:
            return
        if self.key_output.push_binding():
            self._binding_mode = control_mode

    # ─── Engagement ───────────────────────────────────────────────────

    def _engage(self):
        """Mark the intervention started and tell the driver it is happening."""
        self._engaged = True
        self._engaged_since = self.clock()
        self._below_release_cycles = 0
        logger.info("Emergency brake engaged (demand %.1f m/s²).",
                    self._wanted_deceleration)
        # Only the state event. Deliberately *not* a ``notification``: those are
        # queued and shown one at a time for 3 s, so the driver saw "!! BRAKE !!"
        # about a second after the braking had already finished, and repeated
        # interventions stacked up behind each other. An intervention indicator
        # has to be live, so it is drawn from this event instead.
        self.event_bus.emit('emergency_brake_changed', {'active': True})

    def _disengage(self):
        """Drop our share of the brake and forget the engagement.

        Called from every path that stops wanting the brake, including the
        disabled and unavailable ones, so a mode change or a setting flipped
        mid-intervention cannot strand a pressed key.
        """
        self.key_output.release()
        self.axis_output.release()
        self._below_release_cycles = 0
        if not self._engaged:
            return
        self._engaged = False
        logger.info("Emergency brake released.")
        self.event_bus.emit('emergency_brake_changed', {'active': False})

    def _report_unavailable(self, reason: str):
        if self._reported_reason == reason:
            return
        self._reported_reason = reason
        logger.warning("Automatic emergency braking is switched on but cannot "
                       "be armed: %s", reason)
        self.event_bus.emit('notification',
                            {'notification': f"^1AEB unavailable: {reason}"})
