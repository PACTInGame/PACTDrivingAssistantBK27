import math
import time
from typing import Dict, Any

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle

def get_object_size(index: int) -> tuple:
    """Gibt die Größe des Objekts basierend auf dem Index zurück"""
    # Hier sollten die tatsächlichen Größen der Objekte definiert werden
    # Beispielwerte für Demonstrationszwecke
    object_sizes = {
        98: (16.6, 0.3),  # Beispielgröße für Index 98
        # Weitere Indizes und Größen können hier hinzugefügt werden
    }
    return object_sizes.get(index, (1.0, 0.5))  # Standardgröße falls Index nicht gefunden wird
def create_rectangle_for_object(x: float, y: float, index: int, heading: float) -> list:
    x= x*4096
    y= y*4096
    height, width = get_object_size(index)
    print("angle_of_obj", heading)
    angle_of_obj = (heading * 360 / 256 + 90) % 360
    print("angle_of_obj", angle_of_obj)

    ang_perp = angle_of_obj + 90
    print(x, y, width/2, height/2)
    (x1, y1) = calc_polygon_points(x, y, width/2 * 65536, ang_perp)
    print("x1, y1", x1/65536, y1/65536)
    (x2, y2) = calc_polygon_points(x, y, -width/2 * 65536, ang_perp)
    print("x2, y2", x2/65536, y2/65536)
    (corner_x, corner_y) = calc_polygon_points(x1, y1, height/2 * 65536, angle_of_obj)
    (corner2_x, corner2_y) = calc_polygon_points(x2, y2, height/2 * 65536, angle_of_obj)
    (corner3_x, corner3_y) = calc_polygon_points(x2, y2, -height/2 * 65536, angle_of_obj)
    (corner4_x, corner4_y) = calc_polygon_points(x1, y1, -height/2 * 65536, angle_of_obj)

    rectangle_for_object = [(corner_x, corner_y), (corner2_x, corner2_y), (corner3_x, corner3_y), (corner4_x, corner4_y)]
    return rectangle_for_object

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
        self.axm = None
        self.event_bus.subscribe('layout_received', self._update_axm)
    def _update_axm(self, axm):
        for object in axm.Info:
            if object.Index == 98:
                create_rectangle_for_object(object.X, object.Y, object.Index, object.Heading)



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
            self.own_rectangle = [(x1, y1), (x2, y2), (x3, y3), (x4, y4)]


        else:
            if self.pdc_result != new_pdc_result:
                self.event_bus.emit('pdc_changed', new_pdc_result)
        return new_pdc_result