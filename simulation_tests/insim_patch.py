"""Compatibility entry point for tracer imports.

Contact decoding lives in the shared pyinsim library. Keeping these aliases and
apply() lets existing tracer copies use the fixed decoder without a second
protocol implementation or mutations to the process-wide packet map.
"""

from pyinsim import CarContact, IS_CON


def apply(pyinsim_module):
    """Retained for existing tracers; pyinsim already registers this decoder."""
