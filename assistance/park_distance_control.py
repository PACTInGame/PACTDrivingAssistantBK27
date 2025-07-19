import math
import time
from typing import Dict, Any

import pyinsim.func
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import calc_polygon_points
from misc.spacial_hash_grid import SpatialHashGrid
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

def create_bboxes_for_own_vehicle(own_vehicle: OwnVehicle):
    # TODO make this dynamic for different car sizes and with more rectangles
    vehicle_size = (1.8, 4.5)  # Breite, Länge in Metern
    angle_of_car = (own_vehicle.data.heading + 16384) / 182.05

    perpendicular_angle = angle_of_car + 90

    (left_side_of_car_x, left_side_of_car_y) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, vehicle_size[0] / 2 * 65536, perpendicular_angle)

    (front_left_x, front_left_y) = calc_polygon_points(left_side_of_car_x, left_side_of_car_y, vehicle_size[1] / 2 * 65536, angle_of_car)
    (front_middle_x, front_middle_y) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, vehicle_size[1] / 2 * 65536, angle_of_car)
    (front_right_x, front_right_y) = calc_polygon_points(front_left_x, front_left_y, -vehicle_size[0] * 65536, perpendicular_angle)

    (rear_left_x, rear_left_y) = calc_polygon_points(left_side_of_car_x, left_side_of_car_y, -vehicle_size[1] / 2 * 65536, angle_of_car)
    (rear_middle_x, rear_middle_y) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, -vehicle_size[1] / 2 * 65536, angle_of_car)
    (rear_right_x, rear_right_y) = calc_polygon_points(rear_left_x, rear_left_y, -vehicle_size[0] * 65536, perpendicular_angle)

    pdc_sensor_angle = 30
    sensor_distances = [0.6 * 65536, 1.4 * 65536, 2.8 * 65536]

    # Polygone für die sensoren ausgehend von den Punkten erstellen
    outer_sensors = []
    middle_sensors = []
    inner_sensors = []
    for i, point in enumerate([(front_left_x, front_left_y), (front_middle_x, front_middle_y), (front_right_x, front_right_y),
                    (rear_left_x, rear_left_y), (rear_middle_x, rear_middle_y), (rear_right_x, rear_right_y)]):
            (x, y) = point
            sensor_angle_1 = (angle_of_car + pdc_sensor_angle) % 360
            sensor_angle_2 = (angle_of_car - pdc_sensor_angle) % 360

            for j, distance in enumerate(sensor_distances):
                if i > 2:
                    distance= -distance  # Negative Werte für die hinteren Sensoren
                (sensor_1_x, sensor_1_y) = calc_polygon_points(x, y, distance, sensor_angle_1)
                (sensor_2_x, sensor_2_y) = calc_polygon_points(x, y, distance, sensor_angle_2)
                if j == 0:
                    inner_sensors.append([(x, y), (sensor_1_x, sensor_1_y), (sensor_2_x, sensor_2_y)])
                elif j == 1:
                    middle_sensors.append([(x, y), (sensor_1_x, sensor_1_y), (sensor_2_x, sensor_2_y)])
                elif j == 2:
                    outer_sensors.append([(x, y), (sensor_1_x, sensor_1_y), (sensor_2_x, sensor_2_y)])




    # angle_of_car = abs((own_vehicle.data.heading + 16384) / 182.05)
    # ang1, ang2, ang3, ang4 = angle_of_car - 160, angle_of_car - 20, angle_of_car + 20, angle_of_car + 160
    # (x1, y1) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang1)
    # (x2, y2) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang2)
    # (x3, y3) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang3)
    # (x4, y4) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, 3.0 * 65536, ang4)
    # own_rectangle_0 = [(x1, y1), (x2, y2), (x3, y3), (x4, y4)]


    return outer_sensors, middle_sensors, inner_sensors

def create_rectangle_for_object(x: float, y: float, index: int, heading: float) -> list:
    x = x * 4096
    y = y * 4096
    height, width = get_object_size(index)
    print("angle_of_obj", heading)
    angle_of_obj = (heading * 360 / 256 + 90) % 360
    print("angle_of_obj", angle_of_obj)

    ang_perp = angle_of_obj + 90
    print(x, y, width / 2, height / 2)
    (x1, y1) = calc_polygon_points(x, y, width / 2 * 65536, ang_perp)
    print("x1, y1", x1 / 65536, y1 / 65536)
    (x2, y2) = calc_polygon_points(x, y, -width / 2 * 65536, ang_perp)
    print("x2, y2", x2 / 65536, y2 / 65536)
    (corner_x, corner_y) = calc_polygon_points(x1, y1, height / 2 * 65536, angle_of_obj)
    (corner2_x, corner2_y) = calc_polygon_points(x2, y2, height / 2 * 65536, angle_of_obj)
    (corner3_x, corner3_y) = calc_polygon_points(x2, y2, -height / 2 * 65536, angle_of_obj)
    (corner4_x, corner4_y) = calc_polygon_points(x1, y1, -height / 2 * 65536, angle_of_obj)

    rectangle_for_object = [(corner_x, corner_y), (corner2_x, corner2_y), (corner3_x, corner3_y),
                            (corner4_x, corner4_y)]
    return rectangle_for_object


def save_rectangles_as_json(rectangles: list, filename: str):
    """Speichert die Rechtecke als JSON-Datei"""
    import json
    with open(filename, 'w') as f:
        json.dump(rectangles, f, indent=4)


def load_rectangles_from_json(filename: str) -> list:
    """Lädt Rechtecke aus einer JSON-Datei"""
    import json
    with open(filename, 'r') as f:
        rectangles = json.load(f)
    return rectangles


class ParkDistanceControl(AssistanceSystem):
    """Toter-Winkel-Warner"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("ParkDistanceControl", event_bus, settings)
        self.detection_distance = 70.0
        self.pdc_result = {
            0: 0,
            1: 0,
            2: 0,
            3: 0,
            4: 0,
            5: 0
        }
        self.last_exec = time.perf_counter()
        self.axm = None
        self.park_grid = SpatialHashGrid(cell_size=15.0 * 65536)
        self.event_bus.subscribe('layout_received', self._update_axm)

    def _update_axm(self, axm):
        """Aktualisiert die AXM-Daten"""
        self.axm = axm
        self.park_grid.clear()
        track_objects = load_rectangles_from_json(filename='park_distance_control_rectangles_ax.json')
        for objects in axm.Info:
            # TODO get all current new objects
            pass
        # TODO create other car boundaries.

        for i, rect in enumerate(track_objects):
            self.park_grid.insert_object(i, [rect[0], rect[1], rect[2], rect[3]], is_static=True)


    def _update_axm_track_boundaries(self, axm):
        """Aktualisiert die AXM-Daten"""
        rects = []
        for object in axm.Info:
            if object.Index == 98:
                rects.append(create_rectangle_for_object(object.X, object.Y, object.Index, object.Heading))

        save_rectangles_as_json(rects, 'park_distance_control_rectangles_ax.json')

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[int, int]:
        """Prüft auf Fahrzeuge im toten Winkel"""
        new_pdc_result = {
            0: 0,
            1: 0,
            2: 0,
            3: 0,
            4: 0,
            5: 0
        }
        if own_vehicle.data.speed < 10:
            outer_sensors, middle_sensors, inner_sensors = create_bboxes_for_own_vehicle(own_vehicle)
            nearby = self.park_grid.query_area(own_vehicle.data.x, own_vehicle.data.y, 30 * 65536)
            collisions = []
            for obj in nearby:
                for i, sensor in enumerate(outer_sensors):
                    if self.park_grid.polygon_overlap(sensor, obj['points']):
                        new_pdc_result[i] = (max(new_pdc_result[i], 1))
                        collisions.append(obj)

            # Filter secondary collisions for efficiency
            secondary_collisions = []
            for obj in collisions:
                for i, sensor in enumerate(middle_sensors):
                    if self.park_grid.polygon_overlap(sensor, obj['points']):
                        new_pdc_result[i] = (max(new_pdc_result[i], 2))
                        secondary_collisions.append(obj)

            for obj in secondary_collisions:
                for i, sensor in enumerate(inner_sensors):
                    if self.park_grid.polygon_overlap(sensor, obj['points']):
                        new_pdc_result[i] = (max(new_pdc_result[i], 3))
            print("New PDC Result:", new_pdc_result)
        if self.pdc_result != new_pdc_result:
            self.event_bus.emit('pdc_changed', new_pdc_result)
            self.pdc_result = new_pdc_result

        return self.pdc_result
