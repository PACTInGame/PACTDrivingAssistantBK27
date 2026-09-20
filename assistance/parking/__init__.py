"""Self-parking: slot detection, trajectory planning and path following.

Three layers, deliberately separated so that none of them knows about LFS:

``geometry``       poses, oriented boxes, the ego footprint -- metres and
                   mathematical radians, nothing else.
``slot_detection`` turns a set of obstacles into ``ParkingSlot`` candidates.
``trajectory``     turns a slot into a drivable ``Trajectory``.
``path_follower``  turns a trajectory plus the current pose into a
                   ``ControlDemand`` (target speed, curvature, direction).

Only ``assistance/park_assist.py`` above them talks to the event bus, and only
``Controls/vehicle_control.py`` below them touches an input device. A future
feature that wants "drive this path" can reuse everything from ``trajectory``
down without inheriting a single parking-specific assumption.

Units in this package, without exception (``reference/conventions.md`` §1):

* positions in **metres**, never in the 1/65536 m of an MCI packet;
* angles in **radians**, mathematical frame -- 0 = +X, growing anticlockwise,
  the frame ``(heading + 16384) / 182.05`` produces;
* speeds in **m/s**, not km/h, because every equation here is kinematic;
* curvature in **1/m**, positive = left turn, which is the sign yaw rate has.
"""
