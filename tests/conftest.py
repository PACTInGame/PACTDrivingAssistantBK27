"""Shared fixtures for the whole suite.

Everything here runs without LFS, without Windows, without a display and
without a socket (``reference/testing.md``).

The factories take **SI-ish units a human can reason about** -- metres,
degrees, km/h -- and convert to LFS units internally, because mixing the two
is the most common bug class in this project (``reference/conventions.md`` §1).

Angle convention for every ``heading`` / ``direction`` argument below is the
LFS one: **0° = +Y (north), counting anticlockwise**, i.e. exactly what
``CompCar.Heading`` encodes.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.event_bus import EventBus                      # noqa: E402
from core.settings_manager import SettingsManager        # noqa: E402
from vehicles.own_vehicle import OwnVehicle              # noqa: E402
from vehicles.vehicle import Vehicle                     # noqa: E402
import pyinsim                                           # noqa: E402


# ─── Unit conversions (reference/conventions.md §1-§3) ───────────────────────

METRE = 65536.0          # MCI / OutSim position unit per metre
DEG_TO_LFS_HEADING = 65536.0 / 360.0    # 182.044 heading units per degree
KMH_TO_MCI_SPEED = 91.02                # CompCar.Speed word per km/h
KMH_TO_MS = 1.0 / 3.6

# Vehicle.update_position derives acceleration from the **real** time between
# two packets (WP7): a = (delta km/h / 3.6) / dt, smoothed by an exponential
# low-pass whose first sample is taken unfiltered. The factories therefore fake
# exactly one preceding packet NOMINAL_DT_S earlier, which makes a requested
# acceleration come back out unchanged.
NOMINAL_DT_S = 0.1


def metres(value: float) -> int:
    """Metres -> MCI/OutSim position units."""
    return int(round(value * METRE))


def lfs_heading(degrees: float) -> int:
    """Degrees (0 = +Y, anticlockwise) -> CompCar heading word."""
    return int(round((degrees % 360.0) * DEG_TO_LFS_HEADING)) % 65536


def mci_speed(kmh: float) -> int:
    """km/h -> CompCar speed word."""
    return int(round(kmh * KMH_TO_MCI_SPEED))


# ─── EventBus and recording ──────────────────────────────────────────────────

class EventRecorder:
    """Captures ``(event, payload)`` for every event it is subscribed to."""

    def __init__(self, bus: EventBus):
        self._bus = bus
        self.calls = []

    def watch(self, *event_types: str) -> "EventRecorder":
        for event_type in event_types:
            self._bus.subscribe(event_type, self._make_handler(event_type))
        return self

    def _make_handler(self, event_type: str):
        def handler(payload=None):
            self.calls.append((event_type, payload))
        return handler

    # --- queries -------------------------------------------------------
    def payloads(self, event_type: str) -> list:
        return [payload for name, payload in self.calls if name == event_type]

    def last(self, event_type: str):
        payloads = self.payloads(event_type)
        assert payloads, f"no {event_type!r} event was emitted"
        return payloads[-1]

    def count(self, event_type: str) -> int:
        return len(self.payloads(event_type))

    def clear(self):
        self.calls.clear()


@pytest.fixture
def bus() -> EventBus:
    """A real EventBus -- components are only ever wired through it."""
    return EventBus()


@pytest.fixture
def recorder(bus):
    """Factory: ``recorder('a', 'b')`` returns an EventRecorder watching a and b."""
    def _make(*event_types: str) -> EventRecorder:
        return EventRecorder(bus).watch(*event_types)
    return _make


# ─── Settings ────────────────────────────────────────────────────────────────

@pytest.fixture
def make_settings(tmp_path):
    """Factory for a SettingsManager backed by a throwaway file.

    ``make_settings(auto_hold=False)`` starts from the shipped defaults and
    applies the overrides. Writing straight into the dict keeps the factory
    free of file I/O per key; ``save()`` still works and targets tmp_path.
    """
    counter = {'n': 0}

    def _make(**overrides) -> SettingsManager:
        counter['n'] += 1
        path = tmp_path / f"settings_{counter['n']}.json"
        settings = SettingsManager(settings_file=str(path))
        settings._settings.update(overrides)
        return settings

    return _make


@pytest.fixture
def settings(make_settings) -> SettingsManager:
    """A SettingsManager with the shipped defaults."""
    return make_settings()


# ─── Vehicle factories ───────────────────────────────────────────────────────

def _apply_position(vehicle, x, y, z, heading, direction, speed, acceleration):
    if direction is None:
        direction = heading
    # update_position derives the acceleration from the previous sample, so
    # fake one: same position, NOMINAL_DT_S earlier, at the speed the car must
    # have had. The filter is empty at that point, so the first derived value
    # is taken unfiltered and comes out exactly as requested.
    vehicle.previous_speed = speed - acceleration * 3.6 * NOMINAL_DT_S
    vehicle.previous_update_time = 0.0
    vehicle.update_position(
        metres(x), metres(y), metres(z),
        lfs_heading(heading), lfs_heading(direction),
        speed,
        timestamp=NOMINAL_DT_S,
    )


@pytest.fixture
def make_vehicle():
    """Factory for a foreign :class:`Vehicle`.

    x/y/z in **metres**, heading/direction in **degrees** (0 = +Y, CCW),
    speed in **km/h**, acceleration in **m/s²** (negative = braking).

    ``cname`` / ``pname`` are **bytes**, because that is what IS_NPL delivers
    and what the lookup tables are keyed on (``reference/conventions.md`` §4).
    """
    def _make(plid: int = 2, x: float = 0.0, y: float = 0.0, z: float = 0.0,
              heading: float = 0.0, direction: float = None,
              speed: float = 0.0, acceleration: float = 0.0,
              cname: bytes = b"XFG", pname: bytes = b"Tester",
              control_mode: int = 0) -> Vehicle:
        vehicle = Vehicle(plid)
        _apply_position(vehicle, x, y, z, heading, direction, speed, acceleration)
        vehicle.update_model_and_driver(cname, pname, control_mode)
        return vehicle

    return _make


@pytest.fixture
def make_own_vehicle(make_outgauge_packet):
    """Factory for the :class:`OwnVehicle`.

    Same units as ``make_vehicle``. The OutGauge half (pedals, gear, lights) is
    applied through the real ``update_outgauge_data`` so the packet contract is
    exercised too. ``gear`` is the raw OutGauge value: 0 = reverse,
    1 = neutral, 2 = first gear.
    """
    def _make(plid: int = 1, x: float = 0.0, y: float = 0.0, z: float = 0.0,
              heading: float = 0.0, direction: float = None,
              speed: float = 0.0, acceleration: float = 0.0,
              gear: int = 2, rpm: float = 2000.0,
              throttle: float = 0.0, brake: float = 0.0, clutch: float = 0.0,
              cname: bytes = b"XFG", pname: bytes = b"Tester",
              control_mode: int = 0, local_plid: int = None,
              viewed_plid: int = None, **lights) -> OwnVehicle:
        own = OwnVehicle()
        if local_plid is not None:
            # As IS_NPL would: the camera-independent own PLID (WP4).
            own.set_local_driver(local_plid)
        _apply_position(own, x, y, z, heading, direction, speed, acceleration)
        own.update_outgauge_data(make_outgauge_packet(
            plid=plid if viewed_plid is None else viewed_plid,
            speed=speed, gear=gear, rpm=rpm,
            throttle=throttle, brake=brake, clutch=clutch, **lights))
        own.update_model_and_driver(cname, pname, control_mode)
        return own

    return _make


@pytest.fixture
def relate_to_own():
    """Fills in ``distance_to_player`` / ``angle_to_player``, as LFS data would.

    ``VehicleManager._apply_frame`` runs these two updates for every car of an
    MCI frame once the own car has been committed. The factories above build a
    single vehicle in isolation, so a test that exercises code reading those
    two fields -- FCW's distance gate, BSW, CTW -- has to do the same.
    """
    def _relate(own, *vehicles):
        own_data = own.data
        for vehicle in vehicles:
            vehicle.update_distance_to_player(own_data.x, own_data.y, own_data.z)
            vehicle.update_angle_to_player(own_data.x, own_data.y, own_data.heading)
        return vehicles[0] if len(vehicles) == 1 else vehicles

    return _relate


# ─── Fake packets ────────────────────────────────────────────────────────────

class FakePacket:
    """Namespace object standing in for an unpacked pyinsim packet."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __repr__(self):
        fields = ", ".join(f"{k}={v!r}" for k, v in sorted(self.__dict__.items()))
        return f"{type(self).__name__}({fields})"


_SHOW_LIGHT_BITS = {
    'indicator_left': pyinsim.DL_SIGNAL_L,
    'indicator_right': pyinsim.DL_SIGNAL_R,
    'full_beam': pyinsim.DL_FULLBEAM,
    'low_beam': pyinsim.DL_DIPPED,
    'tc': pyinsim.DL_TC,
    'abs': pyinsim.DL_ABS,
    'handbrake': pyinsim.DL_HANDBRAKE,
    'battery': pyinsim.DL_BATTERY,
    'oil': pyinsim.DL_OILWARN,
    'engine': pyinsim.DL_ENGINE,
}


def show_lights(**flags) -> int:
    """Build an OutGauge ``ShowLights`` mask from named booleans.

    Known names: indicator_left, indicator_right, full_beam, low_beam, tc, abs,
    handbrake, battery, oil, engine.
    """
    mask = 0
    for name, on in flags.items():
        if name not in _SHOW_LIGHT_BITS:
            raise KeyError(f"unknown ShowLights flag {name!r}")
        if on:
            mask |= _SHOW_LIGHT_BITS[name]
    return mask


@pytest.fixture
def make_outgauge_packet():
    """Factory for an OutGauge packet. ``speed`` in km/h, pedals 0.0…1.0.

    Light state is given as keywords (``full_beam=True``) or as a raw
    ``show_lights`` mask. ``flags`` is the raw ``OutGaugePack.Flags`` word --
    ``pyinsim.OG_SHIFT`` / ``OG_CTRL`` are the modifier keys the input guard
    reads (``reference/ui.md`` §1.4).
    """
    def _make(plid: int = 1, speed: float = 0.0, gear: int = 2,
              rpm: float = 2000.0, throttle: float = 0.0, brake: float = 0.0,
              clutch: float = 0.0, turbo: float = 0.0, fuel: float = 0.5,
              car: bytes = b"XFG", show_lights_mask: int = 0,
              flags: int = 0,
              **lights) -> FakePacket:
        return FakePacket(
            Time=0,
            Car=car,
            Flags=flags,
            Gear=gear,
            PLID=plid,
            Speed=speed * KMH_TO_MS,      # OutGauge sends m/s
            RPM=rpm,
            Turbo=turbo,
            EngTemp=90.0,
            Fuel=fuel,
            OilPress=0.0,
            OilTemp=90.0,
            DashLights=0,
            ShowLights=show_lights_mask | show_lights(**lights),
            Throttle=throttle,
            Brake=brake,
            Clutch=clutch,
            Display1='',
            Display2='',
            ID=0,
        )

    return _make


@pytest.fixture
def make_compcar():
    """Factory for one ``CompCar`` entry of an MCI packet (metres/degrees/km/h)."""
    def _make(plid: int = 2, x: float = 0.0, y: float = 0.0, z: float = 0.0,
              heading: float = 0.0, direction: float = None,
              speed: float = 0.0, node: int = 0, lap: int = 0,
              position: int = 1, info: int = 0, ang_vel: int = 0) -> FakePacket:
        return FakePacket(
            Node=node,
            Lap=lap,
            PLID=plid,
            Position=position,
            Info=info,
            Sp3=0,
            X=metres(x),
            Y=metres(y),
            Z=metres(z),
            Speed=mci_speed(speed),
            Direction=lfs_heading(heading if direction is None else direction),
            Heading=lfs_heading(heading),
            AngVel=ang_vel,
        )

    return _make


@pytest.fixture
def make_mci_packet():
    """Factory for an ``IS_MCI`` packet from a list of CompCar entries.

    LFS splits more than 16 cars over several packets; build several packets to
    reproduce that.
    """
    def _make(cars) -> FakePacket:
        cars = list(cars)
        return FakePacket(
            Size=4 + 28 * len(cars),
            Type=pyinsim.ISP_MCI,
            ReqI=0,
            NumC=len(cars),
            Info=cars,
        )

    return _make


@pytest.fixture
def make_mci_frame(make_mci_packet):
    """Splits cars into MCI packets and sets CCI_FIRST/CCI_LAST like LFS does.

    LFS carries at most ``MCI_MAX_CARS`` (16) cars per packet and marks the
    first CompCar of the set with ``CCI_FIRST`` and the last one with
    ``CCI_LAST`` (``reference/insim.md`` §2). ``mark=False`` reproduces a
    stream that sets neither bit.
    """
    def _make(cars, chunk: int = 16, mark: bool = True) -> list:
        cars = list(cars)
        if mark and cars:
            cars[0].Info = getattr(cars[0], 'Info', 0) | pyinsim.CCI_FIRST
            cars[-1].Info = getattr(cars[-1], 'Info', 0) | pyinsim.CCI_LAST
        return [make_mci_packet(cars[i:i + chunk])
                for i in range(0, len(cars), chunk)] or [make_mci_packet([])]

    return _make


@pytest.fixture
def make_cim_packet():
    """Factory for an ``IS_CIM`` packet (connection interface mode)."""
    def _make(mode: int = 0, sub_mode: int = 0, sel_type: int = 0,
              ucid: int = 0) -> FakePacket:
        return FakePacket(
            Size=8,
            Type=pyinsim.ISP_CIM,
            ReqI=0,
            UCID=ucid,
            Mode=mode,
            SubMode=sub_mode,
            SelType=sel_type,
            Sp3=0,
        )

    return _make


@pytest.fixture
def make_bfn_packet():
    """Factory for an inbound ``IS_BFN`` packet (LFS cleared our buttons)."""
    def _make(sub_t: int = pyinsim.BFN_USER_CLEAR, ucid: int = 0,
              click_id: int = 0, max_click: int = 0) -> FakePacket:
        return FakePacket(
            Size=8,
            Type=pyinsim.ISP_BFN,
            ReqI=0,
            SubT=sub_t,
            UCID=ucid,
            ClickID=click_id,
            ClickMax=max_click,
            Inst=0,
        )

    return _make


@pytest.fixture
def make_npl_packet():
    """Factory for an ``IS_NPL`` packet (player joined / left the pits).

    ``ucid=0`` is the local connection, ``ptype`` bit 1 marks an AI driver and
    bit 2 a remote player (``reference/conventions.md`` §5.4).

    Note the defaults describe **the local driver**: since WP4 the
    ``VehicleManager`` adopts the first ``ucid=0``/``ptype=0`` player as the
    own car, so it will not appear in ``manager.vehicles``. Build foreign
    players with ``ptype=PTYPE_AI`` or ``ucid=1, ptype=PTYPE_REMOTE``.
    """
    def _make(plid: int = 1, pname: bytes = b"Tester", cname: bytes = b"XFG",
              ucid: int = 0, ptype: int = 0, flags: int = 0,
              plate: bytes = b"        ", sname: bytes = b"XFG") -> FakePacket:
        return FakePacket(
            Size=76,
            Type=pyinsim.ISP_NPL,
            ReqI=0,
            PLID=plid,
            UCID=ucid,
            PType=ptype,
            Flags=flags,
            PName=pname,
            Plate=plate,
            CName=cname,
            SName=sname,
            Tyres=[0, 0, 0, 0],
            H_Mass=0,
            H_TRes=0,
            Model=0,
            Pass=0,
            Spare=0,
            SetF=0,
            NumP=1,
            Sp2=0,
            Sp3=0,
        )

    return _make


@pytest.fixture
def make_pll_packet():
    """Factory for an ``IS_PLL`` packet (player left)."""
    def _make(plid: int = 2) -> FakePacket:
        return FakePacket(Size=1, Type=pyinsim.ISP_PLL, ReqI=0, PLID=plid)

    return _make


# IS_STA flags, as read by lfs/lfs_state.py (reference/ui.md §1).
ISS_GAME = 1
ISS_REPLAY = 2
ISS_PAUSED = 4
ISS_SHIFTU = 8
ISS_DIALOG = 16
ISS_SHIFTU_FOLLOW = 32
ISS_SHIFTU_NO_OPT = 64
ISS_SHOW_2D = 128
ISS_FRONT_END = 256
ISS_MULTI = 512
ISS_MPSPEEDUP = 1024
ISS_WINDOWED = 2048
ISS_SOUND_MUTE = 4096
ISS_VIEW_OVERRIDE = 8192
ISS_VISIBLE = 16384
ISS_TEXT_ENTRY = 32768


@pytest.fixture
def make_sta_packet():
    """Factory for an ``IS_STA`` packet.

    ``on_track=True`` sets ISS_GAME|ISS_VISIBLE and clears ISS_FRONT_END; extra
    flags can be OR-ed in via ``flags``.
    """
    def _make(on_track: bool = True, flags: int = 0, in_game_cam: int = 3,
              view_plid: int = 0, track: bytes = b"SO6R",
              num_p: int = 1, base_flags: int = None) -> FakePacket:
        # base_flags replaces the derived base entirely -- that is the only
        # way to build the main-menu / server-list signature, which sets
        # neither ISS_GAME nor ISS_FRONT_END (reference/ui.md §1.1).
        if base_flags is not None:
            base = base_flags
        else:
            base = (ISS_GAME | ISS_VISIBLE) if on_track else (ISS_FRONT_END | ISS_VISIBLE)
        return FakePacket(
            Size=28,
            Type=pyinsim.ISP_STA,
            ReqI=0,
            Zero=0,
            ReplaySpeed=1.0,
            Flags=base | flags,
            InGameCam=in_game_cam,
            ViewPLID=view_plid,
            NumP=num_p,
            NumConns=1,
            NumFinished=0,
            RaceInProg=0,
            QualMins=0,
            RaceLaps=0,
            Spare2=0,
            Spare3=0,
            Track=track,
            Weather=0,
            Wind=0,
        )

    return _make


# ─── Fake InSim connection ───────────────────────────────────────────────────

class FakeInsim:
    """Records everything that would have gone out over the InSim socket."""

    def __init__(self):
        self.sent = []
        self.bindings = {}

    def send(self, packet_type, **fields):
        self.sent.append((packet_type, fields))

    def sendp(self, packet):
        self.sent.append((getattr(packet, 'Type', None), packet))

    def bind(self, packet_type, handler):
        self.bindings[packet_type] = handler

    def close(self):
        self.sent.append(('close', {}))

    def of_type(self, packet_type) -> list:
        return [fields for sent_type, fields in self.sent if sent_type == packet_type]


class FakeConnector:
    """The slice of ``LFSConnector`` that StateHandler and MessageSender use.

    The button half records what would have gone out, so a test can ask what
    is currently on screen without a socket.
    """

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.debug = False
        self.insim = FakeInsim()
        self.is_connected = True
        self.outgauge_restarts = 0
        self.buttons = []        # (click_id, style, t, l, w, h, text, inst)
        self.deletes = []        # click_id
        self.clears = 0
        self.commands = []
        self.messages = []

    def start_outgauge(self):
        self.outgauge_restarts += 1

    def start_outsim(self):
        pass

    # --- button path ---------------------------------------------------
    def send_button(self, click_id, style, t, l, w, h, text, inst=0):
        self.buttons.append((click_id, style, t, l, w, h, text, inst))
        return True

    def delete_button(self, click_id):
        self.deletes.append(click_id)
        return True

    def clear_all_buttons(self):
        self.clears += 1
        return True

    def send_command_to_lfs(self, command):
        self.commands.append(command)
        return True

    def send_local_message_to_lfs(self, message):
        self.messages.append(message)
        return True

    # --- queries -------------------------------------------------------
    def drawn_ids(self) -> set:
        """Every ClickID an IS_BTN was sent for."""
        return {entry[0] for entry in self.buttons}

    def last_button(self, click_id):
        """The most recent IS_BTN for one ClickID, or None."""
        for entry in reversed(self.buttons):
            if entry[0] == click_id:
                return entry
        return None

    def reset(self):
        self.buttons.clear()
        self.deletes.clear()
        self.clears = 0


@pytest.fixture
def message_sender(fake_connector):
    """A real :class:`MessageSender` on top of the recording connector."""
    from lfs.message_sender import MessageSender
    return MessageSender(fake_connector)


@pytest.fixture
def fake_insim() -> FakeInsim:
    return FakeInsim()


@pytest.fixture
def fake_connector(bus) -> FakeConnector:
    """A connector stub wired to the test EventBus -- no socket involved."""
    return FakeConnector(bus)
