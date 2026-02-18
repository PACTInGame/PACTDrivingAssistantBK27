from typing import Dict, Any
from assistance import park_distance_control
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points, point_in_rectangle
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class ForwardCollisionWarning(AssistanceSystem):
    """Kollisionswarnung für Fahrzeuge voraus"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("forward_collision_warning", event_bus, settings)
        self.current_warning_level = 0
        self.own_rectangle = None

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Kollisionsgefahr voraus"""
        warning_level = 0
        reversing = (own_vehicle.data.heading - own_vehicle.data.direction) > 10000 or (own_vehicle.data.heading - own_vehicle.data.direction) < -10000
        if not self.is_enabled() or own_vehicle.data.speed < 10 or reversing:
            if warning_level != self.current_warning_level:
                self.event_bus.emit('needed_deceleration_update', {
                    'deceleration': 0,
                })
                self.current_warning_level = warning_level
                self.event_bus.emit('collision_warning_changed', {
                    'level': warning_level,
                })
            return {'level': 0}


        angle_of_car = abs((own_vehicle.data.heading + 16384) / 182.05)
        ang1, ang2, ang3, ang4 = angle_of_car + 1, angle_of_car - 20, angle_of_car + 20, angle_of_car - 1
        (x1, y1) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 85 * 65536, ang1)
        (x2, y2) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang2)
        (x3, y3) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang3)
        (x4, y4) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 85 * 65536, ang4)
        self.own_rectangle = [(x1, y1), (x2, y2), (x3, y3), (x4, y4)]
        max_needed_deceleration = 0
        cw_dist = self.settings.get("collision_warning_distance")
        factors = [7.5, 3.0, 2.0] if cw_dist == 0 else [7.5, 5.0, 2.5] if cw_dist == 1 else [7.5, 6.5, 5.5]


        for vehicle in vehicles.values():
            if self._is_vehicle_ahead(vehicle):
                needed_braking = self._calculate_needed_braking(own_vehicle, vehicle)  # Nötiges Bremsen in m/s^2
                max_needed_deceleration = max(max_needed_deceleration, needed_braking)
                if needed_braking != float('inf'):
                    if needed_braking > factors[0] or (needed_braking > 0 and self.current_warning_level > 2):
                        warn = 3
                    elif needed_braking > factors[1] or (needed_braking > 0 and self.current_warning_level > 1):
                        warn = 2
                    elif -needed_braking < own_vehicle.data.acceleration and needed_braking > factors[2]:
                        warn = 1
                    else:
                        warn = 0
                    if warn > warning_level:
                        warning_level = warn

        if warning_level > 2:
            self.event_bus.emit('needed_deceleration_update', {
                'deceleration': max_needed_deceleration,
            })
        else:
            self.event_bus.emit('needed_deceleration_update', {
                'deceleration': 0,
            })
        if warning_level != self.current_warning_level:
            self.current_warning_level = warning_level
            self.event_bus.emit('collision_warning_changed', {
                'level': warning_level,
            })

        return {
            'level': warning_level,
        }

    def _is_vehicle_ahead(self, other_vehicle: Vehicle) -> bool:
        """Prüft ob Fahrzeug vor uns ist"""

        is_vehicle_ahead = point_in_rectangle(other_vehicle.data.x, other_vehicle.data.y, self.own_rectangle)

        return is_vehicle_ahead

    def _calculate_needed_braking(self, own_vehicle: OwnVehicle, other_vehicle: Vehicle) -> float:
        """
        Calculates the EXACT needed acceleration (negative for braking)
        to avoid collision.
        """

        # --- 1. SETUP & CONVERSION ---
        v_own = own_vehicle.data.speed * 0.277778  # km/h to m/s
        v_other = other_vehicle.data.speed * 0.277778  # km/h to m/s
        # Ensure a_other is treated as signed (negative for braking)
        relative_speed = v_own - v_other
        a_other = other_vehicle.data.acceleration

        # --- 2. GEOMETRY & DISTANCE ---
        own_vehicle_size = park_distance_control.get_vehicle_size(own_vehicle.data.cname)
        other_vehicle_size = park_distance_control.get_vehicle_size(other_vehicle.data.cname)

        # Average length is used to find center-to-center offset,
        # assuming data.distance_to_player is center-to-center.
        length_of_both_vehicles = (own_vehicle_size[0] + other_vehicle_size[0]) / 2

        SAFETY_BUFFER = 0.5  # meters
        d = other_vehicle.data.distance_to_player - length_of_both_vehicles - SAFETY_BUFFER
        if relative_speed > 0:
            d = d - relative_speed * 0.2 # Reaction time buffer (0.2s)
        self.event_bus.emit('dist_debug', {
            'distance': d,
        })
        # --- 3. PANIC & TRIVIAL CHECKS ---

        # If we have already hit the buffer (or the car), brake maximally immediately
        if d <= 0.01:
            return 20  # Max panic braking (negative)

        # If we are slower than them and they are not braking (or accelerating away),
        # we don't need to do anything.
        if v_own <= v_other and a_other >= 0:
            return 0.0

        # --- 4. CALCULATE TIME HORIZONS ---

        # Time until the lead car comes to a complete stop
        # If a_other is 0 (constant speed) or > 0 (accelerating), it never stops.
        if a_other >= -0.001:
            t_stop = float('inf')
        else:
            t_stop = -v_other / a_other

        # Time until we would crash/match speed if we used dynamic braking logic
        # If v_own <= v_other here, we are slower but they are braking.
        # The time to match is theoretically infinite/undefined in this specific math
        # context until they slow down below our speed, so we treat it as 'never catch dynamically'
        if v_own <= v_other:
            t_match = float('inf')
        else:
            t_match = (2 * d) / (v_own - v_other)

        # --- 5. THE LOGIC SWITCH ---
        if t_match < t_stop:
            # === DYNAMIC CASE ===
            # We will catch them while they are still moving.
            # We need to match their acceleration plus a term to close the gap.
            # Formula: a_req = a_lead - (delta_v^2 / 2d)
            req_accel = a_other - ((v_own - v_other) ** 2 / (2 * d))

        else:
            # === STATIC CASE ===
            # They will stop before we catch them.
            # Treat them as a stationary wall located at their stopping point.

            # 1. Calculate distance lead car travels before stopping
            d_lead_stop = -(v_other ** 2) / (2 * a_other)

            # 2. Total distance we have available to stop
            d_total = d + d_lead_stop

            # 3. Calculate braking to stop in that distance
            # Formula: v^2 = 2*a*d  ->  a = -v^2 / 2d
            req_accel = -(v_own ** 2) / (2 * d_total)

        return abs(req_accel)

# TODO no automatic braking for now