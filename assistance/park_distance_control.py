import math
import time
from typing import Dict, Any

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class ParkDistanceControl(AssistanceSystem):
    """Toter-Winkel-Warner"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("ParkDistanceControl", event_bus, settings)
        self.detection_distance = 70.0
        self.pdc_result = {
            'fl': False,
            'fm': False,
            'fr': False,
            'rl': False,
            'rm': False,
            'rr': False
        }
        self.last_exec = time.perf_counter()

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Prüft auf Fahrzeuge im toten Winkel"""
        new_pdc_result = {
            'fl': False,
            'fm': False,
            'fr': False,
            'rl': False,
            'rm': False,
            'rr': False
        }
        if own_vehicle.data.speed < 10:
            angle_of_car = abs((own_vehicle.data.heading + 16384) / 182.05)
            ang1, ang2, ang3, ang4 = angle_of_car - 160, angle_of_car - 20, angle_of_car + 20, angle_of_car + 160
            (x1, y1) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang1)
            (x2, y2) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang2)
            (x3, y3) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang3)
            (x4, y4) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang4)
            self.own_rectangle = [(x1/65536, y1/65536), (x2/65536, y2/65536), (x3/65536, y3/65536), (x4/65536, y4/65536)]


        else:
            if self.pdc_result != new_pdc_result:
                self.event_bus.emit('pdc_changed', new_pdc_result)
        return new_pdc_result