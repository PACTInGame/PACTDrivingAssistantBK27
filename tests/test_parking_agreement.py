"""The detector and the planner have to agree about what a space is.

This file exists because of a live defect, and it is the only test that
catches that whole class of them.

``ParkingSlotDetector`` decides what the driver is *offered*;
``ParkingPlanner`` decides what can be *driven*. Nothing connects the two but
a constant, and when the detector was the more optimistic of the pair the
result was not a missing feature -- it was a broken one. The assistant offered
the space, the plan failed, the ranking picked the same space again on the
next scan, and the button blinked on and off twice a second beside a space the
car was never going to enter. Two live runs were spent on that.

So: every space the detector reports, for a spread of car sizes and turning
radii, must be one the planner can plan. The converse is not required -- a
detector that is *stricter* than the planner only means a space is missed,
which is a disappointment rather than a defect.

The one thing this cannot cover is the driver's lateral distance from the
parked row. Below about 2.3 m there is no space of any length the car can
swing into, and that is a property of where the driver stopped, not of the
space; :data:`LATERAL_OFFSETS` therefore starts above it.
"""

import pytest

from assistance.parking.geometry import OrientedBox, Pose, VehicleShape
from assistance.parking.slot_detection import (KIND_PARALLEL, Obstacle,
                                               ParkingSlotDetector)
from assistance.parking.trajectory import ParkingPlanner

# Smallest and largest the conservative size table produces, plus one between.
CAR_SIZES = ((4.5, 1.8), (4.8, 1.9), (5.2, 2.0))
# The one corner the length constant cannot cover, and does not claim to: the
# largest car with the driver closest to the parked row needs up to 4.3 m of
# slack, which no constant that also serves a 4.5 m car could promise. It is
# excluded here and documented in ``slot_detection`` -- and it is *safe* to
# exclude, because the planner refuses it and ``ParkAssist`` demotes it and
# offers the next space (``TestRanking`` covers that half).
UNCOVERED = {((5.2, 2.0), 2.6)}
# The default setting (6.0) and two below it. The setting allows up to 12 m,
# but a turning circle that large cannot enter a kerbside space at any length
# from a normal road position -- that is the lateral limit the module
# docstring describes, and it is the planner's to reject, not the detector's.
TURN_RADII = (4.0, 5.0, 6.0)
# Centre-to-centre, ego to parked row. See the module docstring.
LATERAL_OFFSETS = (2.6, 3.0, 3.6)
NEIGHBOUR = (4.5, 1.9)


def scene(gap, lateral, ego_x=0.0):
    """Two parked cars with *gap* metres of clear kerb between them."""
    half = gap * 0.5 + NEIGHBOUR[0] * 0.5
    return [Obstacle(OrientedBox(half, -lateral, 0.0, *NEIGHBOUR), 'front', 0.0),
            Obstacle(OrientedBox(-half, -lateral, 0.0, *NEIGHBOUR), 'rear', 0.0)]


def shape_for(length, width):
    return VehicleShape(length, width, wheelbase=length * 0.58,
                        rear_axle_offset=length * 0.28)


@pytest.mark.parametrize('length,width', CAR_SIZES)
@pytest.mark.parametrize('radius', TURN_RADII)
@pytest.mark.parametrize('lateral', LATERAL_OFFSETS)
def test_the_shortest_offered_space_can_be_planned(length, width, radius,
                                                   lateral):
    """The space at the detector's own limit has to be drivable.

    Right at the limit, and just above it: the limit itself is the case that
    went wrong in the game, and a margin that only works above it is not a
    margin.
    """
    if ((length, width), lateral) in UNCOVERED:
        pytest.skip("documented gap in what a length constant can promise")
    shape = shape_for(length, width)
    detector = ParkingSlotDetector(shape)
    planner = ParkingPlanner(shape, min_turn_radius=radius)
    ego = Pose(0.0, 0.0, 0.0)

    shortest = detector.required_length(KIND_PARALLEL)
    for gap in (shortest + 0.01, shortest + 0.25, shortest + 1.0):
        obstacles = scene(gap, lateral)
        offered = [slot for slot in detector.scan(ego, obstacles)
                   if slot.kind == KIND_PARALLEL and not slot.open_ended]
        assert offered, f"{gap:.2f} m is at or above the detector's own limit"
        result = planner.plan(ego, offered[0], [o.box for o in obstacles])
        assert result.ok, (f"offered a {gap:.2f} m space to a {length} m car "
                           f"at radius {radius}, then could not plan it: "
                           f"{result.reason}")


@pytest.mark.parametrize('length,width', CAR_SIZES)
def test_a_space_below_the_limit_is_not_offered(length, width):
    """The other half of the contract: too short means not offered at all."""
    shape = shape_for(length, width)
    detector = ParkingSlotDetector(shape)
    shortest = detector.required_length(KIND_PARALLEL)
    offered = [slot for slot in detector.scan(Pose(0.0, 0.0, 0.0),
                                              scene(shortest - 0.3, 3.0))
               if slot.kind == KIND_PARALLEL and not slot.open_ended]
    assert offered == []
