"""Feed a recorded simulation trace back through the assistance systems.

**Offline tool, not a test.** It needs neither LFS nor Windows -- only a
``trace.jsonl`` from ``simulation_tests/runs/``.

Why it exists: an in-game run costs about seventy seconds, needs the game
running, and drifts a little between repetitions. For *tuning a threshold*
that is the wrong loop. The trace already holds every number the assistance
systems read -- positions, headings, speeds and ``AngVel`` for every car, at
the MCI rate -- so the decision can simply be recomputed. Sweeping a parameter
over seven recorded scenarios takes a couple of seconds and is exactly
repeatable.

It lives in ``tools/`` and not in ``simulation_tests/`` on purpose: that
package deliberately imports nothing from the add-on (``simulation_tests/
README.md``), and this does the opposite -- it imports the systems under test.

What it cannot tell you:

* **Whether the intervention would have changed the outcome.** The trace is a
  fixed history. If the run it came from braked, the braking is baked into the
  positions; if it did not, the car keeps going however loudly the replay
  warns. Use it to ask "does it fire, when, and for what", then confirm the
  outcome in game.
* **Anything about the UI, the audio or the actuation path.** It stops at the
  systems' return values and their events.

The ego's speed here is MCI's, not OutGauge's. Live, ``OwnVehicle`` takes speed
from OutGauge at twice the rate, so a replayed number can differ from the live
one by one MCI step of acceleration. That is small next to what this tool is
for -- whether a threshold is crossed at all, and roughly when.

Run it from the project root::

    python tools/replay_trace.py simulation_tests/runs/22_*
    python tools/replay_trace.py --detail simulation_tests/runs/25_*
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistance.blind_spot_warning import BlindSpotWarning    # noqa: E402
from assistance.collision_warning import ForwardCollisionWarning  # noqa: E402
from assistance.cross_traffic_warning import CrossTrafficWarning  # noqa: E402
from core.event_bus import EventBus                           # noqa: E402
from core.settings_manager import SettingsManager             # noqa: E402
from vehicles.own_vehicle import OwnVehicle                   # noqa: E402
from vehicles.vehicle import Vehicle                          # noqa: E402


class TraceClock:
    """The trace's own clock, so hold times behave as they did live."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def load(run_dir):
    path = os.path.join(run_dir, 'trace.jsonl')
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle]


def identify(trace):
    """(own PLID, {plid: CName}) from the IS_NPL packets.

    The local driver is the one ``PType`` does not mark as AI
    (``reference/conventions.md`` section 5.4).
    """
    ai = set()
    names = {}
    for record in trace:
        if record['ev'] != 'NPL':
            continue
        data = record['d']
        names[data['PLID']] = data.get('CName', 'XFG')
        if 'AI' in str(data.get('ptype_flags', '')):
            ai.add(data['PLID'])
    own = next((plid for plid in names if plid not in ai), None)
    return own, names


def replay(run_dir, detail=False, settings_overrides=None):
    trace = load(run_dir)
    own_plid, names = identify(trace)
    if own_plid is None:
        print(f"{os.path.basename(run_dir)}: no local driver in the trace")
        return

    settings = SettingsManager(settings_file=os.path.join(
        os.environ.get('TEMP', '/tmp'), 'pact_replay_settings.json'))
    settings._settings.update(settings_overrides or {})

    bus = EventBus()
    clock = TraceClock()
    blind_spot = BlindSpotWarning(bus, settings)
    blind_spot.clock = clock
    cross_traffic = CrossTrafficWarning(bus, settings)
    forward = ForwardCollisionWarning(bus, settings)
    # FCW is the only one of the three whose demand does not come back in its
    # result dict -- it publishes it, because ``EmergencyBrake`` is the only
    # consumer. Catch it off the bus rather than reaching into the object.
    demands = {}
    bus.subscribe('needed_deceleration_update',
                  lambda data: demands.__setitem__(
                      (data or {}).get('source') or 'forward_collision',
                      float((data or {}).get('deceleration') or 0.0)))

    own = OwnVehicle()
    own.set_local_driver(own_plid)
    vehicles = {}
    changes = []
    previous = None

    for record in trace:
        if record['ev'] != 'MCI':
            continue
        clock.now = record['t']
        frame = {car['PLID']: car for car in record['d']['cars']}
        if own_plid not in frame:
            continue

        mine = frame[own_plid]
        own.update_position(mine['X'], mine['Y'], mine['Z'], mine['Heading'],
                            mine['Direction'], mine['speed_kmh'],
                            timestamp=record['t'],
                            ang_vel=mine.get('AngVel', 0))
        own.update_model_and_driver(names.get(own_plid, 'XFG'), 'Me', 0)

        others = {}
        for plid, car in frame.items():
            if plid == own_plid:
                continue
            vehicle = vehicles.setdefault(plid, Vehicle(plid))
            vehicle.update_position(car['X'], car['Y'], car['Z'],
                                    car['Heading'], car['Direction'],
                                    car['speed_kmh'], timestamp=record['t'],
                                    ang_vel=car.get('AngVel', 0))
            vehicle.update_model_and_driver(names.get(plid, 'XFG'), 'AI', 0)
            vehicle.update_distance_to_player(own.data.x, own.data.y,
                                              own.data.z)
            vehicle.update_angle_to_player(own.data.x, own.data.y,
                                           own.data.heading)
            others[plid] = vehicle

        demands.clear()
        bsw_result = blind_spot.process(own, others)
        ctw_result = cross_traffic.process(own, others)
        fcw_result = forward.process(own, others)
        state = (max(bsw_result['left_level'], bsw_result['right_level']),
                 ctw_result['level'],
                 round(max(bsw_result['deceleration'],
                           ctw_result['deceleration'],
                           demands.get('forward_collision', 0.0)), 1),
                 fcw_result['level'])
        if state != previous:
            changes.append((record['t'], state, own.data.speed))
            previous = state

    contacts = [r['t'] for r in trace if r['ev'] in ('CON', 'OBH')]
    name = os.path.basename(os.path.normpath(run_dir))
    print(f"{name[:52]:52s} bsw<={max((c[1][0] for c in changes), default=0)} "
          f"ctw<={max((c[1][1] for c in changes), default=0)} "
          f"fcw<={max((c[1][3] for c in changes), default=0)} "
          f"demand<={max((c[1][2] for c in changes), default=0.0):5.1f}  "
          f"contacts={len(contacts)}")
    if not detail:
        return

    timeline = [(t, 'CONTACT', None, None) for t in contacts]
    timeline += [(r['t'], 'marker ' + r['ev'], None, None)
                 for r in trace if r['src'] == 'marker']
    timeline += [(t, None, state, speed) for t, state, speed in changes]
    for t, label, state, speed in sorted(timeline, key=lambda row: row[0]):
        if label is not None:
            print(f"     t={t:6.2f}  {label}")
        elif state == (0, 0, 0.0, 0):
            print(f"     t={t:6.2f}  v={speed:5.1f}  clear")
        else:
            print(f"     t={t:6.2f}  v={speed:5.1f}  bsw={state[0]} "
                  f"ctw={state[1]} fcw={state[3]} demand={state[2]:5.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('runs', nargs='+', help='run directories')
    parser.add_argument('--detail', action='store_true',
                        help='print every change, with markers and contacts')
    parser.add_argument('--setting', action='append', default=[],
                        metavar='KEY=VALUE',
                        help='override a setting, repeatable')
    args = parser.parse_args()

    overrides = {}
    for pair in args.setting:
        key, _, value = pair.partition('=')
        try:
            overrides[key] = json.loads(value)
        except json.JSONDecodeError:
            overrides[key] = value

    for run_dir in args.runs:
        replay(run_dir, detail=args.detail, settings_overrides=overrides)


if __name__ == '__main__':
    main()
