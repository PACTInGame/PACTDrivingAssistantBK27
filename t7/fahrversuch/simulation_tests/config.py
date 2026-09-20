"""Fixed configuration for the simulation test harness.

Ports are chosen so the harness can run **next to** a live add-on session:
the add-on owns OutGauge 30000 and OutSim 29998 (see reference/lfs-setup.md).
The tracer never binds those. It asks LFS for its own OutGauge/OutSim stream on
:data:`TRACER_UDP_PORT` via ``SMALL_SSG`` / ``SMALL_SSP``, which is a per-InSim
connection setting and does not disturb the add-on's streams.
"""

# --- LFS endpoints ---------------------------------------------------------
LFS_HOST = "127.0.0.1"
INSIM_PORT = 29999

#: UDP port LFS streams MCI/OutGauge/OutSim to *for this InSim connection*.
#: Must differ from the add-on's 30000 (OutGauge) and 29998 (OutSim).
TRACER_UDP_PORT = 30011

#: Loopback request/reply channel between run_scenario/replay and the tracer.
CONTROL_PORT = 30111

# --- Rates -----------------------------------------------------------------
#: Mouse sampling grid while recording, in milliseconds (the "50 ms Takt").
INPUT_SAMPLE_INTERVAL_MS = 50
#: MCI interval requested from LFS. 100 ms matches the add-on's default.
MCI_INTERVAL_MS = 100
#: OutGauge interval requested via SMALL_SSG.
OUTGAUGE_INTERVAL_MS = 50
#: OutSim interval requested via SMALL_SSP (only when --outsim is given).
OUTSIM_INTERVAL_MS = 100

# --- Defaults --------------------------------------------------------------
#: Packets the base tracer logs unless a scenario or the CLI says otherwise.
DEFAULT_PACKETS = (
    "STA",   # game state: on track / dialog / text entry / camera / track
    "CIM",   # which LFS screen the local connection is on
    "NPL",   # player joined or left the pits (identity, car, PType)
    "PLP",   # player went to the garage
    "PLL",   # player left
    "MCI",   # multi car info: position, speed, heading of every car
    "CON",   # car-to-car contact          (needs ISF_CON)
    "OBH",   # car-to-object hit           (needs ISF_OBH)
    "MSO",   # chat / system messages
    "BTC",   # button clicked
    "RST",   # race start
    "AXI",   # autocross layout info
)

#: Keys the recorder consumes itself; they never reach the replay stream.
#: Chosen because LFS binds neither of them.
DEFAULT_MARKER_KEY = "scroll_lock"
DEFAULT_STOP_KEY = "pause"
#: Panic key that aborts a running replay and releases everything held down.
DEFAULT_ABORT_KEY = "pause"

#: Window title substring used to recognise LFS (case-insensitive).
LFS_WINDOW_MATCH = "live for speed"

#: Trace file format version. Bump when a record's meaning changes.
TRACE_FORMAT = 1
#: Input recording format version.
INPUT_FORMAT = 1
