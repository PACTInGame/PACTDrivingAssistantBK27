"""What we have observed about each car model, learned while the game runs.

Three car-specific numbers drive the automatic gearbox and the HUD's redline
colour: idle rpm, redline and the number of forward gears. Until now the only
source was the manual calibration -- 36 seconds of standing still, per car,
before the gearbox does anything at all.

They can be *measured*, and LFS hands us everything needed in the OutGauge
stream: the highest rpm the engine has ever reached, the highest gear ever
engaged, and the rpm that is there while the car stands still off the throttle.
The values improve as the car is driven and are remembered between sessions, so
the second lap in a car is already better informed than the first.

**Why not the shift light.** `OutGaugePack.ShowLights & DL_SHIFT` looks like a
redline delivered by LFS itself, and `ui_manager` used it to colour the rpm red.
Measured in this project: **the shift light is only active in the race cars**;
most road cars never set it, so the HUD's rpm never turned red for them. It is
not a usable source. The highest rpm ever seen is.

## The camera trap, and why the packet's own `Car` field is the answer

OutGauge describes the car the **camera** is on, not necessarily the player's
(`conventions.md` §5). Press TAB and the next packet carries another car's rpm
and gear. Learning those under the player's car name would poison the profile,
and the window is exactly one packet wide -- a car name tracked through a
*different* channel (`IS_NPL`, MCI) changes at its own pace, not in step with
the OutGauge stream.

So this module never asks anyone which car it is. `Car`, `RPM`, `Gear`,
`Throttle` and `Speed` are unpacked from **one packet**
(`pyinsim/insim.py`, `OutGaugePack.unpack`), so they are consistent by
construction: whatever `Car` says, the numbers beside it belong to that car.
Spectating therefore cannot mis-attribute anything -- it just teaches us about
the car being watched, which is the same car model for everyone.

## What a measurement has to survive, learned the hard way

Every gate here answers a failure seen in the game, not a hypothetical:

* A **rising** rpm is not a rev limit. While a car is revved for the first time
  the highest rpm ever seen *is* the current rpm, so it only counts once it has
  stopped rising (``REDLINE_SETTLE_S``).
* An engine being **shut down** is not idling. LFS stops a standing engine on
  its own, and on the way down it is stationary and off the throttle -- every
  other gate lets it through. One shutdown dragged a mod's learned idle from 627
  to 446 min-1. An idling engine is governed and holds its speed
  (``IDLE_MAX_RPM_PER_S``).
* A **stopped** engine reports ``rpm 0``, which is the absence of a reading, not
  a reading of zero (``ENGINE_RUNNING_MIN_RPM``).
* **Neutral** is the gear a parked car is in, so seeing it says nothing about
  the gearbox.

## Cost

One dict lookup and about eight comparisons per OutGauge packet (~30 Hz), on the
packet thread. No file I/O there: writing is deferred to :meth:`maybe_save`,
which a slow scheduled task and the shutdown path call.
"""

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

from misc.helpers import resolve_data_path as resolve_path

logger = logging.getLogger(__name__)

PROFILE_FILE = os.path.join('data', 'car_profiles.json')

# --- Plausibility gates ------------------------------------------------------
# A four-stroke does not idle below 500 min-1; the lowest idle measured among the
# LFS stock cars is the UF1 at ~970. Below this the engine is off or cranking,
# and a stopped engine reports 0 -- which must never become an idle speed.
ENGINE_RUNNING_MIN_RPM = 300.0
# The highest-revving LFS car is the BF1 at ~19000 min-1. Anything past this is a
# corrupt packet, not an engine.
MAX_PLAUSIBLE_RPM = 25000.0
# OutGauge gear index: 0 = reverse, 1 = neutral, 2 = 1st. Nothing in LFS has ten
# forward gears.
MAX_PLAUSIBLE_GEAR = 11
# Idle is only sampled while the car really stands still and the driver is off
# the throttle, so the samples are idle rpm and nothing else.
IDLE_MAX_SPEED_KMH = 1.0
IDLE_MAX_THROTTLE = 0.05
# Enough samples to be a median rather than an accident, few enough that a car
# that idled at a different temperature hours ago cannot dominate.
IDLE_SAMPLE_LIMIT = 200
# An idling engine is *governed*: its speed hunts around a target by a few rpm.
# An engine that is being shut down, or started, sweeps through everything below
# idle instead -- and while it does, it is stationary and off the throttle, so
# every other gate lets it through. Measured live: a shutdown dragged a mod's
# learned idle from 627 down to 446 min-1 and back. A sample therefore only
# counts while the engine speed is holding still.
IDLE_MAX_RPM_PER_S = 400.0
# Stability can only be judged between packets that are actually consecutive
# (~30 Hz). After a longer gap -- the camera was on another car -- we cannot
# tell, so we do not guess.
IDLE_MAX_GAP_S = 0.5

# Only write the file when something actually changed, and not more often than
# this. Writing runs off the packet thread either way.
SAVE_INTERVAL_S = 30.0

# How long the highest rpm has to stand still before it counts as a redline.
#
# While a car is being revved for the first time, *every* packet sets a new
# maximum -- the "highest rpm ever seen" is then just the current rpm, and using
# it would paint the readout red for the whole first climb and make the gearbox
# upshift immediately. Once the engine stops going higher, because the driver
# backed off or the limiter caught it, the maximum stops moving and becomes a
# statement about the engine rather than about this moment. Five seconds is long
# enough that no gear change spans it and short enough to be settled before the
# first lap ends. A value read from the file is settled by definition.
REDLINE_SETTLE_S = 5.0


def car_key(car: Any) -> str:
    """The car name as a dict key, from whatever OutGauge put in the packet.

    ``OutGaugePack.Car`` is three raw bytes and, unlike ``Display1``/``Display2``,
    pyinsim does not strip the padding from it.

    A **stock car** puts its name there as text: ``XFG``, ``RB4``. A **mod** puts
    its numeric id there, and those bytes are not text at all -- measured live,
    two mods sent ``06 C8 D3`` and ``AC C6 55``. Decoding them as characters gave
    keys like ``'\x06ÈÓ'``: usable as a dict key, unreadable in the profile file
    and in the log. Non-text bytes are therefore rendered as the six-digit hex id
    LFS itself uses for a mod.

    Nothing here needs a list of known names, which is the point
    (`conventions.md` §4): an unrecognised key simply starts a new profile.
    """
    if isinstance(car, (bytes, bytearray)):
        raw = bytes(car).split(b'\x00', 1)[0]
    elif isinstance(car, str):
        # Also the migration path for keys an earlier build stored as characters:
        # latin-1 is a total, lossless byte<->char mapping, so this recovers the
        # original bytes exactly and re-renders them the new way.
        raw = car.strip().encode('latin-1', errors='replace')
    else:
        return ''

    if not raw:
        return ''
    if all(0x20 < byte < 0x7F for byte in raw):
        return raw.decode('ascii').upper()
    return _mod_id(raw)


def _mod_id(raw: bytes) -> str:
    """A mod's three id bytes as the hex id LFS shows.

    Byte order is the one lfs.net uses for mod ids (little-endian 24-bit). It
    could **not** be verified locally: the installed mods are filed under their
    titles, and ``C:\\LFS\\docs\\InSim.txt`` is now only a link to the online
    documentation. It is cosmetic either way -- the profile is keyed on the
    bytes, so a wrong rendering costs readability, not correctness. If the id
    does not match the one on lfs.net, reverse the byte order here.
    """
    return format(int.from_bytes(raw.ljust(3, b'\x00')[:3], 'little'), '06X')


# Measured values for the LFS stock cars, so that a car the driver has never
# touched still shifts on the first lap. Every entry was produced by running
# this project's own calibration in the car -- nothing here is from memory or
# from a spec sheet, and a value that could not be measured is simply absent.
#
# It is a *seed*, not an authority: the driver's own calibration overrides it,
# and cars outside it (every mod) fall back to the learned profile, which is why
# keying on the name here is safe (`conventions.md` §4).
STOCK_PROFILES: Dict[str, Dict[str, Any]] = {
    'FXO': {'idle': 1093, 'redline': 7482, 'forward_gears': 5},
    'FXR': {'idle': 1409, 'redline': 7492, 'forward_gears': 6},
    'FZ5': {'idle': 952, 'redline': 7971, 'forward_gears': 5},
    'FZR': {'idle': 1425, 'redline': 8475, 'forward_gears': 6},
    'LX4': {'idle': 934, 'redline': 8974, 'forward_gears': 6},
    'LX6': {'idle': 934, 'redline': 8975, 'forward_gears': 6},
    'RAC': {'idle': 942, 'redline': 6490, 'forward_gears': 5},
    'RB4': {'idle': 959, 'redline': 7481, 'forward_gears': 5},
    'UF1': {'idle': 970, 'redline': 6983, 'forward_gears': 4},
    'UFR': {'idle': 1431, 'redline': 8979, 'forward_gears': 5},
    'XFG': {'idle': 950, 'redline': 7979, 'forward_gears': 5},
    'XFR': {'idle': 1424, 'redline': 7979, 'forward_gears': 6},
    'XRG': {'idle': 951, 'redline': 6981, 'forward_gears': 5},
    'XRR': {'idle': 1409, 'redline': 7492, 'forward_gears': 6},
    'XRT': {'idle': 958, 'redline': 7481, 'forward_gears': 5},
}

# Cars the automatic gearbox must not shift **on its own**, even though it could
# work out their numbers. Author's decision, not a technical limit: on a
# single-seater the driver wants the gears. They are simply absent from
# ``STOCK_PROFILES``, and a *learned* profile must not arm them either -- only
# the driver's own calibration does, which is how someone who wants it gets it.
NO_AUTOMATIC_BY_DEFAULT = frozenset({'FBM', 'FOX', 'FO8', 'BF1'})

# Cars the automatic gearbox must not shift **at all**. The MRT5 has a
# motorbike gearbox: the shift logic in ``assistance/gearbox.py`` assumes a car
# gearbox (a neutral between reverse and first, a clutch worth disengaging),
# and none of that holds here. Arming it from a calibration would still shift
# wrongly, so the calibration is refused rather than the result ignored.
NEVER_AUTOMATIC = frozenset({'MRT'})

# Both lists are keyed on the car name and therefore only cover the stock cars.
# That is deliberate and the safe direction is stated: a *mod* single-seater is
# not recognised and will shift automatically once its profile is learned. If
# that turns out to matter, the distinguishing property has to be measured
# rather than named (`conventions.md` §4).


def stock_profile(car) -> Optional[Dict[str, Any]]:
    """The built-in values for a stock car, or ``None`` for anything else."""
    return STOCK_PROFILES.get(car_key(car))


def automatic_gearbox_allowed(car) -> bool:
    """May the automatic gearbox operate this car at all?"""
    return car_key(car) not in NEVER_AUTOMATIC


def automatic_gearbox_by_default(car) -> bool:
    """May it arm itself here, or does it need the driver's own calibration?"""
    key = car_key(car)
    return key not in NEVER_AUTOMATIC and key not in NO_AUTOMATIC_BY_DEFAULT


class CarProfile:
    """The observations for one car model."""

    __slots__ = ('max_rpm', 'max_gear', 'max_rpm_at', 'idle_samples',
                 'last_rpm', 'last_seen',
                 '_version', '_cached_version', '_cached_idle')

    def __init__(self, max_rpm: float = 0.0, max_gear: int = 0,
                 max_rpm_at: float = 0.0):
        self.max_rpm = max_rpm
        self.max_gear = max_gear
        # Clock reading when max_rpm last went up. 0.0 means "from the file",
        # i.e. settled long ago.
        self.max_rpm_at = max_rpm_at
        self.idle_samples = deque(maxlen=IDLE_SAMPLE_LIMIT)
        # Previous packet for this car, for judging whether the engine speed is
        # holding still. None: no previous packet in this session.
        self.last_rpm = None
        self.last_seen = None
        self._version = 0
        self._cached_version = -1
        self._cached_idle = None

    def add_idle_sample(self, rpm: float):
        self.idle_samples.append(rpm)
        self._version += 1

    @property
    def idle_rpm(self) -> Optional[float]:
        """The rpm that was there most of the time while standing still.

        The median, not the minimum: an idle governor hunts around its target,
        and a minimum has no defence against a single low reading. ``None``
        means the car has never been observed idling.

        Cached against a version counter, because the gearbox asks once per
        assistance cycle and sorting 200 samples for an answer that has not
        changed is exactly the kind of per-cycle work `AGENTS.md` §1 forbids.
        New samples only arrive while the car stands still -- that is, never
        while the answer is being used to shift.
        """
        if self._cached_version == self._version:
            return self._cached_idle
        if not self.idle_samples:
            value = None
        else:
            ordered = sorted(self.idle_samples)
            value = ordered[len(ordered) // 2]
        self._cached_idle = value
        self._cached_version = self._version
        return value

    @property
    def forward_gears(self) -> int:
        """Forward gears, from the highest gear ever engaged (0 = not known)."""
        return max(0, self.max_gear - 1)

    @property
    def has_measurements(self) -> bool:
        """Did this profile ever learn anything?

        ``forward_gears`` rather than ``max_gear``: neutral is the gear a
        parked car is in, so seeing it is not an observation about the gearbox.
        """
        return bool(self.max_rpm > 0 or self.forward_gears > 0
                    or self.idle_samples)

    def as_dict(self) -> Dict[str, Any]:
        idle = self.idle_rpm
        return {
            'max_rpm': round(self.max_rpm, 1),
            'max_gear': self.max_gear,
            'idle_rpm': None if idle is None else round(idle, 1),
        }


class CarProfiles:
    """Learns idle, redline and gear count per car model from OutGauge.

    Subscribes to ``outgauge_data`` itself; nothing has to feed it. Query it
    with :meth:`redline`, :meth:`idle` and :meth:`forward_gears`.
    """

    def __init__(self, event_bus=None, path: Optional[str] = None,
                 clock=time.monotonic):
        self.clock = clock
        self._path = path if path is not None else resolve_path(PROFILE_FILE)
        # Written on the packet thread, read on the assistance and UI threads.
        # The lock is held for a handful of comparisons only.
        self._lock = threading.Lock()
        self._profiles: Dict[str, CarProfile] = {}
        self._dirty = False
        self._saved_at = 0.0
        self._load()
        if event_bus is not None:
            event_bus.subscribe('outgauge_data', self.record)

    # --- Learning -----------------------------------------------------

    def record(self, packet):
        """One OutGauge packet. Runs on the packet thread, ~30 Hz.

        Every field is read from *this* packet, including the car name: that is
        what makes the values immune to a camera change (see the module
        docstring). Every field is also read defensively -- a packet from an
        older or modded LFS must not kill the packet handler.
        """
        car = car_key(getattr(packet, 'Car', None))
        if not car:
            return

        try:
            rpm = float(getattr(packet, 'RPM', 0.0) or 0.0)
            gear = int(getattr(packet, 'Gear', 0) or 0)
            throttle = float(getattr(packet, 'Throttle', 1.0) or 0.0)
            speed_kmh = float(getattr(packet, 'Speed', 0.0) or 0.0) * 3.6
        except (TypeError, ValueError):
            return

        with self._lock:
            profile = self._profiles.get(car)
            if profile is None:
                profile = self._profiles[car] = CarProfile()
                logger.info("Learning a profile for car %r.", car)

            now = self.clock()
            if ENGINE_RUNNING_MIN_RPM <= rpm <= MAX_PLAUSIBLE_RPM:
                if rpm > profile.max_rpm:
                    profile.max_rpm = rpm
                    profile.max_rpm_at = now
                    self._dirty = True
                if (speed_kmh <= IDLE_MAX_SPEED_KMH
                        and throttle <= IDLE_MAX_THROTTLE
                        and _engine_speed_is_holding(profile, rpm, now)):
                    profile.add_idle_sample(rpm)
                    self._dirty = True

            if 0 <= gear <= MAX_PLAUSIBLE_GEAR and gear > profile.max_gear:
                profile.max_gear = gear
                self._dirty = True

            profile.last_rpm = rpm
            profile.last_seen = now

    # --- Queries ------------------------------------------------------

    def profile(self, car) -> Optional[CarProfile]:
        with self._lock:
            return self._profiles.get(car_key(car))

    def redline(self, car) -> Optional[float]:
        """The engine's rev limit, or ``None`` while we cannot tell yet.

        The highest rpm ever seen -- but only once it has stopped rising for
        ``REDLINE_SETTLE_S``. During the first climb the maximum *is* the
        current rpm, and answering with it would mean "you are at the redline"
        at every rpm. Better to say nothing than to say something false.
        """
        profile = self.profile(car)
        if profile is None or profile.max_rpm <= 0:
            return None
        if self.clock() - profile.max_rpm_at < REDLINE_SETTLE_S:
            return None
        return profile.max_rpm

    def highest_rpm_seen(self, car) -> Optional[float]:
        """The raw maximum, settled or not. Diagnostics and tests."""
        profile = self.profile(car)
        if profile is None or profile.max_rpm <= 0:
            return None
        return profile.max_rpm

    def idle(self, car) -> Optional[float]:
        profile = self.profile(car)
        return None if profile is None else profile.idle_rpm

    def forward_gears(self, car) -> int:
        profile = self.profile(car)
        return 0 if profile is None else profile.forward_gears

    def known_cars(self):
        with self._lock:
            return sorted(self._profiles)

    # --- Persistence --------------------------------------------------

    def maybe_save(self, force: bool = False):
        """Write the file if anything changed. **Never on the packet thread.**

        A scheduled task calls this every 30 s and the shutdown path forces it.
        Rate-limited because a car that is being driven marks the profile dirty
        several times a second.
        """
        with self._lock:
            if not self._dirty:
                return
            now = self.clock()
            if not force and now - self._saved_at < SAVE_INTERVAL_S:
                return
            # Ein Profil ohne jede Messung ist kein Wissen, sondern nur die
            # Notiz "dieses Auto wurde einmal gesehen" - das gehoert nicht in
            # die Datei (live beobachtet als RB4-Eintrag mit lauter Nullen,
            # weil beim Start der Motor aus war).
            snapshot = {car: profile.as_dict()
                        for car, profile in self._profiles.items()
                        if profile.has_measurements}
            self._saved_at = now
            self._dirty = False

        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self._path, 'w', encoding='utf-8') as handle:
                json.dump(snapshot, handle, indent=4, sort_keys=True)
        except OSError as exc:
            # Losing the file costs learning time, nothing else - the profiles
            # are still in memory and rebuild themselves from driving.
            logger.warning("Could not write %s: %s: %s",
                           self._path, type(exc).__name__, exc)
            with self._lock:
                self._dirty = True

    def _load(self):
        try:
            with open(self._path, 'r', encoding='utf-8') as handle:
                stored = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable %s: %s: %s",
                           self._path, type(exc).__name__, exc)
            return

        if not isinstance(stored, dict):
            logger.warning("Ignoring %s: expected an object at the top level.",
                           self._path)
            return

        for car, values in stored.items():
            if not isinstance(values, dict):
                continue
            key = car_key(car)
            if not key:
                continue
            profile = CarProfile(
                max_rpm=_as_float(values.get('max_rpm'), 0.0),
                max_gear=_as_int(values.get('max_gear'), 0),
            )
            # One stored sample stands for the whole earlier session: it is a
            # median already, and re-seeding the deque with copies of it would
            # only make it harder for this session's measurements to correct it.
            idle = _as_float(values.get('idle_rpm'), 0.0)
            if idle >= ENGINE_RUNNING_MIN_RPM:
                profile.add_idle_sample(idle)
            self._profiles[key] = profile

        if self._profiles:
            logger.info("Loaded car profiles for %s.",
                        ", ".join(sorted(self._profiles)))


def _engine_speed_is_holding(profile: CarProfile, rpm: float,
                             now: float) -> bool:
    """Is the engine sitting at a governed speed rather than sweeping through?

    Rate-limited rather than step-limited, because the OutGauge send rate is a
    setting in ``cfg.txt`` -- a fixed "rpm per packet" threshold would mean
    something different on another machine.
    """
    if profile.last_rpm is None or profile.last_seen is None:
        return False
    elapsed = now - profile.last_seen
    if elapsed <= 0 or elapsed > IDLE_MAX_GAP_S:
        return False
    return abs(rpm - profile.last_rpm) / elapsed <= IDLE_MAX_RPM_PER_S


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
