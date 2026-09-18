"""Standalone in-game test harness for the PACT Driving Assistant.

This package drives Live for Speed through recorded mouse/keyboard replays and
records what the game reports back over InSim/OutGauge, so an automated agent can
verify behaviour without a human in the loop.

It is deliberately **independent of the add-on**: nothing in here imports
``core``, ``assistance``, ``ui``, ``lfs`` or ``vehicles``. The only shared code is
``pyinsim`` — the LFS protocol library, not add-on logic.
"""

__all__ = []
