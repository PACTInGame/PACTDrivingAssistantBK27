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

def get_vehicle_size(cname) -> tuple:
    # TODO qmight be wrong way for pdc
    car_sizes = {
        b'UF1': (2.95, 1.5),
        b'XFG': (3.7, 1.7),
        b'XRG': (4.5, 1.8),
        b'LX4': (3.6, 1.7),
        b'LX6': (3.6, 1.7),
        b'RB4': (4.5, 1.9),
        b'FXO': (4.5, 1.9),
        b'XRT': (4.5, 1.9),
        b'RAC': (4.1, 1.8),
        b'FZ5': (4.6, 2),
        b'UFR': (3.2, 1.6),
        b'XFR': (3.9, 1.9),
        b'FXR': (5.0, 2.1),
        b'XRR': (5.0, 2.1),
        b'FZR': (5.0, 2.1),
    }
    return car_sizes.get(cname, (4.5, 1.8))  # Standardgröße falls Index nicht gefunden wird
def get_object_size(index: int) -> tuple:
    """Gibt die Größe des Objekts basierend auf dem Index zurück"""
    # Hier sollten die tatsächlichen Größen der Objekte definiert werden
    # TODO andere Indizes und Größen hinzufügen
    print("Index:", index)
    object_sizes = {
        48: (0.5, 0.5),
        49: (0.5, 0.5),
        50: (0.5, 0.5),
        51: (0.5, 0.5),
        52: (0.75, 0.75),
        53: (0.75, 0.75),
        54: (0.75, 0.75),
        55: (0.75, 0.75),
        64: (0.3, 1.4),
        65: (0.3, 1.4),
        66: (0.3, 1.4),
        67: (0.3, 1.4),
        68: (0.3, 1.4),
        69: (0.3, 1.4),
        70: (0.3, 1.4),
        71: (0.3, 1.4),
        72: (0.3, 1.4),
        73: (0.3, 1.4),
        74: (0.3, 1.4),
        75: (0.3, 1.4),
        76: (0.3, 1.4),
        77: (0.3, 1.4),
        78: (0.3, 1.4),
        79: (0.3, 1.4),
        80: (0.3, 1.4),
        81: (0.3, 1.4),
        82: (0.3, 1.4),
        83: (0.3, 1.4),
        84: (0.3, 1.4),
        85: (0.3, 1.4),
        86: (0.3, 1.4),
        87: (0.3, 1.4),
        88: (0.3, 1.4),
        89: (0.3, 1.4),
        90: (0.3, 1.4),
        91: (0.3, 1.4),
        96: (3.8, 0.3),
        97: (10.1, 0.3),
        98: (16.6, 0.3),
        104: (8.3, 0.3),
        105: (1.3, 0.3),
        106: (1.3, 0.3),
        112: (1.0, 6.0),
        113: (1.0, 6.0),
        136: (0.2, 0.2),
        137: (0.2, 0.2),
        138: (0.2, 0.2),
        139: (0.2, 0.2),
        144: (0.75, 1.75),
        148: (0.2, 2.5),
        160: (0.7, 0.7),
        161: (0.7, 0.7),
        168: (1.3, 1.3),
        169: (1.3, 1.3),


        # Weitere Indizes und Größen können hier hinzugefügt werden
    }
    return object_sizes.get(index, (0.5, 0.5))  # Standardgröße falls Index nicht gefunden wird

def create_bboxes_for_own_vehicle(own_vehicle: OwnVehicle):
    vehicle_size_def = get_vehicle_size(own_vehicle.data.cname)
    print("Vehicle size for PDC:", own_vehicle.data.cname)
    vehicle_size = (vehicle_size_def[1], vehicle_size_def[0])  # switch
    angle_of_car = (own_vehicle.data.heading + 16384) / 182.05

    perpendicular_angle = angle_of_car + 90

    (left_side_of_car_x, left_side_of_car_y) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, vehicle_size[0] / 2 * 65536, perpendicular_angle)

    (front_left_x, front_left_y) = calc_polygon_points(left_side_of_car_x, left_side_of_car_y, vehicle_size[1] / 2 * 65536, angle_of_car)
    (front_middle_x, front_middle_y) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, vehicle_size[1] / 2 * 65536, angle_of_car)
    (front_right_x, front_right_y) = calc_polygon_points(front_left_x, front_left_y, -vehicle_size[0] * 65536, perpendicular_angle)

    (rear_left_x, rear_left_y) = calc_polygon_points(left_side_of_car_x, left_side_of_car_y, -vehicle_size[1] / 2 * 65536, angle_of_car)
    (rear_middle_x, rear_middle_y) = calc_polygon_points(own_vehicle.data.x, own_vehicle.data.y, -vehicle_size[1] / 2 * 65536, angle_of_car)
    (rear_right_x, rear_right_y) = calc_polygon_points(rear_left_x, rear_left_y, -vehicle_size[0] * 65536, perpendicular_angle)

    pdc_sensor_angle = 25
    sensor_distances = [0.1 * 65536, 1.4 * 65536, 2.8 * 65536]

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
    x = x * 4096 # TODO check if correct for 65536 scale
    y = y * 4096
    height, width = get_object_size(index)
    angle_of_obj = (heading * 360 / 256 + 90) % 360

    ang_perp = angle_of_obj + 90
    (x1, y1) = calc_polygon_points(x, y, width / 2 * 65536, ang_perp)
    (x2, y2) = calc_polygon_points(x, y, -width / 2 * 65536, ang_perp)
    (corner_x, corner_y) = calc_polygon_points(x1, y1, height / 2 * 65536, angle_of_obj)
    (corner2_x, corner2_y) = calc_polygon_points(x2, y2, height / 2 * 65536, angle_of_obj)
    (corner3_x, corner3_y) = calc_polygon_points(x2, y2, -height / 2 * 65536, angle_of_obj)
    (corner4_x, corner4_y) = calc_polygon_points(x1, y1, -height / 2 * 65536, angle_of_obj)

    rectangle_for_object = [(corner_x, corner_y), (corner2_x, corner2_y), (corner3_x, corner3_y),
                            (corner4_x, corner4_y)]
    return rectangle_for_object

def create_rectangle_for_vehicle(x: float, y: float, type: str, heading: float) -> list:
    height, width = get_vehicle_size(type)
    # cars use a different heading system than objects, so we need to convert it
    angle_of_obj = (heading * 360 / 65536 + 90) % 360

    ang_perp = angle_of_obj + 90
    (x1, y1) = calc_polygon_points(x, y, width / 2 * 65536, ang_perp)
    (x2, y2) = calc_polygon_points(x, y, -width / 2 * 65536, ang_perp)
    (corner_x, corner_y) = calc_polygon_points(x1, y1, height / 2 * 65536, angle_of_obj)
    (corner2_x, corner2_y) = calc_polygon_points(x2, y2, height / 2 * 65536, angle_of_obj)
    (corner3_x, corner3_y) = calc_polygon_points(x2, y2, -height / 2 * 65536, angle_of_obj)
    (corner4_x, corner4_y) = calc_polygon_points(x1, y1, -height / 2 * 65536, angle_of_obj)

    rectangle_for_object = [(corner_x, corner_y), (corner2_x, corner2_y), (corner3_x, corner3_y),
                            (corner4_x, corner4_y)]
    return rectangle_for_object



def save_rectangles_as_json(rectangles: list, filename: str):
    """Speichert die Rechtecke als JSON-Datei oder fügt sie zu einer bestehenden Datei hinzu"""
    import json
    import os

    # Check if file exists and is not empty
    if os.path.exists(filename) and os.path.getsize(filename) > 0:
        # Read existing data
        try:
            with open(filename, 'r') as f:
                existing_data = json.load(f)

            # Append new rectangles to existing data
            existing_data.extend(rectangles)

            # Write back the combined data
            with open(filename, 'w') as f:
                json.dump(existing_data, f, indent=4)

        except (json.JSONDecodeError, ValueError) as e:
            print(f"Error reading existing JSON file: {e}")
            print("Creating new file with current data...")
            # If there's an error reading the file, create a new one
            with open(filename, 'w') as f:
                json.dump(rectangles, f, indent=4)
    else:
        # File doesn't exist or is empty, create new file
        with open(filename, 'w') as f:
            json.dump(rectangles, f, indent=4)


class ParkDistanceControl(AssistanceSystem):
    """PDC"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("park_distance_control", event_bus, settings)
        self.detection_distance = 70.0
        self.pdc_result = {
            0: -1,
            1: -1,
            2: -1,
            3: -1,
            4: -1,
            5: -1
        }
        self.last_exec = time.perf_counter()
        self.park_grid = SpatialHashGrid(cell_size=15.0 * 65536)
        self.event_bus.subscribe('layout_received', self._update_axm)
        self.last_axm_update = time.perf_counter()
        self.track = "ax"
        self.object_id = 10000

    def load_rectangles_from_json(self, filename: str) -> list:
        """Lädt Rechtecke aus einer JSON-Datei"""
        import json
        with open(filename, 'r') as f:
            rectangles = json.load(f)
        for i, rect in enumerate(rectangles):
            self.park_grid.insert_object(i, [rect[0], rect[1], rect[2], rect[3]], is_static=True)

    def _update_axm(self, axm):
        """Aktualisiert die AXM-Daten"""
        current_time = time.perf_counter()
        print(current_time - self.last_axm_update)
        if current_time - self.last_axm_update > 5:
            self.park_grid.clear()
            self.object_id = 10000
        self._update_axm_track_boundaries_and_save(axm)
        #self._update_axm_track_boundaries(axm)
        self.load_rectangles_from_json(filename='park_distance_control_rectangles_ax.json')
        #self.park_grid.plot_grid()
        print("statistics: ", self.park_grid.get_statistics())
        self.last_axm_update = current_time


    def _update_axm_track_boundaries_and_save(self, axm):
        """Aktualisiert die AXM-Daten"""
        rects = []
        for object in axm.Info:
            if object.Index == 98 or object.Index == 97 or object.Index == 96: # Armco 1-5
                rects.append(create_rectangle_for_object(object.X, object.Y, object.Index, object.Heading))
            elif object.Index == 136: # Post Green
                rects.append(create_rectangle_for_object(object.X, object.Y, object.Index, object.Heading))
        print(len(rects))
        save_rectangles_as_json(rects, 'park_distance_control_rectangles_ax.json')

    def _update_axm_track_boundaries(self, axm):
        """Aktualisiert die AXM-Daten"""
        rects = []
        for object in axm.Info:
                rects.append(create_rectangle_for_object(object.X, object.Y, object.Index, object.Heading))

        for i, rect in enumerate(rects):
            self.park_grid.insert_object(self.object_id, [rect[0], rect[1], rect[2], rect[3]], is_static=True)
            self.object_id += 1

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[int, int]:
        """Prüft auf Fahrzeuge im toten Winkel"""
        new_pdc_result = {
            0: -1,
            1: -1,
            2: -1,
            3: -1,
            4: -1,
            5: -1
        }
        if own_vehicle.data.speed < 10:
            new_pdc_result = {
                0: 0,
                1: 0,
                2: 0,
                3: 0,
                4: 0,
                5: 0
            }
            self.park_grid.clear_dynamic_objects()
            vehicle_copy = vehicles.copy() # to avoid runtime error for changing dict size during iteration
            for vehicle in vehicle_copy.values():
                if vehicle.data.distance_to_player > 15:
                    self.park_grid.remove_object(vehicle.data.player_id)
                    continue
                rectangle = create_rectangle_for_vehicle(vehicle.data.x, vehicle.data.y,
                                                        vehicle.data.cname, vehicle.data.heading)
                self.park_grid.insert_object(vehicle.data.player_id, [rectangle[0], rectangle[1], rectangle[2], rectangle[3]], is_static=False)
                #self.park_grid.plot_grid()

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
            #print("New PDC Result:", new_pdc_result)
        if self.pdc_result != new_pdc_result:
            self.event_bus.emit('pdc_changed', new_pdc_result)
            self.pdc_result = new_pdc_result

        return self.pdc_result
