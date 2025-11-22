import json
import math
import os
import heapq
from typing import Dict, Any, List, Tuple, Optional

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


# --- Helper Classes for Math and Graph Logic ---

class Vector3:
    """Simple helper for 3D math operations"""

    @staticmethod
    def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    @staticmethod
    def distance_sq(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2

    @staticmethod
    def distance(a, b):
        return math.sqrt(Vector3.distance_sq(a, b))

    @staticmethod
    def normalize(v):
        m = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        if m == 0: return (0, 0, 0)
        return (v[0] / m, v[1] / m, v[2] / m)

    @staticmethod
    def dot(a, b):
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]

    @staticmethod
    def cross_y(a, b):
        """Returns the Y component of the cross product (useful for 2D navigation on X,Z plane)"""
        return a[0] * b[1] - a[1] * b[0]


class NavigationGraph:
    def __init__(self):
        # junction_index -> {neighbor_junction_index: {road_id: int, length: float}}
        self.adjacency = {}
        self.junction_locations = {}  # index -> (x,y,z)
        self.road_map = {}  # road_id -> Road Data

    def add_junction(self, index, location):
        self.junction_locations[index] = location
        if index not in self.adjacency:
            self.adjacency[index] = {}

    def add_road_connection(self, j_from, j_to, road_id, length):
        # Add edge to graph
        self.adjacency[j_from][j_to] = {'road_id': road_id, 'length': length}
        # Since roads are usually bidirectional in this data structure context
        self.adjacency[j_to][j_from] = {'road_id': road_id, 'length': length}

    def dijkstra(self, start_junction, end_junction) -> List[int]:
        """Returns a list of Junction Indices representing the path"""
        print(f"[DEBUG DIJKSTRA] Starting dijkstra from junction {start_junction} to {end_junction}")

        queue = [(0, start_junction, [])]  # cost, current_node, path
        visited = set()
        min_dist = {start_junction: 0}

        print(f"[DEBUG DIJKSTRA] Initial queue: {queue}")
        print(f"[DEBUG DIJKSTRA] Available junctions in adjacency: {list(self.adjacency.keys())}")

        iteration = 0
        while queue:
            iteration += 1
            (cost, u, path) = heapq.heappop(queue)

            print(
                f"[DEBUG DIJKSTRA] Iteration {iteration}: Processing node {u} with cost {cost:.2f}, path so far: {path}")

            if u in visited:
                print(f"[DEBUG DIJKSTRA] Node {u} already visited, skipping")
                continue

            visited.add(u)
            path = path + [u]

            print(f"[DEBUG DIJKSTRA] Updated path: {path}")

            if u == end_junction:
                print(f"[DEBUG DIJKSTRA] SUCCESS! Reached destination {end_junction}")
                print(f"[DEBUG DIJKSTRA] Final path: {path}, total cost: {cost:.2f}")
                return path

            if u in self.adjacency:
                neighbors = self.adjacency[u]
                print(f"[DEBUG DIJKSTRA] Node {u} has {len(neighbors)} neighbors: {list(neighbors.keys())}")

                for v, data in neighbors.items():
                    weight = data['length']
                    road_id = data['road_id']

                    if v not in visited:
                        new_cost = cost + weight
                        old_cost = min_dist.get(v, float('inf'))

                        if new_cost < old_cost:
                            min_dist[v] = new_cost
                            heapq.heappush(queue, (new_cost, v, path))
                            print(
                                f"[DEBUG DIJKSTRA]   -> Adding neighbor {v} via road {road_id}, cost: {new_cost:.2f} (weight: {weight:.2f})")
                        else:
                            print(
                                f"[DEBUG DIJKSTRA]   -> Skipping neighbor {v}, new_cost {new_cost:.2f} >= old_cost {old_cost:.2f}")
                    else:
                        print(f"[DEBUG DIJKSTRA]   -> Neighbor {v} already visited")
            else:
                print(f"[DEBUG DIJKSTRA] WARNING: Node {u} has no neighbors in adjacency!")

            print(f"[DEBUG DIJKSTRA] Queue size after iteration {iteration}: {len(queue)}")

        print(f"[DEBUG DIJKSTRA] FAILED! No path found from {start_junction} to {end_junction}")
        print(f"[DEBUG DIJKSTRA] Visited nodes: {visited}")
        print(f"[DEBUG DIJKSTRA] Final queue was empty")
        return []


# --- Main System ---

class NavigationSystem(AssistanceSystem):
    """Navigation with GPS emulation"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("sat_nav", event_bus, settings)

        # State
        self.sat_nav_active = True  # Default to active to allow processing
        self.current_track = "BL1"
        self.map_loaded = False

        # Navigation Data
        self.graph = NavigationGraph()
        self.roads_raw = {}  # road_id -> dict
        self.junctions_raw = []  # list of dicts

        # Route State
        self.destination_junction_idx = 1
        self.current_route_junctions = []  # List of junction indices [0, 4, 5, 2]
        self.current_route_roads = []  # List of road_ids corresponding to the path

        # Vehicle Tracking
        self.last_known_road_id = None
        self.last_notification_dist = float('inf')
        self.next_maneuver_emitted = False

        # Constants
        self.NOTIFICATION_DISTANCE = 150.0  # Meters before junction to notify
        self.OFF_ROUTE_TOLERANCE = 50.0  # Meters allowed away from road before recalc

    def _load_map_data(self):
        """Parses the JSON and builds the Graph"""
        file_path = f"track_data/track_data_{self.current_track[:2]}.json"

        if not os.path.exists(file_path):
            print(f"NavSystem: Map file not found {file_path}")
            return

        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
        except Exception as e:
            print(f"NavSystem: Error loading JSON: {e}")
            return

        # 1. Store Raw Data
        self.junctions_raw = data.get('junctions', [])
        for r in data.get('roads', []):
            # Calculate length for weight
            path_len = 0
            pts = r['path']
            for i in range(len(pts) - 1):
                path_len += Vector3.distance(pts[i], pts[i + 1])

            r['length'] = path_len
            self.roads_raw[r['road_id']] = r

        # 2. Build Graph Nodes
        for idx, j in enumerate(self.junctions_raw):
            self.graph.add_junction(idx, tuple(j['location']))

        # 3. Build Graph Edges
        # We need to find which junctions share a road
        road_to_junctions = {}  # road_id -> [junction_idx, junction_idx]

        for j_idx, j_data in enumerate(self.junctions_raw):
            for r_id in j_data['connected_roads']:
                if r_id not in road_to_junctions:
                    road_to_junctions[r_id] = []
                road_to_junctions[r_id].append(j_idx)

        for r_id, j_indices in road_to_junctions.items():
            if len(j_indices) == 2:
                # Standard road connecting two junctions
                length = self.roads_raw[r_id]['length']
                self.graph.add_road_connection(j_indices[0], j_indices[1], r_id, length)
            elif len(j_indices) > 2:
                # Multiple junctions on one road - need to order them along the path
                # and connect adjacent pairs
                road_path = self.roads_raw[r_id]['path']

                # Calculate each junction's position along the road path
                junction_positions = []  # [(junction_idx, distance_along_path)]

                for j_idx in j_indices:
                    j_loc = self.junctions_raw[j_idx]['location']
                    # Find closest point on road path to this junction
                    min_dist = float('inf')
                    best_segment_idx = 0
                    best_t = 0

                    for i in range(len(road_path) - 1):
                        p1 = road_path[i]
                        p2 = road_path[i + 1]

                        # Find projection of junction onto segment
                        ab = (p2[0] - p1[0], p2[1] - p1[1], p2[2] - p1[2])
                        ap = (j_loc[0] - p1[0], j_loc[1] - p1[1], j_loc[2] - p1[2])
                        ab_sq = ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2

                        if ab_sq > 0:
                            t = (ap[0] * ab[0] + ap[1] * ab[1] + ap[2] * ab[2]) / ab_sq
                            t = max(0, min(1, t))

                            closest = (p1[0] + t * ab[0], p1[1] + t * ab[1], p1[2] + t * ab[2])
                            dist = Vector3.distance(j_loc, closest)

                            if dist < min_dist:
                                min_dist = dist
                                best_segment_idx = i
                                best_t = t

                    # Calculate distance along path to this junction
                    dist_along_path = 0
                    for i in range(best_segment_idx):
                        dist_along_path += Vector3.distance(road_path[i], road_path[i + 1])

                    # Add partial distance for the segment where junction is located
                    if best_segment_idx < len(road_path) - 1:
                        segment_length = Vector3.distance(road_path[best_segment_idx],
                                                          road_path[best_segment_idx + 1])
                        dist_along_path += best_t * segment_length

                    junction_positions.append((j_idx, dist_along_path))

                # Sort junctions by their position along the road
                junction_positions.sort(key=lambda x: x[1])

                # Connect adjacent junctions
                for i in range(len(junction_positions) - 1):
                    j_from = junction_positions[i][0]
                    j_to = junction_positions[i + 1][0]

                    # Calculate distance between these two junctions
                    dist_from = junction_positions[i][1]
                    dist_to = junction_positions[i + 1][1]
                    length = abs(dist_to - dist_from)

                    self.graph.add_road_connection(j_from, j_to, r_id, length)
                    print(f"NavSystem: Connected junctions {j_from} <-> {j_to} on road {r_id} (length: {length:.2f}m)")
            # Note: Logic for loop roads or dead ends could be added here

        self.map_loaded = True
        print(f"NavSystem: Map Loaded. {len(self.roads_raw)} roads, {len(self.junctions_raw)} junctions.")

    def _get_closest_road(self, position: Tuple[float, float, float]) -> Tuple[int, float]:
        """
        Finds the road_id closest to the position.
        Returns (road_id, distance_to_road_centerline)
        """
        min_dist = float('inf')
        best_road = None

        # Optimization: Check last known road and its neighbors first
        candidate_roads = list(self.roads_raw.values())

        # (In a real massive map, you would use a QuadTree/KDTree here)

        for road in candidate_roads:
            # Optimization: Check distance to bounding box or first point before checking all segments
            # Simple check: distance to first point of road
            if not road['path']: continue

            # Rough check
            dist_to_start = Vector3.distance_sq(position, road['path'][0])
            # If we are very far from the start of the road, and the road isn't super long, skip?
            # For now, we do a segment check for accuracy.

            path = road['path']
            for i in range(len(path) - 1):
                p1 = path[i]
                p2 = path[i + 1]
                d = self._dist_to_segment(position, p1, p2)
                if d < min_dist:
                    min_dist = d
                    best_road = road['road_id']

        return best_road, min_dist

    def _dist_to_segment(self, p, a, b):
        """Distance from point p to segment ab"""
        # Vector AB
        ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        # Vector AP
        ap = (p[0] - a[0], p[1] - a[1], p[2] - a[2])

        ab_sq = ab[0] ** 2 + ab[1] ** 2 + ab[2] ** 2
        if ab_sq == 0: return Vector3.distance(p, a)

        # Project AP onto AB
        t = (ap[0] * ab[0] + ap[1] * ab[1] + ap[2] * ab[2]) / ab_sq

        # Clamp t to segment [0, 1]
        t = max(0, min(1, t))

        # Closest point
        c = (a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2])
        return Vector3.distance(p, c)

    def _recalculate_route(self, start_road_id):
        """
        Recalculates Dijkstra from the junctions connected to the current road
        to the destination.
        """
        print(f"[DEBUG] _recalculate_route called with start_road_id: {start_road_id}")

        if not self.map_loaded:
            print("[DEBUG] Map not loaded, aborting recalculation")
            return

        # Find which junctions this road connects
        connected_junctions = []
        print(f"[DEBUG] Searching for junctions connected to road {start_road_id}")

        for j_idx, j_data in enumerate(self.junctions_raw):
            if start_road_id in j_data['connected_roads']:
                connected_junctions.append(j_idx)
                print(f"[DEBUG] Found junction {j_idx} connected to road {start_road_id}")

        print(f"[DEBUG] Total connected junctions found: {len(connected_junctions)} - {connected_junctions}")

        if not connected_junctions:
            print("[DEBUG] No connected junctions found, aborting recalculation")
            return

        # Pick the closest junction or just pick one (Dijkstra handles the cost)
        # Ideally, we check which direction we are driving, but simply:
        start_node = connected_junctions[0]
        print(f"[DEBUG] Selected start_node: {start_node}, destination: {self.destination_junction_idx}")

        print(f"[DEBUG] Running Dijkstra from {start_node} to {self.destination_junction_idx}")
        path_indices = self.graph.dijkstra(start_node, self.destination_junction_idx)
        print(f"[DEBUG] Dijkstra returned path: {path_indices}")

        self.current_route_junctions = path_indices

        # Convert junction path to road path
        self.current_route_roads = []
        print(f"[DEBUG] Converting junction path to road path...")

        if len(path_indices) > 1:
            for i in range(len(path_indices) - 1):
                u = path_indices[i]
                v = path_indices[i + 1]
                road_info = self.graph.adjacency[u][v]
                road_id = road_info['road_id']
                self.current_route_roads.append(road_id)
                print(f"[DEBUG] Junction {u} -> {v}: road_id {road_id}, length {road_info['length']:.2f}m")

        print(f"[DEBUG] Road path before prepend: {self.current_route_roads}")

        # Prepend the current road if it's not the first in the list 
        # (Dijkstra starts at the junction, we might be in the middle of the road leading to it)
        if not self.current_route_roads or self.current_route_roads[0] != start_road_id:
            print(f"[DEBUG] Prepending start_road_id {start_road_id} to route")
            self.current_route_roads.insert(0, start_road_id)
        else:
            print(f"[DEBUG] start_road_id {start_road_id} already first in route, no prepend needed")

        print(f"NavSystem: Recalculated Route via: {self.current_route_roads}")
        print(f"[DEBUG] Route junctions: {self.current_route_junctions}")
        self.next_maneuver_emitted = False
        print("[DEBUG] _recalculate_route completed")

    def _determine_maneuver(self, current_road_id, next_road_id, junction_idx):
        """
        Calculates if the sequence is straight, left or right.
        Uses the geometry of the road ends near the junction.
        """
        # Get road data
        r_curr = self.roads_raw[current_road_id]['path']
        r_next = self.roads_raw[next_road_id]['path']
        j_loc = self.junctions_raw[junction_idx]['location']

        # Determine "Incoming Vector" (Car direction towards junction)
        # We check which end of r_curr is closer to j_loc
        dist_start = Vector3.distance_sq(r_curr[0], j_loc)
        dist_end = Vector3.distance_sq(r_curr[-1], j_loc)

        # If end is closer to junction, incoming vector is p[-2] -> p[-1]
        if dist_end < dist_start:
            p_near = r_curr[-1]
            p_far = r_curr[max(0, len(r_curr) - 5)]  # Go back a few points for stability
        else:
            p_near = r_curr[0]
            p_far = r_curr[min(len(r_curr) - 1, 5)]

        vec_in = Vector3.sub(p_near, p_far)

        # Determine "Outgoing Vector" (Direction away from junction)
        dist_start_n = Vector3.distance_sq(r_next[0], j_loc)
        dist_end_n = Vector3.distance_sq(r_next[-1], j_loc)

        if dist_start_n < dist_end_n:
            # Starts at junction, goes to end
            p_n_start = r_next[0]
            p_n_far = r_next[min(len(r_next) - 1, 5)]
        else:
            # Ends at junction (weird, but possible), so it goes "backwards" away
            p_n_start = r_next[-1]
            p_n_far = r_next[max(0, len(r_next) - 5)]

        vec_out = Vector3.sub(p_n_far, p_n_start)

        # Normalize
        vec_in = Vector3.normalize(vec_in)
        vec_out = Vector3.normalize(vec_out)

        # Cross Product (Y-up)
        # In many games: X is right, Y is up, Z is forward. 
        # cross_y returns positive/negative based on handedness.
        # Assuming standard right-hand rule where Y is up:
        # If Cross Y is Positive -> Left Turn
        # If Cross Y is Negative -> Right Turn
        cross_val = Vector3.cross_y(vec_in, vec_out)
        dot_val = Vector3.dot(vec_in, vec_out)  # approx 1 means straight

        if dot_val > 0.8:
            return "Go Straight"
        elif cross_val > 0:
            # Tune this based on your coordinate system (might need to swap to Right)
            return "Turn Left"
        else:
            return "Turn Right"

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """navigation processing"""
        if not self.is_enabled():
            return {'sat_nav_active': False}

        # 1. Init Map
        if not self.map_loaded:
            self._load_map_data()
            if not self.map_loaded:
                return {'sat_nav_active': False, 'error': 'Map failed load'}

        # 2. Get Position
        # Convert game units to map units (Meters)
        x = own_vehicle.data.x / 65536.0
        y = own_vehicle.data.y / 65536.0
        z = own_vehicle.data.z / 65536.0
        current_pos = (x, y, z)

        print(f"[DEBUG NAV] Current position: ({x:.2f}, {y:.2f}, {z:.2f})")

        # 3. Map Matching
        road_id, dist_to_road = self._get_closest_road(current_pos)

        print(f"[DEBUG NAV] Closest road: {road_id}, distance to road: {dist_to_road:.2f}m")

        if road_id is None:
            print("[DEBUG NAV] No road found - Off Map")
            return {'sat_nav_active': True, 'status': 'Off Map'}

        # 4. Route Logic

        # If we have no route, or we are on a road that is NOT in the current route, recalc
        on_route = road_id in self.current_route_roads

        print(f"[DEBUG NAV] Current route roads: {self.current_route_roads}")
        print(f"[DEBUG NAV] On route: {on_route}, current road_id: {road_id}")

        # If we are "on route" but the road_id isn't the first one, we have progressed.
        # Pop the previous roads.
        if on_route:
            print(f"[DEBUG NAV] Vehicle is on route")
            roads_popped = 0
            while self.current_route_roads and self.current_route_roads[0] != road_id:
                popped = self.current_route_roads.pop(0)
                roads_popped += 1
                print(f"[DEBUG NAV] Popped completed road: {popped}")
                # Also pop junctions if necessary? (Simpler to just trust road list)
                self.next_maneuver_emitted = False  # Reset notification for new leg

            if roads_popped > 0:
                print(f"[DEBUG NAV] Total roads popped: {roads_popped}, new route: {self.current_route_roads}")

        if not on_route or not self.current_route_roads:
            # We are off route (and close enough to a valid road to snap to it)
            print(f"[DEBUG NAV] Vehicle is OFF route or no route exists")
            if dist_to_road < self.OFF_ROUTE_TOLERANCE:
                print(f"[DEBUG NAV] Distance {dist_to_road:.2f}m < tolerance {self.OFF_ROUTE_TOLERANCE}m, recalculating...")
                self._recalculate_route(road_id)
            else:
                print(f"[DEBUG NAV] Distance {dist_to_road:.2f}m > tolerance {self.OFF_ROUTE_TOLERANCE}m, not recalculating")

        self.last_known_road_id = road_id

        # 5. Navigation Instructions
        print(f"[DEBUG NAV] Current route length: {len(self.current_route_roads)} roads")

        if len(self.current_route_roads) >= 2:
            # We have a current road and a next road
            next_road_id = self.current_route_roads[1]
            print(f"[DEBUG NAV] Current road: {road_id}, Next road: {next_road_id}")

            # Find the junction connecting these two
            target_junction_idx = -1

            # Look through junctions to find the one linking current and next
            for j_idx in self.current_route_junctions:
                j = self.junctions_raw[j_idx]
                if road_id in j['connected_roads'] and next_road_id in j['connected_roads']:
                    target_junction_idx = j_idx
                    print(f"[DEBUG NAV] Found connecting junction: {j_idx}")
                    break

            if target_junction_idx != -1:
                j_loc = self.junctions_raw[target_junction_idx]['location']
                dist_to_junction = Vector3.distance(current_pos, j_loc)

                print(f"[DEBUG NAV] Distance to next junction {target_junction_idx}: {dist_to_junction:.2f}m")
                print(f"[DEBUG NAV] Notification distance: {self.NOTIFICATION_DISTANCE}m")
                print(f"[DEBUG NAV] Next maneuver already emitted: {self.next_maneuver_emitted}")

                # Check Threshold
                if dist_to_junction < self.NOTIFICATION_DISTANCE and not self.next_maneuver_emitted:
                    maneuver = self._determine_maneuver(road_id, next_road_id, target_junction_idx)

                    print(f"[DEBUG NAV] EMITTING MANEUVER: {maneuver} in {int(dist_to_junction)}m")

                    self.event_bus.emit("notification", {
                        'notification': f"{maneuver} in {int(dist_to_junction)}m",
                        'type': 'navigation',
                        'icon': maneuver.lower().replace(" ", "_")  # e.g. turn_left
                    })
                    self.next_maneuver_emitted = True
                else:
                    print(f"[DEBUG NAV] Not emitting maneuver (distance: {dist_to_junction:.2f}m, already emitted: {self.next_maneuver_emitted})")
            else:
                print(f"[DEBUG NAV] WARNING: No junction found connecting road {road_id} and {next_road_id}")

        elif len(self.current_route_roads) == 1:
            print(f"[DEBUG NAV] On final road segment")
            # Check if we are near destination
            # Assuming destination is the last junction in current_route_junctions
            if self.current_route_junctions:
                dest_j_idx = self.current_route_junctions[-1]
                dest_loc = self.junctions_raw[dest_j_idx]['location']
                dist = Vector3.distance(current_pos, dest_loc)

                print(f"[DEBUG NAV] Distance to destination junction {dest_j_idx}: {dist:.2f}m")

                if dist < 50 and not self.next_maneuver_emitted:
                    print(f"[DEBUG NAV] EMITTING: Destination Reached")
                    self.event_bus.emit("notification", {'notification': 'Destination Reached'})
                    self.next_maneuver_emitted = True
        else:
            print(f"[DEBUG NAV] No route available")

        return {
            'sat_nav_active': self.sat_nav_active,
            'current_road': road_id,
            'next_turn_dist': 0  # Update with actual calculated var if needed for UI
        }