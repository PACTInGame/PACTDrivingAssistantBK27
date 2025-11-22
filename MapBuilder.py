import math
import matplotlib.pyplot as plt
from collections import namedtuple

# --- CONFIGURATION ---
LANE_WIDTH = 4.0
MERGE_THRESHOLD = 6.0       # Max distance to merge parallel opposite roads
Z_TOLERANCE = 1.0           # Height difference tolerance for bridges/tunnels
JUMP_THRESHOLD = 15.0       # If car jumps (teleport), don't draw a line
SAMPLE_DIST = 2.0           # Minimum distance between recorded geometry points

# --- DATA STRUCTURES ---

class Node:
    def __init__(self, id, x, y, z):
        self.id = id
        self.x = x
        self.y = y
        self.z = z

class Edge:
    def __init__(self, id, start_node, end_node):
        self.id = id
        self.start_node = start_node
        self.end_node = end_node
        self.points = []  # List of (x,y,z) tuples
        self.is_two_way = False
        self.heading_at_creation = 0

class MapBuilder:
    def __init__(self):
        self.nodes = []
        self.edges = []
        self.node_counter = 0
        self.edge_counter = 0

        # State Machine
        self.is_recording = False
        self.current_edge_start_node = None
        self.current_geometry = [] # Temporary points for current segment
        self.last_pos = None

        # Visualization
        self.fig, self.ax = plt.subplots()
        self.ax.set_aspect('equal')
        self.setup_plot()

    def setup_plot(self):
        plt.ion() # Interactive mode on
        self.ax.set_title("Live Map Generation")
        self.ax.set_xlabel("X Coordinate")
        self.ax.set_ylabel("Y Coordinate")

    # --- MATH HELPERS ---

    def dist(self, p1, p2):
        return math.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2)

    def dist_3d(self, p1, p2):
        return math.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2 + (p1[2]-p2[2])**2)

    def get_vector(self, angle_deg):
        # Convert Game Heading (0=N, CCW) to Math Vector
        # N(0)->(0,1), W(90)->(-1,0), S(180)->(0,-1), E(270)->(1,0)
        rad = math.radians(angle_deg)
        return (-math.sin(rad), math.cos(rad))

    def angle_diff(self, a1, a2):
        diff = abs(a1 - a2) % 360
        return 360 - diff if diff > 180 else diff

    def segments_intersect(self, p1, p2, p3, p4):
        # Standard Line Segment Intersection Logic (2D only)
        # Returns intersection point (x,y) or None
        x1, y1, _ = p1
        x2, y2, _ = p2
        x3, y3 = p3[0], p3[1]
        x4, y4 = p4[0], p4[1]

        denom = (y4 - y3) * (x2 - x1) - (x4 - x3) * (y2 - y1)
        if denom == 0: return None # Parallel

        ua = ((x4 - x3) * (y1 - y3) - (y4 - y3) * (x1 - x3)) / denom
        ub = ((x2 - x1) * (y1 - y3) - (y2 - y1) * (x1 - x3)) / denom

        if 0 <= ua <= 1 and 0 <= ub <= 1:
            ix = x1 + ua * (x2 - x1)
            iy = y1 + ua * (y2 - y1)
            return (ix, iy)
        return None

    # --- CORE LOGIC ---

    def get_closest_edge(self, pos, heading):
        """
        Finds an edge that is close (within threshold) and on same Z-level.
        Returns (Edge, distance, is_opposite_direction)
        """
        closest_dist = float('inf')
        target_edge = None
        is_opposite = False

        x, y, z = pos

        for edge in self.edges:
            # Optimization: Check Z bounding box first
            if not edge.points: continue

            # Heuristic: Check distance to every point in edge (Expensive but accurate)
            # For production, use a QuadTree or Spatial Hash here.
            for pt in edge.points:
                if abs(pt[2] - z) > Z_TOLERANCE: continue # Height check (Bridge/Tunnel)

                d = self.dist((x,y), pt)
                if d < MERGE_THRESHOLD:
                    # We found a close road. Check Heading.
                    # Calculate approximate heading of this segment of the edge
                    # (Simplified: compare against edge creation heading)

                    diff = self.angle_diff(heading, edge.heading_at_creation)

                    # Case A: Same direction (re-driving same road)
                    if d < LANE_WIDTH and diff < 45:
                        if d < closest_dist:
                            closest_dist = d
                            target_edge = edge
                            is_opposite = False

                    # Case B: Opposite direction (Merging into 2-way)
                    elif diff > 135:
                        if d < closest_dist:
                            closest_dist = d
                            target_edge = edge
                            is_opposite = True

        return target_edge, closest_dist, is_opposite

    def create_node(self, x, y, z):
        n = Node(self.node_counter, x, y, z)
        self.nodes.append(n)
        self.node_counter += 1
        return n

    def create_edge(self, start_node, end_node, geometry, heading):
        e = Edge(self.edge_counter, start_node, end_node)
        e.points = geometry
        e.heading_at_creation = heading
        self.edges.append(e)
        self.edge_counter += 1
        return e

    def split_edge_at_point(self, edge, point_coords):
        """
        Splits an existing edge into two edges at point_coords.
        Returns the new middle node.
        """
        # 1. Find index of closest point in geometry to split
        best_idx = 0
        min_d = float('inf')
        px, py = point_coords

        for i, pt in enumerate(edge.points):
            d = math.sqrt((pt[0]-px)**2 + (pt[1]-py)**2)
            if d < min_d:
                min_d = d
                best_idx = i

        # 2. Create new Middle Node
        mid_z = edge.points[best_idx][2]
        mid_node = self.create_node(px, py, mid_z)

        # 3. Create second half edge (Mid -> End)
        new_geom_2 = edge.points[best_idx:]
        # Ensure continuity
        if not new_geom_2: new_geom_2 = [edge.points[-1]]

        edge2 = self.create_edge(mid_node, edge.end_node, new_geom_2, edge.heading_at_creation)
        edge2.is_two_way = edge.is_two_way

        # 4. Truncate original edge (Start -> Mid)
        edge.points = edge.points[:best_idx+1]
        edge.end_node = mid_node

        return mid_node

    # --- USER CONTROLS ---

    def start_recording(self):
        print(">> STARTED Recording")
        self.is_recording = True
        self.current_geometry = []
        self.current_edge_start_node = None
        self.last_pos = None

    def pause_recording(self):
        print(">> PAUSED Recording (Dead End / Turning)")
        self.finish_current_segment()
        self.is_recording = False
        self.last_pos = None

    def stop_recording(self):
        print(">> STOPPED Recording")
        self.finish_current_segment()
        self.is_recording = False

    def finish_current_segment(self):
        if self.current_edge_start_node and len(self.current_geometry) > 1:
            # Create end node at last pos
            last_pt = self.current_geometry[-1]
            end_node = self.create_node(*last_pt)

            # Save the edge
            # Note: we store heading of the first segment for simplicity
            heading = 0 # You might want to store average heading
            self.create_edge(self.current_edge_start_node, end_node, self.current_geometry, heading)

        self.current_geometry = []
        self.current_edge_start_node = None

    # --- MAIN LOOP CALLED EVERY SECOND ---

    def update_position(self, x, y, z, heading):
        current_pos = (x, y, z)

        # 1. Update Visuals (Debug View)
        self.render_debug_view(current_pos, heading)

        if not self.is_recording:
            return

        # Check for teleportation (game reset)
        if self.last_pos and self.dist_3d(current_pos, self.last_pos) > JUMP_THRESHOLD:
            self.finish_current_segment()
            self.last_pos = current_pos
            return

        # 2. Check environment (Overlaps / Merges)
        nearby_edge, dist, is_opposite = self.get_closest_edge(current_pos, heading)

        if nearby_edge:
            if is_opposite:
                # We are driving the return lane of a 2-way road
                if not nearby_edge.is_two_way:
                    print(f"Merging: Marked Edge {nearby_edge.id} as TWO-WAY")
                    nearby_edge.is_two_way = True

                # If we were drawing a line, stop it (we merged into existing)
                self.finish_current_segment()
                self.last_pos = current_pos
                return

            else:
                # We are re-driving an existing lane
                # Do nothing, just update position
                self.finish_current_segment() # Ensure we don't draw duplicates
                self.last_pos = current_pos
                return

        # 3. We are in new territory
        if self.current_edge_start_node is None:
            # Start a new segment
            self.current_edge_start_node = self.create_node(x, y, z)
            self.current_geometry = [current_pos]
        else:
            # Check intersection with OTHER edges (Crossing logic)
            if self.last_pos:
                collision_point = None
                hit_edge = None

                # Check against all edges (except the one we are making)
                for edge in self.edges:
                    # Optimization: Bounding box check first
                    # Z-check
                    if not edge.points or abs(edge.points[0][2] - z) > Z_TOLERANCE:
                        continue

                    # Check every segment of this edge
                    for i in range(len(edge.points)-1):
                        p1 = edge.points[i]
                        p2 = edge.points[i+1]

                        # Check intersection between (LastPos->CurrPos) and (P1->P2)
                        intersect = self.segments_intersect(self.last_pos, current_pos, p1, p2)
                        if intersect:
                            collision_point = intersect
                            hit_edge = edge
                            break
                    if hit_edge: break

                if hit_edge:
                    print(f"Junction Detected at {collision_point}")
                    # 1. Split the Hit Edge
                    junction_node = self.split_edge_at_point(hit_edge, collision_point)

                    # 2. Finish current driving edge at junction
                    self.current_geometry.append((*collision_point, z))
                    self.create_edge(self.current_edge_start_node, junction_node, self.current_geometry, heading)

                    # 3. Start new driving edge from junction
                    self.current_edge_start_node = junction_node
                    self.current_geometry = [current_pos]
                else:
                    # Just add the point if distance is enough
                    if self.dist(self.current_geometry[-1], current_pos) > SAMPLE_DIST:
                        self.current_geometry.append(current_pos)

        self.last_pos = current_pos

    def render_debug_view(self, car_pos, car_heading):
        self.ax.clear()
        self.ax.set_title(f"Map Debug - Pos: {int(car_pos[0])}, {int(car_pos[1])}")

        # Draw Edges
        for e in self.edges:
            xs = [p[0] for p in e.points]
            ys = [p[1] for p in e.points]
            color = 'green' if e.is_two_way else 'blue'
            # Draw thicker line for roads
            self.ax.plot(xs, ys, c=color, linewidth=2)
            # Draw direction arrow at end
            if len(xs) > 1:
                self.ax.arrow(xs[-2], ys[-2], xs[-1]-xs[-2], ys[-1]-ys[-2],
                              head_width=2, color=color)

        # Draw Current Path (Red)
        if self.is_recording and len(self.current_geometry) > 0:
            xs = [p[0] for p in self.current_geometry]
            ys = [p[1] for p in self.current_geometry]
            self.ax.plot(xs, ys, c='red', linestyle='--', linewidth=1)

        # Draw Car
        self.ax.plot(car_pos[0], car_pos[1], 'ro', markersize=8)

        # Draw Heading Vector
        vec = self.get_vector(car_heading)
        self.ax.arrow(car_pos[0], car_pos[1], vec[0]*10, vec[1]*10, head_width=3, color='black')

        plt.draw()
        plt.pause(0.001)

# --- EXAMPLE USAGE SIMULATION ---
if __name__ == "__main__":
    import time

    mapper = MapBuilder()

    # Simulate: User starts recording
    mapper.start_recording()

    # Simulate: Driving North
    print("Driving North...")
    for i in range(0, 50, 5):
        mapper.update_position(0, i, 0, 0) # x=0, y increasing, heading N(0)
        time.sleep(0.1)

    # Simulate: Turning East (Right)
    print("Turning East...")
    for i in range(0, 50, 5):
        mapper.update_position(i, 50, 0, 270) # x increasing, y=50, heading E(270)
        time.sleep(0.1)

    # Simulate: Driving South (Parallel to first road, but far away -> Highway)
    print("Driving South (Highway separate)...")
    mapper.current_edge_start_node = None # Reset for simulation jump
    mapper.current_geometry = []
    for i in range(50, -10, -5):
        mapper.update_position(20, i, 0, 180) # x=20 (20m away), South
        time.sleep(0.1)

    # Simulate: Driving South (CLOSE to first road -> Two Way Merge)
    print("Driving South (Merge test)...")
    mapper.last_pos = None # Teleport logic for sim
    for i in range(50, 0, -5):
        # Drive at x=3 (3 meters from x=0), Heading South (180)
        mapper.update_position(3, i, 0, 180)
        time.sleep(0.1)

    print("Done. Close window to exit.")
    plt.ioff()
    plt.show()