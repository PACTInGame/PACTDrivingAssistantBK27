"""Proving, once and by itself, that the throttle can be taken and given back.

``AxisThrottleCut`` cuts the throttle with ``/axis -1 throttle``, which needs no
knowledge and destroys nothing, and gives it back with ``/axis <n> throttle``
plus ``/invert <p> throttle``, which needs the *right* number and the *right*
polarity. LFS keeps one input per function and one function per input, so a
wrong ``n`` puts the throttle on an input the driver is not using **and** takes
whatever that input was doing away from it -- the box in
``reference/control-intervention.md`` §2.2.

Both values are read out of LFS's own controller file (:mod:`misc.lfs_config`),
so they are not guesses. This module is the last link: it confirms that what
the file says actually behaves that way in the running game, before an
emergency ever depends on it.

Nothing is asked of the driver
==============================

An earlier version put a "check the throttle axis" button in the menu and asked
the driver to hold the pedal on command. That was the wrong shape: a driver
assistant that needs a setup ritual will be used with the ritual skipped. The
check now runs on its own, and only when it is already safe and meaningful:

* the throttle pedal has been identified at the device, and LFS has been seen
  reading *that* pedal for a sustained stretch of driving
  (:meth:`~misc.pedal_watch.PedalWatch.confidence`);
* the driver is holding the throttle anyway, at a speed where losing it for
  half a second is nothing;
* no intervention is running.

It then cuts and restores through the very same ``AxisThrottleCut`` that an
intervention uses -- not a copy of it, because verifying something other than
what runs in anger verifies nothing -- and watches two independent readings of
the same pedal:

* :class:`~misc.pedal_watch.PedalWatch` reads the **hardware**, over SDL, and
  LFS cannot influence it;
* ``OutGaugePack.Throttle`` is what **LFS** currently makes of its throttle
  input.

Cut: LFS must fall to zero while the pedal stays down. Restore: LFS must follow
the pedal again. A restore that does not work sets ``throttle_axis_broken``, so
it is never attempted a second time -- a second attempt would take a second
axis away for nothing.

**The polarity is why this exists in its current form.** The first live run
reported "axis 9 is not your throttle" with axis 9 being exactly right: the
restore had put the assignment back but not the invert flag, so a fully pressed
pedal read as no throttle at all. That failure looked precisely like a wrong
axis number, and only LFS's own file (``throttle invert 1``) told the two
apart.
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# After an ``/axis`` command: a TCP round trip plus at least one OutGauge frame.
SETTLE_S = 0.7
# The hardware pedal has to be held this far down for the comparison to mean
# anything, and LFS has to report at least this much for "LFS agrees".
PEDAL_HELD = 0.5
LFS_SEES_IT = 0.3
# And this little for "LFS is not reading the pedal any more".
LFS_BLIND = 0.15
# Fast enough that half a second without throttle changes nothing.
MIN_SPEED_KMH = 25.0


class _Inconclusive(Exception):
    """The driver lifted off; nothing was proved and nothing was broken."""


class ThrottleAxisCheck:
    """One run of the verification described in the module docstring."""

    def __init__(self, event_bus, settings, pedals, cut, sleep=None, clock=None):
        self.event_bus = event_bus
        self.settings = settings
        self.pedals = pedals
        self.cut = cut
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._lfs_throttle = 0.0
        self._thread: Optional[threading.Thread] = None
        self._done = False
        self.event_bus.subscribe('outgauge_data', self._on_outgauge)

    # ─── Bus ──────────────────────────────────────────────────────────

    def _on_outgauge(self, packet):
        try:
            self._lfs_throttle = float(getattr(packet, 'Throttle', 0.0) or 0.0)
        except (TypeError, ValueError):
            self._lfs_throttle = 0.0

    # ─── Entry point ──────────────────────────────────────────────────

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def maybe_run(self, own_vehicle) -> bool:
        """Start the check if this is a good moment. Called from the AEB pass.

        Cost when it declines, which is every cycle but one in a session: four
        comparisons and a settings read. The decision deliberately lives here
        rather than in a timer, because "a good moment" is a property of what
        the car is doing.
        """
        if self._done or self.running():
            return False
        if self.settings.get('throttle_axis_verified') or \
                self.settings.get('throttle_axis_broken'):
            self._done = True
            return False
        if self.cut.assignment() is None:
            return False
        if self.cut.confidence() < self.cut.REQUIRED_CONFIDENCE:
            return False
        if own_vehicle.data.speed < MIN_SPEED_KMH:
            return False
        if (self.pedals.driver_throttle() or 0.0) < PEDAL_HELD:
            return False
        if self._lfs_throttle < LFS_SEES_IT:
            # LFS is not reading this pedal right now. Cutting and restoring
            # would prove nothing, and the restore could still do damage.
            return False

        self._done = True
        self._thread = threading.Thread(target=self._run,
                                        name='throttle-axis-check', daemon=True)
        self._thread.start()
        return True

    # ─── The check ────────────────────────────────────────────────────

    def _run(self):
        """Never lets an exception escape, and always hands the throttle back.

        The ``finally`` is the important part: an exception between the cut and
        the restore would otherwise leave the driver with no throttle at all,
        and the only thing that would notice is the guardian, after the process
        died.

        Three outcomes, not two. A run that proved nothing because the driver
        lifted off is **not** a failure: marking it one would disable the
        throttle cut over a coincidence of timing. It simply leaves everything
        as it was, and the next straight tries again.
        """
        verified = False
        inconclusive = False
        try:
            verified = self._check()
        except _Inconclusive:
            inconclusive = True
            self._done = False
        except Exception as exc:
            logger.error("The throttle axis check failed: %s: %s",
                         type(exc).__name__, exc)
        finally:
            self.cut.release()
        if inconclusive:
            return
        self.settings.set('throttle_axis_verified', bool(verified))
        if not verified:
            self.settings.set('throttle_axis_broken', True)
        self.event_bus.emit('throttle_axis_checked', {'verified': bool(verified)})

    def _check(self) -> bool:
        assignment = self.cut.assignment()
        logger.info("Checking the throttle axis: LFS reads %.2f, the pedal is "
                    "at %.2f; about to hand it to /axis %d invert %d.",
                    self._lfs_throttle, self.pedals.driver_throttle() or 0.0,
                    assignment.axis, assignment.invert)

        if not self.cut.engage(force=True):
            logger.error("The throttle cut could not be sent at all.")
            return False
        self._sleep(SETTLE_S)
        lfs_while_cut = self._lfs_throttle
        pedal_while_cut = self.pedals.driver_throttle() or 0.0

        self.cut.release()
        self._sleep(SETTLE_S)
        lfs_after = self._lfs_throttle
        pedal_after = self.pedals.driver_throttle() or 0.0

        logger.info("Throttle axis check: cut -> LFS %.2f (pedal %.2f), "
                    "restored -> LFS %.2f (pedal %.2f).",
                    lfs_while_cut, pedal_while_cut, lfs_after, pedal_after)

        if pedal_while_cut < PEDAL_HELD or pedal_after < PEDAL_HELD:
            # The driver lifted off mid-check. Nothing was proved and nothing
            # was broken, so this is not a failure -- it may run again.
            logger.info("The throttle was released during the check - it will "
                        "be repeated.")
            raise _Inconclusive()
        if lfs_while_cut > LFS_BLIND:
            logger.error("LFS kept reading the throttle after /axis -1 - the "
                         "cut does not work on this install.")
            return False
        if lfs_after < LFS_SEES_IT:
            logger.error("Handing the throttle back to /axis %d (invert %d) "
                         "did not restore it. The throttle will not be cut, "
                         "and axis %d may need to be set again in LFS under "
                         "Options - Controls.",
                         assignment.axis, assignment.invert, assignment.axis)
            return False

        logger.info("Throttle axis %d verified: it is cut and restored "
                    "correctly.", assignment.axis)
        self.event_bus.emit('notification',
                            {'notification': "^2Throttle cut ready"})
        return True
