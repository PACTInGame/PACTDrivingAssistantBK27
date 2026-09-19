"""WP10 -- AI traffic: route search cost, hostile route data, and the start prompt.

Three things are pinned here:

* the **windowed** nearest-point search (``known-issues.md`` #9) must return
  what the old full scan returned for a car driving its route -- across the
  wrap point of a closed loop included -- and must resync by itself when the
  car is somewhere else entirely (respawn, ``/restart``, teleport);
* ``track_data_XX.json`` is treated as hostile input: every shape violation
  becomes a ``RouteDataError`` with a readable message, and the start handler
  turns that into a notification instead of raising inside the packet handler
  that delivered the menu click;
* starting traffic reloads the layout and restarts the race, so the menu asks
  first.

Everything runs without LFS. The route files in ``track_data/`` are real data
and are used as such; the 2000-point route of the performance guard is built
here, because no shipped route is that long.
"""

import glob
import json
import math
import os
import time

import pytest

from assistance.AI_Driver import (AIDriver, RouteDataError, _scan_whole_path,
                                  analyze_upcoming_track, calculate_angle,
                                  get_closest_index_on_route,
                                  load_routes_from_file,
                                  distance_to_route_sq,
                                  OFF_ROUTE_DISTANCE_M,
                                  ROUTE_SEARCH_WINDOW)
from tests.conftest import lfs_heading, metres
from ui.menu_system import BTN_CLOSE, MenuSystem
from ui.ui_manager import UIManager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACK_DATA = sorted(glob.glob(os.path.join(PROJECT_ROOT, 'track_data', '*.json')))


# ─── Helpers ─────────────────────────────────────────────────────────────────

class FakeAIController:
    """Records what ``AIDriver`` sends instead of talking to InSim."""

    def __init__(self):
        self.commands = []        # (plid, AIControlState)
        self.stopped = []         # plid
        self.info_requests = []   # (plid, repeat_interval)
        self.handlers = {}        # plid -> callback

    def control_ai(self, plid, state):
        self.commands.append((plid, state))

    def stop_ai_control(self, plid):
        self.stopped.append(plid)

    def reset_ai_controls(self, plid):
        pass

    def bind_ai_info_handler(self, plid, handler):
        self.handlers[plid] = handler

    def request_ai_info(self, plid, repeat_interval=None):
        self.info_requests.append((plid, repeat_interval))

    def plids(self):
        return {plid for plid, _state in self.commands}


class _FakeAII:
    """One IS_AII, as ``AIDriver.monitor_ai`` reads it."""

    def __init__(self, plid, rpm=3000.0, gear=3):
        self.PLID = plid
        self.RPM = rpm
        self.Gear = gear


def circle_route(road_id=20, point_count=2000, radius=1591.5):
    """A closed loop with realistic point spacing (~5 m at the default size)."""
    path = [(radius * math.cos(2 * math.pi * i / point_count),
             radius * math.sin(2 * math.pi * i / point_count),
             0.0)
            for i in range(point_count)]
    path.append(path[0])          # closed loops repeat their first point
    return {'road_id': road_id, 'closed_loop': True, 'path': path}


def straight_route(road_id=20, point_count=100, spacing=5.0):
    return {'road_id': road_id, 'closed_loop': False,
            'path': [(i * spacing, 0.0, 0.0) for i in range(point_count)]}


def valid_payload():
    return {
        'roads': [{'road_id': 20, 'closed_loop': False,
                   'path': [[0, 0, 0], [1, 0, 0], [2, 0, 0]]}],
        'markers': [{'type': 'stop_line', 'position': [1, 0, 0]}],
    }


def write_json(tmp_path, payload, name='track_data_XX.json'):
    path = tmp_path / name
    if isinstance(payload, str):
        path.write_text(payload, encoding='utf-8')
    else:
        path.write_text(json.dumps(payload), encoding='utf-8')
    return str(path)


@pytest.fixture
def controller():
    return FakeAIController()


@pytest.fixture
def driver(bus, settings, controller):
    """An ``AIDriver`` with a recording AI controller bound, as LFS would."""
    driver = AIDriver(bus, settings)
    bus.emit('AI_Controller_initialized', controller)
    return driver


@pytest.fixture
def active_driver(driver):
    """Route data loaded and the state machine in STATE_ACTIVE."""
    driver.current_track = 'SO7'
    driver.routes = {20: straight_route(point_count=200)}
    driver.state = AIDriver.STATE_ACTIVE
    return driver


@pytest.fixture
def ai_car(make_vehicle):
    def _make(plid=2, **kwargs):
        vehicle = make_vehicle(plid=plid, pname=f"AI {plid}".encode(), **kwargs)
        vehicle.data.is_ai = True
        return vehicle
    return _make


# ─── The windowed route search ───────────────────────────────────────────────

def _d_sq(point, x, y, z):
    return (point[0] - x) ** 2 + (point[1] - y) ** 2 + (point[2] - z) ** 2


@pytest.mark.parametrize("path_file", TRACK_DATA, ids=lambda p: os.path.basename(p))
def test_the_windowed_search_matches_a_full_scan_along_every_shipped_route(path_file):
    """A car driving its route sees exactly what the full scan saw.

    Walked point by point over every road of a real track file, one and a half
    laps, so the wrap point of each closed loop is crossed. Ties are allowed:
    a closed loop repeats its first point as its last, so index 0 and index
    n-1 are the same place -- the assertion is on the distance, not the index.
    """
    roads, _markers = load_routes_from_file(path_file)
    assert roads

    for road in roads:
        path = road['path']
        previous = None
        for step in range(int(len(path) * 1.5)):
            index = step % len(path)
            # A point between two route points, a metre off the centre line.
            here, following = path[index], path[(index + 1) % len(path)]
            x = (here[0] + following[0]) / 2 + 1.0
            y = (here[1] + following[1]) / 2 - 1.0
            z = here[2]

            windowed = get_closest_index_on_route(x, y, z, road, previous_index=previous)
            full = _scan_whole_path(path, x, y, z)[0]

            assert _d_sq(path[windowed], x, y, z) == pytest.approx(
                _d_sq(path[full], x, y, z), abs=1e-6), (
                f"{os.path.basename(path_file)} road {road['road_id']} "
                f"step {step}: windowed {windowed} != full {full}")
            previous = windowed


def test_the_search_wraps_around_the_end_of_a_closed_loop():
    route = circle_route(point_count=200)
    path = route['path']
    last = len(path) - 2          # one before the repeated closing point

    # Sitting on the second point, having been near the end last cycle.
    index = get_closest_index_on_route(path[1][0], path[1][1], path[1][2],
                                       route, previous_index=last)

    assert index in (1,)          # not the clamped edge, not a full-scan reset


def test_an_open_route_does_not_wrap():
    """Without closed_loop the window is clamped, it does not jump to the end."""
    route = straight_route(point_count=100)
    path = route['path']

    index = get_closest_index_on_route(path[0][0], path[0][1], path[0][2],
                                       route, previous_index=0)

    assert index == 0


def test_a_teleported_car_finds_the_far_side_of_the_route():
    """/restart, respawn or a shift+U jump: the cached index is worthless."""
    route = straight_route(point_count=400)
    path = route['path']
    target = 300
    assert abs(target - 5) > ROUTE_SEARCH_WINDOW

    index = get_closest_index_on_route(path[target][0], path[target][1], path[target][2],
                                       route, previous_index=5)

    assert index == target


def test_a_car_far_off_its_route_still_gets_the_nearest_point():
    route = straight_route(point_count=400)

    index = get_closest_index_on_route(1000.0, 300.0, 0.0, route, previous_index=5)

    assert index == _scan_whole_path(route['path'], 1000.0, 300.0, 0.0)[0]


def test_the_old_full_scan_signature_still_works():
    """Existing callers pass three coordinates and a route dict, nothing else."""
    route = straight_route(point_count=50)

    assert get_closest_index_on_route(52.0, 1.0, 0.0, route) == 10


def test_an_empty_path_is_index_zero_not_an_exception():
    assert get_closest_index_on_route(1.0, 2.0, 3.0, {'path': []}) == 0
    assert get_closest_index_on_route(1.0, 2.0, 3.0, {}, previous_index=7) == 0


def test_analyze_upcoming_track_survives_an_empty_lookahead():
    curvature, target = analyze_upcoming_track([])

    assert curvature == 0.0
    assert target == (0.0, 0.0, 0.0)


# ─── Route data validation ───────────────────────────────────────────────────

@pytest.mark.parametrize("path_file", TRACK_DATA, ids=lambda p: os.path.basename(p))
def test_the_shipped_route_files_pass_validation(path_file):
    roads, markers = load_routes_from_file(path_file)

    assert roads and markers
    for road in roads:
        assert isinstance(road['road_id'], int)
        assert len(road['path']) >= 2
        assert all(isinstance(v, float) for v in road['path'][0])
    for marker in markers:
        assert marker['type'] in ('stop_line', 'arrow_left', 'arrow_right')
        assert len(marker['position']) == 3


def test_an_inverted_road_is_reversed_on_load(tmp_path):
    payload = valid_payload()
    payload['roads'][0]['inverted'] = True

    roads, _markers = load_routes_from_file(write_json(tmp_path, payload))

    assert roads[0]['path'] == [(2.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 0.0)]


BROKEN_PAYLOADS = {
    'not json': '{ this is not json',
    'top level list': [],
    'roads not a list': {'roads': {'20': []}},
    'road not an object': {'roads': ['20']},
    'road without id': {'roads': [{'path': [[0, 0, 0], [1, 0, 0]]}]},
    'road id not an int': {'roads': [{'road_id': 'twenty', 'path': [[0, 0, 0], [1, 0, 0]]}]},
    'duplicate road id': {'roads': [{'road_id': 20, 'path': [[0, 0, 0], [1, 0, 0]]},
                                    {'road_id': 20, 'path': [[0, 0, 0], [1, 0, 0]]}]},
    'road without path': {'roads': [{'road_id': 20}]},
    'path too short': {'roads': [{'road_id': 20, 'path': [[0, 0, 0]]}]},
    'point too short': {'roads': [{'road_id': 20, 'path': [[0, 0], [1, 0, 0]]}]},
    'point not numeric': {'roads': [{'road_id': 20, 'path': [["x", 0, 0], [1, 0, 0]]}]},
    'point is a string': {'roads': [{'road_id': 20, 'path': ["000", [1, 0, 0]]}]},
    'markers not a list': {'roads': [], 'markers': 3},
    'marker not an object': {'roads': [], 'markers': ["stop_line"]},
    'marker without position': {'roads': [], 'markers': [{'type': 'stop_line'}]},
}


@pytest.mark.parametrize("name", sorted(BROKEN_PAYLOADS))
def test_malformed_route_data_raises_a_readable_error(tmp_path, name):
    path = write_json(tmp_path, BROKEN_PAYLOADS[name])

    with pytest.raises(RouteDataError) as raised:
        load_routes_from_file(path)

    assert os.path.basename(path) in str(raised.value)


def test_a_missing_route_file_raises_route_data_error(tmp_path):
    with pytest.raises(RouteDataError):
        load_routes_from_file(str(tmp_path / 'nope.json'))


def test_an_unknown_marker_type_is_ignored_not_fatal(tmp_path):
    payload = valid_payload()
    payload['markers'].append({'type': 'roundabout', 'position': [0, 0, 0]})

    _roads, markers = load_routes_from_file(write_json(tmp_path, payload))

    assert [m['type'] for m in markers] == ['stop_line']


def test_broken_route_data_notifies_instead_of_raising(driver, bus, recorder,
                                                       tmp_path, monkeypatch):
    """The start handler runs on the InSim packet thread -- it must not raise."""
    seen = recorder('notification', 'send_command_to_lfs', 'ai_traffic_state_changed')
    broken = write_json(tmp_path, '{ not json at all')
    monkeypatch.setattr('assistance.AI_Driver.resolve_path',
                        lambda *parts: broken)
    driver.current_track = 'SO7'

    driver._on_start()

    assert driver.state == AIDriver.STATE_INACTIVE
    assert seen.count('send_command_to_lfs') == 0, "the race was restarted for nothing"
    assert seen.count('ai_traffic_state_changed') == 0
    assert seen.count('notification') == 1


def test_a_valid_file_starts_traffic_and_loads_the_layout(driver, bus, recorder,
                                                          tmp_path, monkeypatch):
    seen = recorder('send_command_to_lfs', 'ai_traffic_state_changed')
    good = write_json(tmp_path, valid_payload())
    monkeypatch.setattr('assistance.AI_Driver.resolve_path', lambda *parts: good)
    driver.current_track = 'SO7'

    driver._on_start()

    assert driver.state == AIDriver.STATE_ACTIVE
    assert seen.payloads('send_command_to_lfs') == ['/axload AI_Traffic', '/restart']
    assert seen.last('ai_traffic_state_changed') == {'active': True}


def test_a_track_without_route_data_is_refused(driver, recorder, tmp_path, monkeypatch):
    seen = recorder('notification', 'send_command_to_lfs')
    monkeypatch.setattr('assistance.AI_Driver.resolve_path',
                        lambda *parts: str(tmp_path / 'missing.json'))
    driver.current_track = 'SO7'

    driver._on_start()

    assert driver.state == AIDriver.STATE_INACTIVE
    assert seen.count('send_command_to_lfs') == 0
    assert seen.count('notification') == 1


def test_a_wrong_track_configuration_is_refused_with_a_hint(driver, recorder, monkeypatch):
    seen = recorder('notification', 'send_command_to_lfs')
    monkeypatch.setattr('assistance.AI_Driver.resolve_path',
                        lambda *parts: os.path.join(PROJECT_ROOT, 'track_data',
                                                    'track_data_SO.json'))
    driver.current_track = 'SO6R'          # City is SO7, not SO6R

    driver._on_start()

    assert driver.state == AIDriver.STATE_INACTIVE
    assert seen.count('send_command_to_lfs') == 0
    hints = [p['notification'] for p in seen.payloads('notification')]
    assert any('Select City' in hint for hint in hints)


# ─── Who gets driven ─────────────────────────────────────────────────────────

def test_a_human_called_MAIK_is_not_adopted(active_driver, make_own_vehicle, make_vehicle):
    """PType bit 1 is the AI flag; the old ``'AI' in pname`` took over humans."""
    own = make_own_vehicle(plid=1, local_plid=1)
    maik = make_vehicle(plid=2, pname=b"MAIK", x=0.0, y=0.0)
    assert maik.data.is_ai is False

    active_driver._process_active(own, {2: maik})

    assert active_driver.assigned_routes == {}
    assert maik.current_route is None


def test_a_real_ai_car_is_adopted(active_driver, make_own_vehicle, ai_car):
    own = make_own_vehicle(plid=1, local_plid=1)
    ai = ai_car(plid=3, x=10.0, y=0.0)

    active_driver._process_active(own, {3: ai})

    assert active_driver.assigned_routes == {3: 20}
    assert ai.current_route == 20


def test_the_ai_flag_comes_from_the_npl_packet(make_vehicle):
    """A car named MAIK with PType AI *is* an AI car -- the name never decides."""
    from vehicles.vehicle import PTYPE_AI
    vehicle = make_vehicle(plid=4, pname=b"MAIK")
    vehicle.update_model_and_driver(b"XFG", b"MAIK", 0, ucid=0, ptype=PTYPE_AI)

    assert AIDriver._is_local_ai_vehicle(None, vehicle) is True


def test_an_adopted_car_gets_a_repeating_ai_info_request(active_driver, controller,
                                                         make_own_vehicle, ai_car):
    own = make_own_vehicle(plid=1, local_plid=1)

    active_driver._process_active(own, {3: ai_car(plid=3, x=10.0, y=0.0)})

    assert controller.info_requests == [(3, 100)]
    assert 3 in controller.handlers


def test_a_departed_car_is_forgotten(active_driver, make_own_vehicle, ai_car):
    own = make_own_vehicle(plid=1, local_plid=1)
    ai = ai_car(plid=3, x=10.0, y=0.0)
    active_driver._process_active(own, {3: ai})
    assert active_driver._route_index

    active_driver._process_active(own, {})

    assert active_driver.assigned_routes == {}
    assert active_driver._route_index == {}


# ─── Track changes and the routes/worker race ────────────────────────────────

def test_a_track_change_drops_running_traffic_without_sending(active_driver, controller,
                                                              recorder, make_own_vehicle,
                                                              ai_car):
    """The cars of the old track are gone; braking them is 20 chat lines.

    A graceful stop would spend two seconds sending brake packets to PLIDs
    LFS no longer knows, and answer each one with "IS_AIC - no driver to
    control". So a track change lets go instead of stopping.
    """
    own = make_own_vehicle(plid=1, local_plid=1)
    active_driver._process_active(own, {3: ai_car(plid=3, x=10.0, y=0.0)})
    sent_before = len(controller.commands)
    seen = recorder('ai_traffic_state_changed')

    active_driver._on_state_data({'track': 'BL1X'})

    assert active_driver.state == AIDriver.STATE_INACTIVE
    assert active_driver.assigned_routes == {}
    assert active_driver.routes is None
    assert controller.commands[sent_before:] == []
    assert controller.stopped == []
    assert seen.last('ai_traffic_state_changed') == {'active': False}


def test_leaving_the_race_drops_running_traffic_without_sending(active_driver, controller,
                                                                make_own_vehicle, ai_car):
    """The end of the run in scenario 20: ESC, exit, back to the menu."""
    own = make_own_vehicle(plid=1, local_plid=1)
    active_driver._on_state_data({'on_track': True, 'track': 'SO7'})
    active_driver._process_active(own, {3: ai_car(plid=3, x=10.0, y=0.0)})
    sent_before = len(controller.commands)

    active_driver._on_state_data({'on_track': False, 'track': 'SO7'})

    assert active_driver.state == AIDriver.STATE_INACTIVE
    assert active_driver.assigned_routes == {}
    assert controller.commands[sent_before:] == []
    assert controller.stopped == []


def test_nothing_is_sent_to_a_car_that_is_no_longer_reported(active_driver, controller,
                                                             make_own_vehicle, ai_car):
    """The one pass in which a race ends used to send an IS_AIC per car.

    ``_process_active`` re-requested AI info for every assigned PLID *before*
    it noticed that they had all disappeared -- one IS_AIC each, answered with
    "no driver to control", once for the whole field.
    """
    own = make_own_vehicle(plid=1, local_plid=1)
    active_driver._process_active(own, {3: ai_car(plid=3, x=10.0, y=0.0)})
    # Old enough that the AI-info watchdog would fire this pass.
    active_driver._last_ai_info_time[3] = time.time() - 10.0
    sent_before = len(controller.commands)
    requests_before = len(controller.info_requests)

    active_driver._process_active(own, {})

    assert controller.commands[sent_before:] == []
    assert controller.info_requests[requests_before:] == []
    assert active_driver.assigned_routes == {}


def test_a_late_ai_info_packet_is_not_answered(active_driver, controller,
                                               make_own_vehicle, ai_car):
    """The repeating IS_AII request outlives the stop; the answer must not."""
    own = make_own_vehicle(plid=1, local_plid=1)
    active_driver._process_active(own, {3: ai_car(plid=3, x=10.0, y=0.0)})
    active_driver._on_stop()
    active_driver.process(own, {})          # no cars left -> finalises at once
    sent_before = len(controller.commands)

    active_driver.monitor_ai(_FakeAII(plid=3, rpm=200, gear=1))

    assert active_driver.state == AIDriver.STATE_INACTIVE
    assert controller.commands[sent_before:] == []


def test_the_local_driver_is_never_adopted_even_without_an_npl(active_driver, controller,
                                                               make_own_vehicle, ai_car):
    """OutGauge points at whichever car the camera is on.

    With ``local_plid`` known, the own vehicle carries that PLID and is a
    plain obstacle. Nothing about a TAB may put it under AI control.
    """
    own = make_own_vehicle(plid=1, local_plid=1, viewed_plid=3, x=0.0, y=30.0)
    ai = ai_car(plid=3, x=10.0, y=0.0)

    active_driver._process_active(own, {3: ai})

    assert 1 not in active_driver.assigned_routes
    assert active_driver.assigned_routes == {3: 20}


def test_a_state_packet_without_a_track_changes_nothing(active_driver):
    """An IS_STA with no usable track must not stop traffic (hostile input)."""
    active_driver._on_state_data({'on_track': True})
    active_driver._on_state_data({'track': ''})
    active_driver._on_state_data({'track': b''})

    assert active_driver.state == AIDriver.STATE_ACTIVE
    assert active_driver.current_track == 'SO7'
    assert active_driver.routes is not None


def test_the_same_track_again_keeps_the_loaded_routes(active_driver):
    routes = active_driver.routes

    active_driver._on_state_data({'track': 'SO7'})

    assert active_driver.routes is routes
    assert active_driver.state == AIDriver.STATE_ACTIVE


def test_routes_dropped_mid_pass_do_not_crash_the_cycle(active_driver, make_own_vehicle,
                                                        ai_car):
    """``_on_state_data`` runs on the packet thread while ``process`` runs on a
    worker: the pass must bind ``self.routes`` once instead of dereferencing it
    twice (``conventions.md`` §6)."""
    own = make_own_vehicle(plid=1, local_plid=1)
    ai = ai_car(plid=3, x=10.0, y=0.0)
    active_driver._process_active(own, {3: ai})

    class DropsRoutes(dict):
        def get(self, key, default=None):
            active_driver.routes = None      # the other thread, mid-pass
            return dict.get(self, key, default)

    active_driver.routes = DropsRoutes(active_driver.routes)

    result = active_driver._process_active(own, {3: ai})

    assert result == {'ai_active': True}


# ─── The long-straight cache ─────────────────────────────────────────────────

def test_the_long_straight_analysis_is_cached_per_route_point(active_driver,
                                                              make_own_vehicle, ai_car):
    own = make_own_vehicle(plid=1, local_plid=1)
    ai = ai_car(plid=3, x=10.0, y=0.0)

    active_driver._process_active(own, {3: ai})
    entries_after_first = dict(active_driver._straight_cache)
    active_driver._process_active(own, {3: ai})

    assert entries_after_first
    assert active_driver._straight_cache == entries_after_first


def test_loading_a_route_file_clears_the_cache(driver, monkeypatch, tmp_path):
    driver._straight_cache[(20, 5)] = True
    monkeypatch.setattr('assistance.AI_Driver.resolve_path',
                        lambda *parts: write_json(tmp_path, valid_payload()))
    driver.current_track = 'SO7'

    assert driver._load_routes() is True
    assert driver._straight_cache == {}


# ─── Stop sequence ───────────────────────────────────────────────────────────

def test_stopping_brakes_then_hands_the_cars_back(active_driver, controller,
                                                  make_own_vehicle, ai_car):
    own = make_own_vehicle(plid=1, local_plid=1)
    ai = ai_car(plid=3, x=10.0, y=0.0)
    active_driver._process_active(own, {3: ai})

    active_driver._on_stop()
    for _ in range(active_driver.STOP_BRAKE_CYCLES):
        active_driver.process(own, {3: ai})

    assert controller.stopped == [3]
    assert active_driver.state == AIDriver.STATE_INACTIVE
    assert active_driver.assigned_routes == {}
    assert active_driver._route_index == {}
    assert ai.current_route is None


# --- Collision avoidance: the car in front ----------------------------------
#
# The corridor, the safe-following law and the emergency threshold together are
# what decides whether an AI car stops behind the one ahead or touches it. All
# three are pure geometry and pure kinematics, so they are checked as such -
# no LFS, no route, no packets.


def _brake_and_throttle(controller, plid):
    """Last throttle/brake this PLID was sent."""
    for other, state in reversed(controller.commands):
        if other == plid and state.brake is not None:
            return state.throttle, state.brake
    raise AssertionError(f"nothing was sent to vehicle {plid}")


def test_the_heading_vector_agrees_with_the_steering_angle(driver):
    """Both describe "straight ahead"; a sign error here points the whole
    collision check backwards."""
    for degrees in (0.0, 37.0, 90.0, 180.0, 271.0):
        heading = lfs_heading(degrees)
        forward_x, forward_y = AIDriver.heading_vector(heading)
        # A point 10 m along that vector must sit at steering angle 0.
        angle = calculate_angle(metres(0.0), metres(0.0),
                                10.0 * forward_x, 10.0 * forward_y, heading)
        assert abs(angle) < 0.5, f"{degrees} deg -> steering angle {angle:.2f}"


def test_a_car_half_a_width_off_centre_at_five_metres_is_seen(driver):
    """What the +-12 deg cone missed.

    At 5 m a +-12 deg cone is +-1.06 m wide - narrower than one car - so a
    stopped car offset by 1.2 m was invisible until contact.
    """
    forward = AIDriver.heading_vector(lfs_heading(0.0))       # +Y
    obstacles = [(2, 1.2, 5.0, 0.0)]

    gap, lead = driver._closest_obstacle_ahead(1, 0.0, 0.0, *forward, obstacles)

    assert gap == pytest.approx(5.0)
    assert lead == 0.0
    # ... and the cone really would have missed it.
    assert math.degrees(math.atan2(1.2, 5.0)) > 12.0


@pytest.mark.parametrize("x, y, why", [
    (0.0, -10.0, "behind"),
    (9.0, 20.0, "in the next street over"),
    (0.0, 80.0, "beyond the detection distance"),
])
def test_the_corridor_ignores_what_is_not_in_the_way(driver, x, y, why):
    forward = AIDriver.heading_vector(lfs_heading(0.0))

    gap, _lead = driver._closest_obstacle_ahead(1, 0.0, 0.0, *forward,
                                                [(2, x, y, 0.0)])

    assert gap == float('inf'), why


def test_the_nearest_car_in_the_corridor_wins(driver):
    forward = AIDriver.heading_vector(lfs_heading(0.0))
    obstacles = [(2, 0.0, 30.0, 50.0), (3, 0.5, 12.0, 20.0), (4, 0.0, 45.0, 0.0)]

    gap, lead = driver._closest_obstacle_ahead(1, 0.0, 0.0, *forward, obstacles)

    assert gap == pytest.approx(12.0)
    assert lead == pytest.approx(20.0)


def test_the_safe_following_speed_stops_at_the_standstill_gap(driver):
    assert driver._calculate_following_speed(driver.CA_STANDSTILL_GAP, 0.0) == 0.0
    assert driver._calculate_following_speed(0.0, 0.0) == 0.0


def test_the_safe_following_speed_is_a_real_braking_distance(driver):
    """From the speed it allows, comfort braking must still fit in the gap."""
    for gap in (8.0, 12.0, 20.0, 35.0, 50.0):
        allowed = driver._calculate_following_speed(gap, 0.0) / 3.6
        # Reaction time at constant speed, then constant deceleration.
        travelled = (allowed * driver.CA_REACTION_TIME
                     + allowed * allowed / (2.0 * driver.CA_COMFORT_DECEL))
        assert travelled <= gap - driver.CA_STANDSTILL_GAP + 1e-6, (
            f"{gap} m: allows {allowed * 3.6:.1f} km/h but needs "
            f"{travelled + driver.CA_STANDSTILL_GAP:.1f} m")


def test_following_a_moving_car_is_not_braked_to_a_crawl(driver):
    """The old linear law ignored the speed of the car ahead entirely: two
    cars cruising 25 m apart at 60 km/h were pushed down to 26 km/h."""
    allowed = driver._calculate_following_speed(25.0, 60.0)

    assert allowed > 60.0


def test_an_ai_car_brakes_fully_for_a_standing_car_in_its_path(
        active_driver, controller, make_own_vehicle, ai_car, make_vehicle):
    """The scenario the run ends on: a car in the way that is not moving."""
    own = make_own_vehicle(plid=1, local_plid=1, x=-100.0, y=-100.0)
    # heading 90 deg = -X ... use 270 deg so the car drives along +X, which is
    # where the straight test route runs.
    follower = ai_car(plid=3, x=0.0, y=0.0, heading=270.0, speed=80.0)
    standing = make_vehicle(plid=4, x=9.0, y=0.0, heading=270.0, speed=0.0)

    active_driver._process_active(own, {3: follower, 4: standing})

    throttle, brake = _brake_and_throttle(controller, 3)
    assert brake == 100
    assert throttle == 0


def test_the_players_car_is_an_obstacle_like_any_other(
        active_driver, controller, make_own_vehicle, ai_car):
    """The player was checked in a branch that switched itself off whenever
    the own PLID happened to be one of the adopted cars."""
    own = make_own_vehicle(plid=1, local_plid=1, x=9.0, y=0.0, speed=0.0)
    follower = ai_car(plid=3, x=0.0, y=0.0, heading=270.0, speed=80.0)

    active_driver._process_active(own, {3: follower})

    throttle, brake = _brake_and_throttle(controller, 3)
    assert brake == 100
    assert throttle == 0


def test_a_clear_road_is_not_braked_for(active_driver, controller,
                                        make_own_vehicle, ai_car):
    own = make_own_vehicle(plid=1, local_plid=1, x=-100.0, y=-100.0)
    lonely = ai_car(plid=3, x=0.0, y=0.0, heading=270.0, speed=30.0)

    active_driver._process_active(own, {3: lonely})

    throttle, brake = _brake_and_throttle(controller, 3)
    assert brake == 0
    assert throttle > 0


# --- Route assignment survives a restart -------------------------------------

def test_a_car_teleported_off_its_route_is_re_assigned(active_driver, make_own_vehicle,
                                                       ai_car):
    """``/restart`` puts every car back on the grid. The assignment used to be
    permanent, so a car adopted a moment earlier kept steering towards a road
    it was no longer anywhere near."""
    active_driver.routes = {20: straight_route(point_count=200),
                            21: {'road_id': 21, 'closed_loop': False,
                                 'path': [(0.0, 500.0 + i * 5.0, 0.0) for i in range(100)]}}
    own = make_own_vehicle(plid=1, local_plid=1, x=-100.0, y=-100.0)
    car = ai_car(plid=3, x=10.0, y=0.0, heading=270.0, speed=30.0)
    active_driver._process_active(own, {3: car})
    assert active_driver.assigned_routes == {3: 20}

    # Teleported onto the other road.
    moved = ai_car(plid=3, x=0.0, y=600.0, heading=0.0, speed=30.0)
    for _ in range(active_driver.OFF_ROUTE_REASSIGN_CYCLES):
        active_driver._process_active(own, {3: moved})

    assert active_driver.assigned_routes == {3: 21}


@pytest.mark.parametrize("path_file", TRACK_DATA, ids=lambda p: os.path.basename(p))
def test_no_shipped_road_ever_looks_off_route_to_a_car_driving_it(path_file):
    """The false positive this guards against is **deterministic**.

    Point spacing in the shipped files runs from 1.1 m to 41.3 m. Judged by
    the nearest stored *point*, a car driving down the middle of the longest
    gap on SO road 33 is 20.6 m away -- 4.4 m under the threshold, at the same
    place on the same corner every single lap, and a slightly sparser hand-drawn
    road would cross it. Judged against the road, the same car is ~0 m away.

    Walked over every segment of every shipped road, at the midpoint and at
    both quarter points, offset 3 m sideways for good measure.
    """
    roads, _markers = load_routes_from_file(path_file)
    assert roads

    limit_sq = OFF_ROUTE_DISTANCE_M ** 2
    worst = 0.0
    for road in roads:
        path = road['path']
        closed = road.get('closed_loop', False)
        for index in range(len(path) - 1):
            here, following = path[index], path[index + 1]
            length = math.dist(here, following)
            if length == 0.0:
                continue
            # A unit normal in the ground plane, so the sample sits beside the
            # centre line rather than on it.
            nx = -(following[1] - here[1]) / length
            ny = (following[0] - here[0]) / length
            for fraction in (0.25, 0.5, 0.75):
                x = here[0] + (following[0] - here[0]) * fraction + 3.0 * nx
                y = here[1] + (following[1] - here[1]) * fraction + 3.0 * ny
                z = here[2] + (following[2] - here[2]) * fraction

                nearest = _scan_whole_path(path, x, y, z)[0]
                distance_sq = distance_to_route_sq(path, nearest, x, y, z,
                                                   closed_loop=closed)
                worst = max(worst, distance_sq)
                assert distance_sq <= limit_sq, (
                    f"{os.path.basename(path_file)} road {road['road_id']} "
                    f"segment {index} ({length:.1f} m long): a car on it reads "
                    f"{math.sqrt(distance_sq):.1f} m off route")

    # And it is not merely under the limit -- it is nowhere near it.
    assert math.sqrt(worst) < 5.0, f"worst {math.sqrt(worst):.1f} m off the road"


def test_the_off_route_test_does_not_depend_on_point_spacing():
    """Two roads, same geometry, one sampled 200x more coarsely."""
    dense = [(i * 0.5, 0.0, 0.0) for i in range(401)]     # 0.5 m apart
    sparse = [(0.0, 0.0, 0.0), (200.0, 0.0, 0.0)]         # 200 m apart

    # A car in the middle of the road, 2 m off the centre line.
    x, y, z = 100.0, 2.0, 0.0

    dense_index = _scan_whole_path(dense, x, y, z)[0]
    sparse_index = _scan_whole_path(sparse, x, y, z)[0]

    assert distance_to_route_sq(dense, dense_index, x, y, z) == pytest.approx(4.0)
    assert distance_to_route_sq(sparse, sparse_index, x, y, z) == pytest.approx(4.0)
    # What the old nearest-point measure would have said about the sparse one:
    assert _scan_whole_path(sparse, x, y, z)[1] == pytest.approx(100.0 ** 2 + 4.0)


def test_a_sparsely_sampled_route_is_not_re_assigned_in_the_middle(
        active_driver, make_own_vehicle, ai_car):
    """The end-to-end version: a road drawn with 200 m between its points must
    not throw the car off it halfway along."""
    active_driver.routes = {20: {'road_id': 20, 'closed_loop': False,
                                 'path': [(0.0, 0.0, 0.0), (200.0, 0.0, 0.0),
                                          (400.0, 0.0, 0.0)]}}
    own = make_own_vehicle(plid=1, local_plid=1, x=-500.0, y=-500.0)

    for step in range(20):
        # Creeping along the middle of the long gap, 2 m off the centre line.
        car = ai_car(plid=3, x=90.0 + step, y=2.0, heading=270.0, speed=36.0)
        active_driver._process_active(own, {3: car})

    assert active_driver.assigned_routes == {3: 20}
    assert not active_driver._off_route.get(3)


def test_a_closed_loop_is_measured_across_its_seam(active_driver):
    """The two segments either side of the nearest point include the one that
    wraps; without that, every closed loop would read as off-route at its seam.
    """
    route = circle_route(point_count=8, radius=100.0)   # 76 m between points
    path = route['path']

    # Halfway along the seam segment, between the last point and the first.
    last = path[-2]
    first = path[0]
    x = (last[0] + first[0]) / 2
    y = (last[1] + first[1]) / 2
    z = 0.0
    nearest = _scan_whole_path(path, x, y, z)[0]

    distance_sq = distance_to_route_sq(path, nearest, x, y, z, closed_loop=True)

    assert math.sqrt(distance_sq) < OFF_ROUTE_DISTANCE_M


def test_a_car_on_its_route_is_never_re_assigned(active_driver, make_own_vehicle, ai_car):
    """One cycle off the route means nothing -- a cut corner, or an MCI frame
    one update behind. Only a car that stays away is re-assigned."""
    own = make_own_vehicle(plid=1, local_plid=1, x=-100.0, y=-100.0)
    car = ai_car(plid=3, x=10.0, y=0.0, heading=270.0, speed=30.0)

    for _ in range(active_driver.OFF_ROUTE_REASSIGN_CYCLES * 2):
        active_driver._process_active(own, {3: car})

    assert active_driver.assigned_routes == {3: 20}
    assert not active_driver._off_route.get(3)


# ─── Performance: the 100 ms budget ──────────────────────────────────────────

def build_traffic_scene(driver, make_own_vehicle, make_vehicle, cars=20, point_count=2000):
    """20 AI cars spread over a 2000-point closed route, as on a full grid."""
    route = circle_route(point_count=point_count)
    path = route['path']
    driver.current_track = 'SO7'
    driver.routes = {20: route}
    driver.state = AIDriver.STATE_ACTIVE

    own = make_own_vehicle(plid=1, local_plid=1, x=0.0, y=0.0)
    vehicles = {}
    step = point_count // cars
    for n in range(cars):
        point = path[n * step]
        vehicle = make_vehicle(plid=n + 2, x=point[0], y=point[1], z=point[2],
                               speed=60.0, pname=f"AI {n}".encode())
        vehicle.data.is_ai = True
        vehicles[n + 2] = vehicle
    return own, vehicles


def test_a_full_grid_needs_no_full_route_scan_per_cycle(driver, make_own_vehicle,
                                                        make_vehicle, monkeypatch):
    """The deterministic half of the budget guard: count the scans, not the clock.

    Route assignment scans every route once per car when it is adopted. After
    that, no cycle may scan a whole path again -- that was the O(cars x route
    length) cost of ``known-issues.md`` #9.
    """
    own, vehicles = build_traffic_scene(driver, make_own_vehicle, make_vehicle)
    driver._process_active(own, vehicles)          # adoption cycle

    import assistance.AI_Driver as module
    scans = []
    real_scan = module._scan_whole_path
    monkeypatch.setattr(module, '_scan_whole_path',
                        lambda *args: (scans.append(1), real_scan(*args))[1])

    for _ in range(10):
        driver._process_active(own, vehicles)

    assert scans == [], f"{len(scans)} full route scans in 10 steady-state cycles"


def test_the_hot_path_is_much_cheaper_than_a_full_scan_per_car(driver, make_own_vehicle,
                                                              make_vehicle):
    """Relative regression guard -- CI timing is noise, ratios are not.

    The baseline is the behaviour before WP10: the nearest-point search scans
    the whole 2000-point path for every car and the 120 m long-straight
    analysis is recomputed for every car, every cycle. Both are emulated by
    dropping the two caches before each measured cycle.
    """
    own, vehicles = build_traffic_scene(driver, make_own_vehicle, make_vehicle)
    driver._process_active(own, vehicles)

    cycles = 10

    def measure(clear_caches):
        started = time.perf_counter()
        for _ in range(cycles):
            if clear_caches:
                driver._route_index.clear()
                driver._straight_cache.clear()
            driver._process_active(own, vehicles)
        return (time.perf_counter() - started) / cycles

    measure(False)                       # warm up
    windowed = measure(False)
    full_scan = measure(True)

    assert windowed < full_scan / 3.0, (
        f"windowed {windowed * 1000:.2f} ms vs full scan {full_scan * 1000:.2f} ms")
    # Absolute ceiling, deliberately generous: the budget is 100 ms per cycle
    # on a mid-range laptop and this container is neither. It only catches an
    # order-of-magnitude regression.
    assert windowed < 0.030, f"{windowed * 1000:.2f} ms per cycle with 20 cars"


# ─── The start confirmation in the menu ──────────────────────────────────────

@pytest.fixture
def menu(bus, message_sender, settings) -> MenuSystem:
    return MenuSystem(UIManager(bus, message_sender, settings), settings)


def test_the_first_click_only_asks(menu, recorder):
    """/axload + /restart throws the driver's layout away -- ask first."""
    seen = recorder('ai_traffic_start', 'notification')
    menu.open_ai_traffic_menu()

    menu._handle_menu_click(22)

    assert seen.count('ai_traffic_start') == 0
    assert menu.ai_traffic_confirm_pending is True
    assert menu.current_menu == 'ai_traffic'
    assert seen.count('notification') == 1


def test_the_second_click_starts(menu, recorder):
    seen = recorder('ai_traffic_start')
    menu.open_ai_traffic_menu()

    menu._handle_menu_click(22)
    menu._handle_menu_click(22)

    assert seen.count('ai_traffic_start') == 1
    assert menu.ai_traffic_confirm_pending is False


def test_the_prompt_is_visible_on_the_button(menu):
    menu.open_ai_traffic_menu()
    before = dict((b[0], b[5]) for b in menu.buttons_for('ai_traffic'))

    menu._handle_menu_click(22)
    after = dict((b[0], b[5]) for b in menu.buttons_for('ai_traffic'))

    assert before[22] != after[22]
    assert set(before) == set(after), "the confirmation must not add or drop buttons"


def test_leaving_the_menu_cancels_the_confirmation(menu, recorder):
    seen = recorder('ai_traffic_start')
    menu.open_ai_traffic_menu()
    menu._handle_menu_click(22)

    menu._handle_menu_click(BTN_CLOSE)      # back to the main menu
    menu.open_ai_traffic_menu()
    menu._handle_menu_click(22)             # first click again: asks, does not start

    assert seen.count('ai_traffic_start') == 0
    assert menu.ai_traffic_confirm_pending is True


def test_a_repaint_cancels_the_confirmation(menu, recorder):
    """SHIFT+B redraws the menu; a pending confirmation must not survive it."""
    seen = recorder('ai_traffic_start')
    menu.on_track = True
    menu.open_ai_traffic_menu()
    menu._handle_menu_click(22)

    menu._on_buttons_cleared()
    menu._handle_menu_click(22)

    assert seen.count('ai_traffic_start') == 0


def test_stopping_traffic_needs_no_confirmation(menu, recorder):
    seen = recorder('ai_traffic_stop')
    menu.open_ai_traffic_menu()
    menu._on_ai_traffic_state_changed({'active': True})

    menu._handle_menu_click(22)

    assert seen.count('ai_traffic_stop') == 1
