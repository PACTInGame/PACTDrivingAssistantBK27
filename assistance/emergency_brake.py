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

Arbitration is the rule from §1: **we only ever add braking.** Releasing our
share is therefore always safe and always allowed, even when
:class:`~misc.input_guard.InputGuard` would refuse a fresh press.

That rule is free on the key path -- LFS merges our keystroke with the driver's
own input and the harder one wins -- but it is **not** free on the axis path.
While the vJoy axis carries ``brake``, LFS has stopped reading the driver's
pedal altogether, so a command of 50 % really is 50 % even if they are standing
on it. So the axis path arbitrates explicitly against
:class:`~misc.pedal_watch.PedalWatch`, which reads the pedal at the device, and
commands ``max(ours, theirs)``. Where the pedal cannot be read at all it
commands full braking instead: the one thing it may never do is guess low.

Throttle is taken away for the duration (``Controls/throttle_cut.py``). Braking
against a pulling engine costs about a third of the deceleration, and every
production AEB closes the throttle first.

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

from Controls.brake_axis import AxisBrakeOutput, REASON_LOADING
from Controls.brake_key import KeyBrakeOutput
from Controls.handover_marker import HandoverMarker
from Controls.throttle_axis_check import ThrottleAxisCheck
from Controls.throttle_cut import AxisThrottleCut, KeyThrottleCut
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc import input_guard
from misc.input_guard import InputGuard
from misc.lfs_config import axis_assignments
from misc.pedal_watch import PedalWatch
from misc.physical_keys import PhysicalKeyState, get_physical_keys
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

# Which system a ``needed_deceleration_update`` came from, when it does not say.
# Only ``ForwardCollisionWarning`` published this event before several systems
# did, so an unlabelled demand is its.
DEFAULT_DEMAND_SOURCE = 'forward_collision'

# "Never checked yet" -- see ``EmergencyBrake._publish_availability``. Not
# ``None``, because ``None`` is a real state there ("armed").
_UNKNOWN = object()

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
    # Not a keystroke question either: without OutGauge nothing knows whose
    # car the camera is on, and the axis path additionally reads the driver's
    # own pedal through it. Blind on both counts.
    input_guard.REASON_NO_OUTGAUGE,
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

    # Same floor as FCW: below it, a collision is a parking manoeuvre and
    # belongs to PDC, not here. It is also the speed below which FCW stops
    # publishing a demand at all, which is why it appears twice below.
    MIN_SPEED_KMH = 10.0
    # ...but that reasoning is about *longitudinal* traffic. A car creeping
    # into a junction at 8 km/h in front of crossing traffic, or edging into
    # the next lane while something overtakes at 50 km/h, is not performing a
    # parking manoeuvre -- the energy in the crash belongs to the other car,
    # and our speed only decides whether we are in its way. So the demands
    # that come from those two systems carry a floor low enough to be out of
    # OutGauge's noise and nothing more.
    CROSSING_DEMAND_SOURCES = frozenset(('cross_traffic', 'blind_spot'))
    MIN_SPEED_CROSSING_KMH = 3.0
    # ─── Anhalten bis zum Stillstand ──────────────────────────────────
    #
    # Under MIN_SPEED_KMH, FCW publishes a demand of 0 whatever is in front of
    # us -- it stops evaluating there. An emergency stop that is still running
    # at that speed therefore used to see its demand collapse to zero and let
    # go at around 5-8 km/h, a metre or two short of the obstacle it had just
    # braked for. That is the worst possible place to hand back.
    #
    # So an intervention that reaches this speed is *committed*: it brakes to
    # a standstill and only then hands over. The remaining energy is trivial
    # (10 km/h is 4 % of the kinetic energy at 50 km/h, about 1.1 m of braking
    # distance at 3 m/s²), the driver can add brake at any time but never has
    # ours taken away, and stopping short of an obstacle is never the wrong
    # outcome. Above this speed the normal hysteresis still releases us as
    # soon as the situation resolves.
    COMMIT_TO_STOP_SPEED_KMH = MIN_SPEED_KMH
    # Stillstand. Deutlich ueber dem Rauschen von OutGauge, deutlich unter
    # jedem Kriechen (0.3 km/h = 8 cm/s).
    STANDSTILL_KMH = 0.3
    # Und die Schwelle, ab der der Wagen als *wieder fahrend* gilt. Zwei
    # Schwellen, nicht eine: gemessen im Log vom 2026-09-20 wurde
    # "standstill reached" in derselben Sekunde zweimal geschrieben. Eine
    # Karosserie, die sich nach einer Vollbremsung ausfedert, ueberschreitet
    # 0.3 km/h noch ein paar Mal, und jede Ueberschreitung setzte die
    # Uebergabefrist zurueck - die Bremse blieb laenger als STANDSTILL_HOLD_S
    # drin, im Grenzfall beliebig lange.
    #
    # 2.0 km/h trennt Ausfedern von Wegrollen physikalisch: an 5 % Steigung
    # wirken a = g*sin(atan(0.05)) ~ 0.49 m/s², nach einer Sekunde also
    # 0.49 m/s = 1.8 km/h. Ein Wagen, der wirklich anrollt, ist innerhalb der
    # Haltezeit darueber; Ausfedern ist es nie.
    STANDSTILL_EXIT_KMH = 2.0
    # Danach die Bremse noch so lange halten. ``AutoHold`` zieht die
    # Handbremse erst, wenn es *gleichzeitig* Stillstand und einen getretenen
    # Bremsdruck sieht - liessen wir im selben Zyklus los, in dem der Wagen
    # steht, rollte er an einer Steigung wieder an.
    STANDSTILL_HOLD_S = 1.0
    # Gas des Fahrers, ab dem die committed-Phase abgebrochen wird. Kein
    # Schwellwert gegen Rauschen allein: ein Pedal in Ruhelage meldet je nach
    # Kalibrierung ein paar Prozent, ein Tastatur-Gas meldet 0 oder 1.
    THROTTLE_OVERRIDE = 0.15
    # Wie lange nach einer Gas-Uebergabe kein neuer Eingriff beginnt
    # (known-issues #49). Der gemessene Fall: der Eingriff bremst unter
    # COMMIT_TO_STOP_SPEED_KMH, der Fahrer gibt Gas, wir uebergeben, das Auto
    # beschleunigt ueber FCWs 10-km/h-Schwelle, dieselbe Anforderung kommt
    # zurueck -- **acht Eingriffe in 4.5 s**. Die Bremse nehmen und sofort
    # wieder zurueckgeben ist schlechter als beides einzeln
    # (control-intervention.md section 3).
    #
    # Die Sperre haengt an zwei Bedingungen, und beide sind noetig:
    #
    # * Sie gilt nur, solange der Fahrer **weiter Gas gibt**. Nimmt er den
    #   Fuss herunter, ist die Uebergabe nicht mehr sein Wille, und ein
    #   bestehender Konflikt darf uns sofort wieder scharf machen.
    # * Sie laeuft nach dieser Zeit in jedem Fall ab. Ein AEB, das sich durch
    #   getretenes Gas dauerhaft abschalten laesst, ist keines -- Panikgas ist
    #   genau der Fall, fuer den es existiert.
    #
    # Was die Sperre im schlechtesten Fall kostet: 1.5 s ohne Eingriff. Der
    # Fahrer beschleunigt dabei aus hoechstens 10 km/h und hat den Weg
    # ausdruecklich fuer frei erklaert.
    THROTTLE_HANDBACK_LOCKOUT_S = 1.5
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
                 guard: Optional[InputGuard] = None, clock=None,
                 pedals: Optional[PedalWatch] = None):
        super().__init__("automatic_emergency_brake", event_bus, settings)

        self.clock = clock or time.monotonic

        # The shared tracker (``misc/physical_keys.get_physical_keys``), so
        # that the parking manoeuvre and this system install the low-level
        # hooks once between them rather than once each. Ownership therefore
        # stays with nobody: a system that stopped the shared tracker on its
        # own shutdown would blind the other one.
        self.physical_keys = physical_keys or get_physical_keys()
        self._owns_physical_keys = False
        self.guard = guard or InputGuard(event_bus)
        # One marker for the whole intervention: the brake half and the
        # throttle half both have something for the guardian to undo, and both
        # have to end up in the same file.
        self.marker = HandoverMarker()
        self.key_output = KeyBrakeOutput(event_bus, settings, self.physical_keys)
        self.axis_output = AxisBrakeOutput(event_bus, settings,
                                           marker=self.marker)
        self.key_throttle = KeyThrottleCut(event_bus, settings, self.marker,
                                           physical=self.physical_keys)
        self.pedals = pedals if pedals is not None else PedalWatch(event_bus,
                                                                   settings)
        self.axis_throttle = AxisThrottleCut(event_bus, settings, self.marker,
                                             pedals=self.pedals,
                                             lfs_axes=self._read_lfs_axes)
        self.throttle_check = ThrottleAxisCheck(event_bus, settings,
                                                self.pedals, self.axis_throttle)

        # Sollverzoegerung je Quelle, gesammelt waehrend eines
        # Assistenzdurchlaufs und am Ende davon geleert -- siehe
        # ``_collect_demand``.
        self._demands: Dict[str, float] = {}
        self._wanted_deceleration = 0.0
        self._demand_source: Optional[str] = None
        self._engaged = False
        self._engaged_since = 0.0
        self._below_release_cycles = 0
        # Der Eingriff hat sich auf einen Halt festgelegt (siehe
        # COMMIT_TO_STOP_SPEED_KMH), und seit wann das Auto steht.
        self._stopping = False
        self._standstill_since: Optional[float] = None
        # Wann zuletzt wegen Fahrergas uebergeben wurde. Ueberlebt
        # ``_disengage`` absichtlich -- die Uebergabe *ist* das Ereignis, das
        # die Sperre setzt (THROTTLE_HANDBACK_LOCKOUT_S).
        self._throttle_handback_at: Optional[float] = None
        self._lockout_logged = False
        # Groesste Sollverzoegerung dieses Eingriffs. Sie traegt die
        # committed-Phase, in der es keine mehr zu lesen gibt.
        self._peak_deceleration = 0.0
        # One complaint per distinct reason, not one per cycle. The sentinel
        # is deliberately not ``None``: ``None`` is a real state ("armed"), and
        # it has to be published once too, or the menu never learns that a
        # previously reported fault is gone.
        self._reported_reason: Any = _UNKNOWN
        self._binding_mode: Optional[int] = None
        self._hooks_tried_at: Optional[float] = None
        self._hooks_pending = False
        # One line about a throttle cut that cannot be armed, not one per
        # intervention.
        self._throttle_reason_reported: Any = _UNKNOWN
        self._blind_arbitration_reported = False
        self._driver_mode = None

        self.event_bus.subscribe('needed_deceleration_update', self._on_deceleration)
        self.event_bus.subscribe('lfs_connected', self._on_connection_changed)
        self.event_bus.subscribe('new_keybinding', self._on_keybinding_changed)
        self.event_bus.subscribe('state_data', self._on_state_data)
        self.event_bus.subscribe('player_name_changed', self._on_player_changed)
        self.event_bus.subscribe('emergency_brake_mode_requested', self._on_mode_requested)

    def on_own_vehicle_updated(self, own_vehicle):
        # O(1); metadata follows NPL/PFL independently of the camera.
        self._driver_mode = own_vehicle.data.control_mode

    def _on_mode_requested(self, data):
        """Validate a menu request even while warning-only mode skips process()."""
        if not isinstance(data, dict):
            return
        if not data.get('enabled'):
            self.settings.set('automatic_emergency_brake', 1)
            return
        reason = None
        if self._driver_mode is None:
            reason = 'control_mode_unknown'
        elif self._driver_mode not in MOUSE_KB_MODES:
            device = self.axis_output.device
            reason = (device.unavailable_reason() if device.prepare()
                      else REASON_LOADING)
            if reason is None:
                reason = self.axis_output.unavailable_reason()
        if reason is not None:
            self._publish_availability(reason)
            self.event_bus.emit('emergency_brake_enable_refused', {'reason': reason})
            return
        self.settings.set('automatic_emergency_brake', AEB_MODE_BRAKE)

    def _read_lfs_axes(self):
        """LFS's own axis assignments, anchored on the brake axis we know.

        The brake number is the one we are entitled to be sure of: every
        intervention hands the brake back to it and the driver's pedal works
        afterwards. It picks the driver's device out of the folder and fixes
        the offset between what the file stores and what ``/axis`` takes -- see
        :mod:`misc.lfs_config`. Called at most once per session.
        """
        return axis_assignments(self.settings.get('lfs_directory'),
                                self.settings.get('user_axis_brake'))

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
                self._release_throttle()
            finally:
                try:
                    self.axis_output.shutdown()
                finally:
                    self._engaged = False
                    self.pedals.stop()
                    if self._owns_physical_keys:
                        self.physical_keys.stop()

    # ─── Events ───────────────────────────────────────────────────────

    def _on_deceleration(self, data):
        """Collect one system's demand. The largest of them wins.

        Three systems publish this event now -- forward collision, cross
        traffic and blind spot -- and each one publishes every cycle. Keeping
        a single scalar meant the *last* emitter of the pass overwrote the
        others, so which hazard got braked for was decided by the iteration
        order of ``AssistanceManager.systems``. They are kept apart by
        ``source`` and reduced with ``max`` in ``_collect_demand``.
        """
        if not isinstance(data, dict):
            return
        source = data.get('source') or DEFAULT_DEMAND_SOURCE
        self._demands[source] = float(data.get('deceleration', 0.0) or 0.0)

    def _collect_demand(self):
        """Take this pass's demands and clear the collection.

        Clearing is what makes a *missing* demand distinguishable from a
        demand of zero (reference/events.md). A system that is switched off,
        has disabled itself after repeated failures, or is simply not called
        because the car left the track publishes nothing -- and a value left
        over from the last time it ran would keep the brake on with nobody
        able to take it back.

        This is why ``EmergencyBrake`` is the **last** system in
        ``AssistanceManager._init_systems``: it consumes what the pass
        produced, so every publisher has to have run first.
        """
        if self._demands:
            self._demand_source = max(self._demands, key=self._demands.get)
            self._wanted_deceleration = self._demands[self._demand_source]
            self._demands = {}
        else:
            self._demand_source = None
            self._wanted_deceleration = 0.0

    def _on_connection_changed(self, data=None):
        # LFS may have restarted; a binding we pushed into the old session
        # proves nothing about this one.
        self.key_output.binding_lost()
        self.key_throttle.binding_lost()
        self._binding_mode = None

    def _on_state_data(self, data):
        """Leaving the track ends any intervention immediately.

        ``AssistanceManager`` stops calling ``process()`` once ``on_track``
        drops, so this is the only place that can still let go -- otherwise a
        driver who hits Shift+P mid-intervention keeps a pressed brake key.
        """
        if isinstance(data, dict) and not data.get('on_track', False):
            self._disengage()

    def _on_player_changed(self, data):
        """Pruefen, ob der Bremseingriff scharf werden *kann* - sofort.

        Ohne das erfaehrt es niemand, bevor der Fahrer das erste Mal auf der
        Strecke rollt: ``AssistanceManager`` ruft ``process()`` nur dort auf,
        also stand im Menue bis dahin "Warnen & Bremsen" in Gruen, auch wenn
        vJoy gar nicht installiert war. IS_NPL ist der frueheste Moment, in
        dem der Eingabemodus feststeht.

        Laeuft auf dem Paket-Thread, aber nur bei IS_NPL - ein paar Mal pro
        Sitzung, nicht pro Zyklus.
        """
        if not isinstance(data, dict):
            return
        control_mode = data.get('control_mode')
        self._driver_mode = control_mode
        if not self._armed():
            return
        if control_mode is None:
            return
        self._output_for(control_mode)
        # Same reason as above: the menu should be able to say "the throttle
        # will not be cut" before the driver finds out at 60 km/h.
        self._throttle_cut_for(control_mode)

    def _on_keybinding_changed(self, data):
        if not isinstance(data, dict):
            return
        setting = data.get('setting')
        if setting == 'user_brake_key':
            self.key_output.binding_lost()
            self._binding_mode = None
        elif setting == 'user_throttle_key':
            self.key_throttle.binding_lost()
            self._binding_mode = None

    # ─── Main pass ────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        """Braking is armed only in mode 2; mode 1 is FCW's warning alone.

        Stays True while a press of ours is outstanding, whatever the setting
        says. ``AssistanceManager`` skips a disabled system entirely, so a mode
        switched off in the middle of an intervention would otherwise leave the
        brake key held with nobody left to release it.
        """
        if self._engaged or self.key_output.holds_press() \
                or self.axis_output.holds_axis() \
                or self.key_throttle.holds_throttle() \
                or self.axis_throttle.holds_throttle():
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
        # Before anything else: take what this pass's warning systems asked
        # for. Every one of them has already run (see ``_collect_demand``).
        self._collect_demand()

        # Not ``is_enabled()``: that one reports True while a press of ours is
        # outstanding, precisely so this pass still runs and can release it.
        if not self._armed():
            self._disengage()
            return {'active': False}

        # Before the output, because it outranks it: an output that is ready
        # to press a key it cannot aim is not armed, it is a promise. Without
        # OutGauge ``is_local_driver`` is False for a driver sitting in their
        # own car, so every single intervention was refused -- silently, at
        # debug level, under a log line that said "armed" (known-issues #51).
        # Now the menu and the log say what is actually wrong.
        outgauge = self.guard.outgauge_reason()
        if outgauge is not None:
            self._publish_availability(
                f"{input_guard.REASON_NO_OUTGAUGE}:{outgauge}")
            self._disengage()
            return {'active': False, 'refused': input_guard.REASON_NO_OUTGAUGE}

        # Binding first: ``_output_for`` refuses an output whose binding was
        # never pushed, so asking it first would refuse forever -- the push
        # sits behind the refusal that the push is supposed to clear.
        self._ensure_binding(own_vehicle, own.control_mode)

        output = self._output_for(own.control_mode)
        # NPL can report an unpushed binding before the first process pass.
        # Refresh after binding and hook setup, even without a brake demand.
        # Key path only: O(1) settings/hook checks, no axis discovery or I/O.
        if own.control_mode in MOUSE_KB_MODES:
            self._publish_throttle_availability(self.key_throttle.unavailable_reason())
        if output is None:
            self._disengage()
            return {'active': False}

        # Learn where the driver's pedals are while LFS is still reading them.
        # Not during an intervention: OutGauge then reports our own brake
        # command back (so the samples would identify our vJoy axis as the
        # driver's pedal), and the throttle it reports is zero however hard the
        # driver is pressing, because we unassigned it.
        self.pedals.observe(own_vehicle, trustworthy=not self._holding_anything())
        if not self._holding_anything():
            # Proves the throttle can be taken and given back, once, by itself
            # and at a moment it picks (Controls/throttle_axis_check.py).
            self.throttle_check.maybe_run(own_vehicle)

        wanted = self._wants_brake(own_vehicle, own)
        if wanted and not self._engaged:
            refusal = self._refusal_for(output, own_vehicle)
            if refusal is not None:
                self._disengage()
                return {'active': False, 'refused': refusal}
            self._engage(own.control_mode)
        elif not wanted and self._engaged:
            self._disengage()

        if not self._engaged:
            return {'active': False}

        fraction = self._arbitrated(self._brake_fraction(own), output)
        output.apply(fraction)
        # Every cycle, not once on engage: a driver who lets go of the
        # throttle and stands on it again mid-intervention produces a fresh
        # press, and LFS reads a *held* input whatever its binding says
        # (known-issues #46, Controls/throttle_cut.py).
        self.key_throttle.suppress()
        return {'active': True,
                'deceleration': self._wanted_deceleration,
                'brake': fraction}

    def _holding_anything(self) -> bool:
        """Is any part of an intervention currently in the driver's way?

        While it is, nothing LFS reports about the pedals describes the driver.
        """
        return (self._engaged
                or self.axis_output.holds_axis()
                or self.key_throttle.holds_throttle()
                or self.axis_throttle.holds_throttle())

    # ─── Arbitration ──────────────────────────────────────────────────

    def _arbitrated(self, fraction: float, output) -> float:
        """Never command less braking than the driver is asking for.

        On the key path this is free: our keystroke and theirs are the same
        event to LFS, and the harder input wins by construction. On the axis
        path it is the opposite -- LFS reads *only* the vJoy axis while we hold
        it, so a driver standing on the pedal got whatever we commanded, and a
        modulated 50 % actively took braking away from them. That is the exact
        inversion of what an assistant may do
        (``reference/control-intervention.md`` §1).

        The driver's pedal is read at the device (:mod:`misc.pedal_watch`),
        because LFS cannot report it while we hold the axis. If it cannot be
        read -- no joystick, pygame missing, the pedal not identified yet --
        the honest answer is that we do not know, and the only value that is
        certainly not *less* than the driver's is full braking. An emergency
        stop is the right place to err on that side.
        """
        if output is not self.axis_output:
            return fraction
        driver = self.pedals.driver_brake()
        if driver is None:
            if not self._blind_arbitration_reported:
                self._blind_arbitration_reported = True
                logger.warning(
                    "The driver's brake pedal cannot be read (%s), so the "
                    "intervention cannot tell whether it would be braking "
                    "less than they are. Commanding full braking instead.",
                    self.pedals.unavailable_reason() or 'not identified yet')
            return 1.0
        return max(fraction, driver)

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
        achieved. Assumptions, stated because they have to be (``AGENTS.md``
        §2): a road car on dry tarmac (mu ~ 1.0) reaches about
        ``FULL_BRAKE_DECELERATION_MS2``, and pedal travel maps roughly linearly
        to deceleration below lockup. Both are approximations, which is exactly
        why the measured deceleration corrects them instead of being trusted.

        ``own.acceleration`` is signed with negative meaning braking
        (``conventions.md`` §3), so the achieved deceleration is its negation.

        The digital key path ignores the value beyond "is it above zero" -- a
        key has no travel.
        """
        demand = self._demanded_deceleration()
        feed_forward = demand / self.FULL_BRAKE_DECELERATION_MS2
        error = demand - (-own.acceleration)
        fraction = feed_forward + error * self.BRAKE_GAIN_PER_MS2
        return max(self.MIN_BRAKE_FRACTION, min(1.0, fraction))

    def _demanded_deceleration(self) -> float:
        """The deceleration this cycle asks for, in m/s^2.

        Normally FCW's published demand. In the committed stop there is none:
        FCW publishes 0 below its own speed floor, and feeding that into the
        loop above collapsed the command to ``MIN_BRAKE_FRACTION`` -- 20 % of
        pedal travel for the last few metres, which is how the car still rolled
        gently into the one in front after an otherwise correct intervention.

        What replaces it is the largest demand this intervention has already
        seen, floored at ``ENGAGE_DECELERATION_MS2`` -- the level at which this
        system calls a situation an emergency in the first place, so it is also
        the right level at which to finish one -- and capped at what full
        braking can actually deliver, so a panic demand of 20 m/s^2 does not
        turn the proportional term into noise.
        """
        if not self._stopping:
            return self._wanted_deceleration
        return min(self.FULL_BRAKE_DECELERATION_MS2,
                   max(self.ENGAGE_DECELERATION_MS2, self._peak_deceleration))

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
                self._publish_availability(None)
                return self.key_output
            self._publish_availability(reason)
            return None

        reason = self.axis_output.unavailable_reason()
        if reason == REASON_LOADING:
            # The vJoy DLL is still coming up on its own thread. Same rule as
            # the input hooks above: it clears itself within a cycle or two,
            # and reporting it would put "AEB unavailable" on screen at every
            # single startup (known-issues #56).
            return None
        if reason is None:
            # Start the watchdog now, not at the first intervention: by then it
            # is too late to pay for process creation, and it only helps if it
            # is already running when we die.
            self.axis_output.start_guardian()
            if not self.axis_output.guardian_ready():
                self._publish_availability('guardian_not_ready')
                return None
            self._publish_availability(None)
            # Both are idempotent and both have to be up *before* the first
            # intervention: the guardian because process creation is too slow
            # to pay for at that moment, the pedal watch because it needs a
            # normal braking manoeuvre to identify the pedal at all. The pedal
            # watch is only *asked* here -- it has to open SDL on the main
            # thread, so ``main.py`` finishes the job in its next pump.
            self.pedals.request_start()
            return self.axis_output
        self._publish_availability(reason)
        return None

    def _try_start_hooks(self):
        """Install the input hooks, at most once every HOOK_RETRY_S.

        Off the assistance thread: installing the two ``pynput`` listeners
        takes over 100 ms, which is a whole cycle budget (``AGENTS.md`` §1).
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

    def _wants_brake(self, own_vehicle: OwnVehicle, own) -> bool:
        """Hysteresis around FCW's deceleration demand, plus the committed stop.

        Three regimes, in the order they are decided:

        1. **Not engaged** - only the demand and the speed floor matter.
        2. **Engaged and committed to a stop** - the demand is ignored, because
           below ``COMMIT_TO_STOP_SPEED_KMH`` there is no demand to read (FCW
           publishes 0 there). We brake to standstill and hold.
        3. **Engaged above that speed** - the original hysteresis.

        The runaway guard applies to all of them: whatever the state, this
        system lets go after ``MAX_ENGAGE_S``.
        """
        if not self._engaged:
            self._below_release_cycles = 0
            if own.speed < self._speed_floor():
                return False
            if self._wanted_deceleration < self.ENGAGE_DECELERATION_MS2:
                return False
            return not self._locked_out(own_vehicle)

        if self._wanted_deceleration > self._peak_deceleration:
            self._peak_deceleration = self._wanted_deceleration

        if self.clock() - self._engaged_since > self.MAX_ENGAGE_S:
            logger.warning("Emergency brake held for more than %.0f s - "
                           "releasing; the deceleration demand (%.1f m/s²) "
                           "is not plausible.",
                           self.MAX_ENGAGE_S, self._wanted_deceleration)
            return False

        if own.speed <= self.COMMIT_TO_STOP_SPEED_KMH:
            self._stopping = True
        if self._stopping:
            return self._wants_brake_while_stopping(own_vehicle, own)

        if self._wanted_deceleration < self.RELEASE_DECELERATION_MS2:
            self._below_release_cycles += 1
            return self._below_release_cycles < self.RELEASE_DEBOUNCE_CYCLES

        self._below_release_cycles = 0
        return True

    def _locked_out(self, own_vehicle) -> bool:
        """Sperrt eine Gas-Uebergabe den naechsten Eingriff noch?

        Siehe ``THROTTLE_HANDBACK_LOCKOUT_S``. Die Sperre loescht sich
        selbst, sobald eine ihrer beiden Bedingungen faellt.
        """
        since = self._throttle_handback_at
        if since is None:
            return False
        if self.clock() - since >= self.THROTTLE_HANDBACK_LOCKOUT_S:
            self._throttle_handback_at = None
            return False
        # Das Gas ist OutGauges, beschreibt also das Kamera-Auto
        # (conventions.md section 5). Ist das nicht unseres, ist es kein
        # Fahrerwille und die Sperre endet.
        if not (getattr(own_vehicle, 'is_local_driver', True)
                and getattr(own_vehicle, 'throttle', 0.0)
                > self.THROTTLE_OVERRIDE):
            self._throttle_handback_at = None
            return False
        if not self._lockout_logged:
            self._lockout_logged = True
            logger.info("Emergency brake: demand of %.1f m/s2 held back for up "
                        "to %.1f s - the driver took over on the throttle.",
                        self._wanted_deceleration,
                        self.THROTTLE_HANDBACK_LOCKOUT_S)
        return True

    def _speed_floor(self) -> float:
        """Lowest speed at which the current demand may start an intervention.

        See ``MIN_SPEED_CROSSING_KMH``: the 10 km/h floor is a statement about
        rear-ending somebody, not about being hit from the side.
        """
        if self._demand_source in self.CROSSING_DEMAND_SOURCES:
            return self.MIN_SPEED_CROSSING_KMH
        return self.MIN_SPEED_KMH

    def _wants_brake_while_stopping(self, own_vehicle: OwnVehicle, own) -> bool:
        """Brake until the car really stands, then hold for the handover.

        ``AutoHold`` needs to see standstill *and* brake pressure in the same
        pass to pull the handbrake, so letting go in the cycle the car stops
        would drop the car on a slope. The hold is timed rather than counted in
        cycles because the assistance rate is a setting (50-200 ms).

        **The driver ends this phase by pressing the throttle.** Above
        ``COMMIT_TO_STOP_SPEED_KMH`` that would be wrong -- panic throttle in
        the middle of an emergency stop is exactly what an AEB exists to
        override. Below it the emergency is over in every practical sense: what
        is left is the last metre or two, and a driver who accelerates there
        has decided the way is clear. Without this the assistant's brake and
        the driver's throttle simply fought each other and the car sat with
        both applied.

        The throttle reading is OutGauge's, so it describes the *camera* car
        (``conventions.md`` section 5). If that is not the car we are driving
        it is not an override signal, and is ignored rather than acted on.
        """
        if own_vehicle.is_local_driver and \
                own_vehicle.throttle > self.THROTTLE_OVERRIDE:
            logger.info("Emergency brake: driver applied throttle at %.1f km/h "
                        "- handing back.", own.speed)
            self._throttle_handback_at = self.clock()
            self._lockout_logged = False
            return False
        # Schmitt-Trigger, kein einzelner Schwellwert: hinein bei
        # STANDSTILL_KMH, hinaus erst bei STANDSTILL_EXIT_KMH. Sonst startet
        # das Ausfedern nach der Vollbremsung die Haltezeit immer wieder neu.
        standing = self._standstill_since is not None
        threshold = (self.STANDSTILL_EXIT_KMH if standing
                     else self.STANDSTILL_KMH)
        if own.speed > threshold:
            self._standstill_since = None
            return True
        now = self.clock()
        if not standing:
            self._standstill_since = now
            logger.info("Emergency brake: standstill reached, holding %.1f s "
                        "for the handover.", self.STANDSTILL_HOLD_S)
        return now - self._standstill_since < self.STANDSTILL_HOLD_S

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
        # Both or neither: ``_binding_mode`` is the "we have written what LFS
        # believes" flag, and it must not claim that while half of it failed.
        brake_pushed = self.key_output.push_binding()
        throttle_pushed = self.key_throttle.push_binding()
        if brake_pushed and throttle_pushed:
            self._binding_mode = control_mode

    # ─── Engagement ───────────────────────────────────────────────────

    def _throttle_cut_for(self, control_mode: int):
        """The throttle cut that matches this control mode, or None.

        Same split as the brake: keys are ignored for throttle in ``wheel_js``
        and there is no axis in ``mouse_kb``. A cut that cannot be armed is
        reported once and then simply skipped -- the brake intervention is far
        more important than the throttle cut and must never depend on it.
        """
        cut = (self.key_throttle if control_mode in MOUSE_KB_MODES
               else self.axis_throttle)
        self._publish_throttle_availability(cut.unavailable_reason())
        return None if cut.unavailable_reason() is not None else cut

    def _publish_throttle_availability(self, reason: Optional[str]):
        """One line and one event per change, not one per intervention."""
        if reason == self._throttle_reason_reported:
            return
        self._throttle_reason_reported = reason
        self.event_bus.emit('throttle_cut_availability', {'reason': reason})
        if reason is None:
            logger.info("The throttle will be cut during an intervention.")
            return
        logger.warning("The throttle cannot be cut during an intervention: "
                       "%s. Braking is unaffected.", reason)

    def _release_throttle(self):
        """Give the throttle back, whichever path took it."""
        self.key_throttle.release()
        self.axis_throttle.release()

    def _engage(self, control_mode: int):
        """Mark the intervention started and tell the driver it is happening."""
        cut = self._throttle_cut_for(control_mode)
        if cut is not None:
            # Throttle first: it is the cheap half, it can only ever help, and
            # every metre driven with the engine still pulling is a metre of
            # braking distance thrown away.
            cut.engage()
        self._engaged = True
        self._engaged_since = self.clock()
        self._below_release_cycles = 0
        self._stopping = False
        self._standstill_since = None
        self._peak_deceleration = self._wanted_deceleration
        logger.info("Emergency brake engaged (demand %.1f m/s², %s).",
                    self._wanted_deceleration,
                    self._demand_source or 'unknown source')
        # Only the state event. Deliberately *not* a ``notification``: those are
        # queued and shown one at a time for 3 s, so the driver saw "!! BRAKE !!"
        # about a second after the braking had already finished, and repeated
        # interventions stacked up behind each other. An intervention indicator
        # has to be live, so it is drawn from this event instead.
        self.event_bus.emit('emergency_brake_changed',
                            {'active': True, 'source': self._demand_source})

    def _disengage(self):
        """Drop our share of the brake and forget the engagement.

        Called from every path that stops wanting the brake, including the
        disabled and unavailable ones, so a mode change or a setting flipped
        mid-intervention cannot strand a pressed key.
        """
        self.key_output.release()
        self.axis_output.release()
        self._release_throttle()
        self._below_release_cycles = 0
        self._stopping = False
        self._standstill_since = None
        self._peak_deceleration = 0.0
        if not self._engaged:
            return
        self._engaged = False
        logger.info("Emergency brake released.")
        self.event_bus.emit('emergency_brake_changed',
                            {'active': False, 'source': None})

    def _publish_availability(self, reason: Optional[str]):
        """Sagt, ob der Bremseingriff scharf ist - und wenn nicht, warum.

        Genau einmal pro *Wechsel*, nicht einmal pro Zyklus: das Event geht ins
        Menue (``ui/menu_system.py``), das daraufhin neu zeichnet. ``None``
        wird mitgesendet, sonst bliebe eine behobene Ursache fuer immer stehen.

        Der Text fuer den Fahrer entsteht im Menue, nicht hier - hier gibt es
        nur den internen Grund, damit die UI ihn uebersetzen kann.
        """
        if self._reported_reason == reason:
            return
        self._reported_reason = reason
        self.event_bus.emit('emergency_brake_availability', {'reason': reason})
        if reason is None:
            logger.info("Automatic emergency braking is armed.")
            return
        logger.warning("Automatic emergency braking is switched on but cannot "
                       "be armed: %s", reason)
        self.event_bus.emit('notification',
                            {'notification': f"^1AEB unavailable: {reason}"})
