"""Capture a loaded LFS layout and turn it into ``track_data/track_data_XX.json``.

**Offline tool, not a test** (it used to live in the project root as
``test.py``, which every test runner tried to collect). It needs LFS running
with the layout loaded and InSim on port 29999, and it pulls in ``numpy``,
``scipy`` and ``matplotlib`` through ``MapBuilder`` -- none of which the
running app needs. See ``reference/ai-traffic.md`` §5.

Run it from the project root:  ``python tools/capture_layout.py``
"""

import os
import sys

# Run as a script, sys.path[0] is tools/ -- MapBuilder and pyinsim live one
# directory up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyinsim
from MapBuilder import MapGenerator

insim = pyinsim.insim(b'127.0.0.1', 29999, Admin=b'', Flags=pyinsim.ISF_MCI | pyinsim.ISF_LOCAL, Interval=1000)
objects = []
def handle_layout(insim, axm):
    for object in axm.Info:
        index = object.Index
        x = object.X / 16
        y = object.Y / 16
        z = object.Zbyte / 4
        # Coordinates in Meters:
        if object.Index != 184:
            objects.append((index, x, y, z))
    print(len(objects))
    if len(objects) >= 318:
        gen = MapGenerator(objects)
        gen.process()
        gen.save_to_json()
        gen.debug_plot()

insim.bind(pyinsim.ISP_AXM, handle_layout)
insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_AXM)

pyinsim.run()
