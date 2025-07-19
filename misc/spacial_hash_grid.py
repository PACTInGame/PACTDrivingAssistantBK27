from typing import List, Tuple, Dict, Set, Optional


class SpatialHashGrid:
    """
    Spatial Hash Grid für effiziente Kollisionserkennung bei Park-Assistenzsystemen.
    Optimiert für Objekte mit 4-Punkt-Koordinaten mit präziser Kollisionserkennung.
    """

    def __init__(self, cell_size: float = 10.0):
        """
        Initialisiert das Spatial Hash Grid.

        Args:
            cell_size: Größe einer Grid-Zelle in Metern (empfohlen: 5-15m)
        """
        self.cell_size = cell_size
        self.grid: Dict[Tuple[int, int], List[Dict]] = {}
        self.static_objects: Dict[int, List[Tuple[float, float]]] = {}
        self.dynamic_objects: Dict[int, List[Tuple[float, float]]] = {}

    def world_to_grid(self, x: float, y: float) -> Tuple[int, int]:
        """Wandelt Weltkoordinaten in Grid-Koordinaten um."""
        return (int(x // self.cell_size), int(y // self.cell_size))

    def calculate_bbox(self, points: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
        """
        Berechnet die Axis-Aligned Bounding Box aus 4 Punkten.
        Diese wird nur für die Spatial Hash Grid Optimierung verwendet.

        Args:
            points: Liste von Koordinaten [(x1,y1), (x2,y2), (x3,y3), (x4,y4)]

        Returns:
            Tuple: (min_x, min_y, max_x, max_y)
        """
        if not points or len(points) < 3:
            raise ValueError("Mindestens 3 Punkte erforderlich")

        min_x = min(point[0] for point in points)
        max_x = max(point[0] for point in points)
        min_y = min(point[1] for point in points)
        max_y = max(point[1] for point in points)

        return (min_x, min_y, max_x, max_y)

    def get_grid_bounds(self, points: List[Tuple[float, float]]) -> Tuple[int, int, int, int]:
        """
        Berechnet die Grid-Grenzen für ein Objekt mit 4 Punkten.

        Returns:
            Tuple: (grid_min_x, grid_min_y, grid_max_x, grid_max_y)
        """
        min_x, min_y, max_x, max_y = self.calculate_bbox(points)

        grid_min_x, grid_min_y = self.world_to_grid(min_x, min_y)
        grid_max_x, grid_max_y = self.world_to_grid(max_x, max_y)

        return grid_min_x, grid_min_y, grid_max_x, grid_max_y

    def point_in_polygon(self, point: Tuple[float, float], polygon: List[Tuple[float, float]]) -> bool:
        """
        Prüft ob ein Punkt innerhalb eines Polygons liegt (Ray Casting Algorithm).

        Args:
            point: Der zu prüfende Punkt (x, y)
            polygon: Liste der Polygon-Eckpunkte

        Returns:
            True wenn der Punkt im Polygon liegt
        """
        x, y = point
        n = len(polygon)
        inside = False

        p1x, p1y = polygon[0]
        for i in range(1, n + 1):
            p2x, p2y = polygon[i % n]
            if y > min(p1y, p2y):
                if y <= max(p1y, p2y):
                    if x <= max(p1x, p2x):
                        if p1y != p2y:
                            xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                        if p1x == p2x or x <= xinters:
                            inside = not inside
            p1x, p1y = p2x, p2y

        return inside

    def line_intersects_line(self, line1: Tuple[Tuple[float, float], Tuple[float, float]],
                             line2: Tuple[Tuple[float, float], Tuple[float, float]]) -> bool:
        """
        Prüft ob sich zwei Linien schneiden.

        Args:
            line1: ((x1, y1), (x2, y2))
            line2: ((x3, y3), (x4, y4))

        Returns:
            True wenn sich die Linien schneiden
        """
        (x1, y1), (x2, y2) = line1
        (x3, y3), (x4, y4) = line2

        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-10:  # Parallel lines
            return False

        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom

        return 0 <= t <= 1 and 0 <= u <= 1

    def polygons_intersect(self, poly1: List[Tuple[float, float]],
                           poly2: List[Tuple[float, float]]) -> bool:
        """
        Prüft ob zwei Polygone sich überschneiden.

        Args:
            poly1: Erstes Polygon als Liste von Punkten
            poly2: Zweites Polygon als Liste von Punkten

        Returns:
            True wenn sich die Polygone überschneiden
        """
        # Test 1: Prüfe ob ein Polygon Punkte des anderen enthält
        for point in poly1:
            if self.point_in_polygon(point, poly2):
                return True

        for point in poly2:
            if self.point_in_polygon(point, poly1):
                return True

        # Test 2: Prüfe ob sich Kanten schneiden
        n1, n2 = len(poly1), len(poly2)

        for i in range(n1):
            line1 = (poly1[i], poly1[(i + 1) % n1])
            for j in range(n2):
                line2 = (poly2[j], poly2[(j + 1) % n2])
                if self.line_intersects_line(line1, line2):
                    return True

        return False

    def polygon_intersects_circle(self, polygon: List[Tuple[float, float]],
                                  center: Tuple[float, float], radius: float) -> bool:
        """
        Prüft ob ein Polygon einen Kreis schneidet.

        Args:
            polygon: Liste der Polygon-Eckpunkte
            center: Mittelpunkt des Kreises (x, y)
            radius: Radius des Kreises

        Returns:
            True wenn sich Polygon und Kreis überschneiden
        """
        cx, cy = center

        # Test 1: Ist der Kreismittelpunkt im Polygon?
        if self.point_in_polygon(center, polygon):
            return True

        # Test 2: Prüfe Distanz zu allen Kanten
        n = len(polygon)
        for i in range(n):
            x1, y1 = polygon[i]
            x2, y2 = polygon[(i + 1) % n]

            # Distanz vom Punkt zur Linie berechnen
            line_length_sq = (x2 - x1) ** 2 + (y2 - y1) ** 2
            if line_length_sq == 0:
                # Punkt zu Punkt Distanz
                dist_sq = (cx - x1) ** 2 + (cy - y1) ** 2
            else:
                # Projektion auf die Linie
                t = max(0.0, min(1.0, ((cx - x1) * (x2 - x1) + (cy - y1) * (y2 - y1)) / line_length_sq))
                projection_x = x1 + t * (x2 - x1)
                projection_y = y1 + t * (y2 - y1)
                dist_sq = (cx - projection_x) ** 2 + (cy - projection_y) ** 2

            if dist_sq <= radius ** 2:
                return True

        return False

    def polygon_intersects_rectangle(self, polygon: List[Tuple[float, float]],
                                     rect_bounds: Tuple[float, float, float, float]) -> bool:
        """
        Prüft ob ein Polygon ein achsenausgerichtetes Rechteck schneidet.

        Args:
            polygon: Liste der Polygon-Eckpunkte
            rect_bounds: (min_x, min_y, max_x, max_y)

        Returns:
            True wenn sich Polygon und Rechteck überschneiden
        """
        min_x, min_y, max_x, max_y = rect_bounds
        rectangle = [(min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)]

        return self.polygons_intersect(polygon, rectangle)

    def insert_object(self, object_id: int, points: List[Tuple[float, float]],
                      is_static: bool = True, metadata: Optional[Dict] = None):
        """
        Fügt ein Objekt ins Grid ein.

        Args:
            object_id: Eindeutige ID des Objekts
            points: 4 Koordinaten des Objekts
            is_static: True für statische Objekte (Straßenobjekte), False für Fahrzeuge
            metadata: Zusätzliche Objektdaten
        """
        if len(points) < 3:
            raise ValueError("Mindestens 3 Punkte erforderlich")

        # Objekt-Info erstellen - jetzt mit beiden Geometrien
        obj_info = {
            'id': object_id,
            'points': points.copy(),  # Tatsächliche Geometrie für präzise Kollision
            'bbox': self.calculate_bbox(points),  # AABB nur für Grid-Optimierung
            'is_static': is_static,
            'metadata': metadata or {}
        }

        # Grid-Bereiche berechnen (basierend auf AABB)
        grid_min_x, grid_min_y, grid_max_x, grid_max_y = self.get_grid_bounds(points)

        # Objekt in alle überschneidenden Grid-Zellen einfügen
        for grid_x in range(grid_min_x, grid_max_x + 1):
            for grid_y in range(grid_min_y, grid_max_y + 1):
                cell_key = (grid_x, grid_y)

                if cell_key not in self.grid:
                    self.grid[cell_key] = []

                self.grid[cell_key].append(obj_info)

        # Objekt-Tracking
        if is_static:
            self.static_objects[object_id] = points.copy()
        else:
            self.dynamic_objects[object_id] = points.copy()

    def remove_object(self, object_id: int, points: Optional[List[Tuple[float, float]]] = None):
        """
        Entfernt ein Objekt aus dem Grid.

        Args:
            object_id: ID des zu entfernenden Objekts
            points: Koordinaten (wenn bekannt, sonst wird gesucht)
        """
        # Koordinaten finden wenn nicht gegeben
        if points is None:
            if object_id in self.static_objects:
                points = self.static_objects[object_id]
            elif object_id in self.dynamic_objects:
                points = self.dynamic_objects[object_id]
            else:
                # Objekt nicht gefunden, nichts zu tun
                return

        # Grid-Bereiche berechnen
        grid_min_x, grid_min_y, grid_max_x, grid_max_y = self.get_grid_bounds(points)

        # Objekt aus allen Grid-Zellen entfernen
        for grid_x in range(grid_min_x, grid_max_x + 1):
            for grid_y in range(grid_min_y, grid_max_y + 1):
                cell_key = (grid_x, grid_y)

                if cell_key in self.grid:
                    self.grid[cell_key] = [obj for obj in self.grid[cell_key]
                                           if obj['id'] != object_id]

                    # Leere Zellen entfernen
                    if not self.grid[cell_key]:
                        del self.grid[cell_key]

        # Aus Tracking entfernen
        if object_id in self.static_objects:
            del self.static_objects[object_id]
        if object_id in self.dynamic_objects:
            del self.dynamic_objects[object_id]

    def update_dynamic_object(self, object_id: int, new_points: List[Tuple[float, float]],
                              metadata: Optional[Dict] = None):
        """
        Aktualisiert ein bewegliches Objekt (z.B. Fahrzeug).

        Args:
            object_id: ID des Objekts
            new_points: Neue Koordinaten
            metadata: Aktualisierte Metadaten
        """
        # Altes Objekt entfernen
        if object_id in self.dynamic_objects:
            old_points = self.dynamic_objects[object_id]
            self.remove_object(object_id, old_points)

        # Neues Objekt einfügen
        self.insert_object(object_id, new_points, is_static=False, metadata=metadata)

    def query_area(self, center_x: float, center_y: float, radius: float) -> List[Dict]:
        """
        Findet alle Objekte in einem kreisförmigen Bereich.
        Verwendet präzise Polygon-Kreis-Kollisionserkennung.

        Args:
            center_x, center_y: Mittelpunkt der Suche
            radius: Suchradius in Metern

        Returns:
            Liste der gefundenen Objekte
        """
        # Grid-Bereich um das Zentrum (AABB für Performance)
        grid_min_x, grid_min_y = self.world_to_grid(center_x - radius, center_y - radius)
        grid_max_x, grid_max_y = self.world_to_grid(center_x + radius, center_y + radius)

        nearby_objects = []
        processed_ids: Set[int] = set()

        for grid_x in range(grid_min_x, grid_max_x + 1):
            for grid_y in range(grid_min_y, grid_max_y + 1):
                cell_key = (grid_x, grid_y)

                if cell_key in self.grid:
                    for obj in self.grid[cell_key]:
                        if obj['id'] not in processed_ids:
                            # Präzise Polygon-Kreis-Kollisionserkennung
                            if self.polygon_intersects_circle(obj['points'], (center_x, center_y), radius):
                                nearby_objects.append(obj)
                                processed_ids.add(obj['id'])

        return nearby_objects

    def query_rectangle(self, min_x: float, min_y: float,
                        max_x: float, max_y: float) -> List[Dict]:
        """
        Findet alle Objekte in einem rechteckigen Bereich.
        Verwendet präzise Polygon-Rechteck-Kollisionserkennung.

        Args:
            min_x, min_y: Untere linke Ecke
            max_x, max_y: Obere rechte Ecke

        Returns:
            Liste der gefundenen Objekte
        """
        grid_min_x, grid_min_y = self.world_to_grid(min_x, min_y)
        grid_max_x, grid_max_y = self.world_to_grid(max_x, max_y)

        nearby_objects = []
        processed_ids: Set[int] = set()

        for grid_x in range(grid_min_x, grid_max_x + 1):
            for grid_y in range(grid_min_y, grid_max_y + 1):
                cell_key = (grid_x, grid_y)

                if cell_key in self.grid:
                    for obj in self.grid[cell_key]:
                        if obj['id'] not in processed_ids:
                            # Präzise Polygon-Rechteck-Kollisionserkennung
                            if self.polygon_intersects_rectangle(obj['points'], (min_x, min_y, max_x, max_y)):
                                nearby_objects.append(obj)
                                processed_ids.add(obj['id'])

        return nearby_objects

    def query_polygon_collision(self, query_polygon: List[Tuple[float, float]]) -> List[Dict]:
        """
        Findet alle Objekte die mit einem gegebenen Polygon kollidieren.
        Verwendet präzise Polygon-Polygon-Kollisionserkennung.

        HINWEIS: Diese Methode ist weniger effizient als die zwei-stufige Filterung
        mit query_area() + polygon_overlap(). Verwende sie nur wenn nötig.

        Args:
            query_polygon: Das Such-Polygon als Liste von Punkten

        Returns:
            Liste der kollidierenden Objekte
        """
        # Grid-Bereich basierend auf AABB des Query-Polygons
        grid_min_x, grid_min_y, grid_max_x, grid_max_y = self.get_grid_bounds(query_polygon)

        colliding_objects = []
        processed_ids: Set[int] = set()

        for grid_x in range(grid_min_x, grid_max_x + 1):
            for grid_y in range(grid_min_y, grid_max_y + 1):
                cell_key = (grid_x, grid_y)

                if cell_key in self.grid:
                    for obj in self.grid[cell_key]:
                        if obj['id'] not in processed_ids:
                            # Präzise Polygon-Polygon-Kollisionserkennung
                            if self.polygons_intersect(query_polygon, obj['points']):
                                colliding_objects.append(obj)
                                processed_ids.add(obj['id'])

        return colliding_objects

    def polygon_overlap(self, poly1: List[Tuple[float, float]],
                        poly2: List[Tuple[float, float]]) -> bool:
        """
        Effiziente Polygon-Überlappungsprüfung für bereits gefilterte Kandidaten.
        Diese Methode sollte nach AABB-Filterung verwendet werden.

        Args:
            poly1: Erstes Polygon
            poly2: Zweites Polygon

        Returns:
            True wenn sich die Polygone überschneiden
        """
        return self.polygons_intersect(poly1, poly2)

    def point_overlap(self, point: Tuple[float, float],
                      polygon: List[Tuple[float, float]]) -> bool:
        """
        Prüft ob ein Punkt mit einem Polygon überlappt.

        Args:
            point: Der zu prüfende Punkt (x, y)
            polygon: Das Polygon

        Returns:
            True wenn der Punkt im Polygon liegt
        """
        return self.point_in_polygon(point, polygon)

    def circle_overlap(self, center: Tuple[float, float], radius: float,
                       polygon: List[Tuple[float, float]]) -> bool:
        """
        Prüft ob ein Kreis mit einem Polygon überlappt.

        Args:
            center: Kreismittelpunkt (x, y)
            radius: Kreisradius
            polygon: Das Polygon

        Returns:
            True wenn sich Kreis und Polygon überschneiden
        """
        return self.polygon_intersects_circle(polygon, center, radius)

    def is_bbox_in_radius(self, bbox: Tuple[float, float, float, float],
                          center_x: float, center_y: float, radius: float) -> bool:
        """Prüft ob eine Bounding Box im Radius liegt. (Legacy-Methode)"""
        min_x, min_y, max_x, max_y = bbox

        # Nächster Punkt der Box zum Zentrum
        closest_x = max(min_x, min(center_x, max_x))
        closest_y = max(min_y, min(center_y, max_y))

        # Distanz zum nächsten Punkt
        distance = math.sqrt((closest_x - center_x) ** 2 + (closest_y - center_y) ** 2)
        return distance <= radius

    def bbox_overlap(self, bbox1: Tuple[float, float, float, float],
                     bbox2: Tuple[float, float, float, float]) -> bool:
        """Prüft ob zwei Bounding Boxes überlappen. (Legacy-Methode)"""
        min_x1, min_y1, max_x1, max_y1 = bbox1
        min_x2, min_y2, max_x2, max_y2 = bbox2

        return not (max_x1 < min_x2 or max_x2 < min_x1 or
                    max_y1 < min_y2 or max_y2 < min_y1)

    def get_statistics(self) -> Dict:
        """Gibt Statistiken über das Grid zurück."""
        total_cells = len(self.grid)
        total_objects = len(self.static_objects) + len(self.dynamic_objects)

        if total_cells > 0:
            avg_objects_per_cell = sum(len(cell) for cell in self.grid.values()) / total_cells
        else:
            avg_objects_per_cell = 0

        return {
            'total_cells': total_cells,
            'total_objects': total_objects,
            'static_objects': len(self.static_objects),
            'dynamic_objects': len(self.dynamic_objects),
            'avg_objects_per_cell': avg_objects_per_cell,
            'cell_size': self.cell_size
        }

    def clear(self):
        """Leert das komplette Grid."""
        self.grid.clear()
        self.static_objects.clear()
        self.dynamic_objects.clear()


# Beispiel-Verwendung und Tests:
if __name__ == "__main__":
    # Grid initialisieren
    grid = SpatialHashGrid(cell_size=15.0)

    # Test 1: Achsenausgerichtetes Rechteck
    grid.insert_object(1, [(10, 10), (15, 10), (15, 15), (10, 15)], is_static=True)

    # Test 2: Rotiertes Rechteck (45° gedreht)
    import math

    angle = math.radians(45)
    cx, cy = 30, 30  # Zentrum
    w, h = 5, 3  # Breite und Höhe

    # Rechteck-Punkte um Zentrum rotieren
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    rotated_points = []
    for dx, dy in [(-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)]:
        rx = cx + dx * cos_a - dy * sin_a
        ry = cy + dx * sin_a + dy * cos_a
        rotated_points.append((rx, ry))

    grid.insert_object(2, rotated_points, is_static=True)

    # Test 3: Dynamisches Objekt (Fahrzeug)
    grid.insert_object(100, [(50, 50), (54, 50), (54, 53), (50, 53)], is_static=False)

    print("=== Test: Kreissuche ===")
    # Fahrzeug-Position: Nach Objekten in 20m Radius suchen
    vehicle_pos = (12, 12)
    nearby = grid.query_area(vehicle_pos[0], vehicle_pos[1], 20)

    print(f"Gefundene Objekte in 20m Radius um ({vehicle_pos[0]}, {vehicle_pos[1]}): {len(nearby)}")
    for obj in nearby:
        print(f"  ID: {obj['id']}, Statisch: {obj['is_static']}, Punkte: {obj['points'][:2]}...")

    print("\n=== Test: Rechtecksuche ===")
    # Rechteckige Suche
    rect_objects = grid.query_rectangle(25, 25, 35, 35)
    print(f"Gefundene Objekte im Rechteck (25,25)-(35,35): {len(rect_objects)}")
    for obj in rect_objects:
        print(f"  ID: {obj['id']}, Statisch: {obj['is_static']}")

    print("\n=== Test: Polygon-Kollision ===")
    # Test mit einem Such-Polygon
    search_polygon = [(28, 28), (32, 28), (32, 32), (28, 32)]
    colliding = grid.query_polygon_collision(search_polygon)
    print(f"Kollidierende Objekte mit Such-Polygon: {len(colliding)}")
    for obj in colliding:
        print(f"  ID: {obj['id']}, Statisch: {obj['is_static']}")

    print("\n=== Test: Fahrzeug bewegen ===")
    # Fahrzeug bewegen
    grid.update_dynamic_object(100, [(55, 55), (59, 55), (59, 58), (55, 58)])
    print("Fahrzeug zu Position (55,55) bewegt")

    # Erneute Suche nach der Bewegung
    nearby_after_move = grid.query_area(57, 57, 10)
    print(f"Objekte in 10m Radius um (57,57): {len(nearby_after_move)}")

    print("\n=== Grid-Statistiken ===")
    stats = grid.get_statistics()
    for key, value in stats.items():
        print(f"{key}: {value}")

    print("\n=== Genauigkeitstest ===")
    # Test der Genauigkeit: Ein rotiertes Objekt sollte nicht mit einem weit entfernten Punkt kollidieren
    test_point = (50, 50)  # Weit weg vom rotierten Rechteck bei (30,30)
    distant_objects = grid.query_area(test_point[0], test_point[1], 5)
    rotated_in_results = any(obj['id'] == 2 for obj in distant_objects)
    print(f"Rotiertes Rechteck fälschlicherweise in 5m Radius um (50,50) erkannt: {rotated_in_results}")

    # Aber es sollte in einem größeren Radius erkannt werden
    larger_search = grid.query_area(test_point[0], test_point[1], 25)
    rotated_in_larger = any(obj['id'] == 2 for obj in larger_search)
    print(f"Rotiertes Rechteck korrekt in 25m Radius um (50,50) erkannt: {rotated_in_larger}")