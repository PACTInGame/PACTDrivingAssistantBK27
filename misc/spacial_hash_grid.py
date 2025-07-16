import math
from typing import List, Tuple, Dict, Set, Optional


class SpatialHashGrid:
    """
    Spatial Hash Grid für effiziente Kollisionserkennung bei Park-Assistenzsystemen.
    Optimiert für Objekte mit 4-Punkt-Koordinaten.
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

        Args:
            points: Liste von 4 Koordinaten [(x1,y1), (x2,y2), (x3,y3), (x4,y4)]

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

        # Objekt-Info erstellen
        obj_info = {
            'id': object_id,
            'points': points.copy(),
            'bbox': self.calculate_bbox(points),
            'is_static': is_static,
            'metadata': metadata or {}
        }

        # Grid-Bereiche berechnen
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

        Args:
            center_x, center_y: Mittelpunkt der Suche
            radius: Suchradius in Metern

        Returns:
            Liste der gefundenen Objekte
        """
        # Grid-Bereich um das Zentrum
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
                            # Distanz-Check
                            if self.is_bbox_in_radius(obj['bbox'], center_x, center_y, radius):
                                nearby_objects.append(obj)
                                processed_ids.add(obj['id'])

        return nearby_objects

    def query_rectangle(self, min_x: float, min_y: float,
                        max_x: float, max_y: float) -> List[Dict]:
        """
        Findet alle Objekte in einem rechteckigen Bereich.

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
                            # Bounding Box Überlappung prüfen
                            if self.bbox_overlap(obj['bbox'], (min_x, min_y, max_x, max_y)):
                                nearby_objects.append(obj)
                                processed_ids.add(obj['id'])

        return nearby_objects

    def is_bbox_in_radius(self, bbox: Tuple[float, float, float, float],
                          center_x: float, center_y: float, radius: float) -> bool:
        """Prüft ob eine Bounding Box im Radius liegt."""
        min_x, min_y, max_x, max_y = bbox

        # Nächster Punkt der Box zum Zentrum
        closest_x = max(min_x, min(center_x, max_x))
        closest_y = max(min_y, min(center_y, max_y))

        # Distanz zum nächsten Punkt
        distance = math.sqrt((closest_x - center_x) ** 2 + (closest_y - center_y) ** 2)
        return distance <= radius

    def bbox_overlap(self, bbox1: Tuple[float, float, float, float],
                     bbox2: Tuple[float, float, float, float]) -> bool:
        """Prüft ob zwei Bounding Boxes überlappen."""
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


# Beispiel-Verwendung:
if __name__ == "__main__":
    # Grid initialisieren
    grid = SpatialHashGrid(cell_size=15.0)

    # Statische Objekte einfügen (z.B. Straßenobjekte)
    grid.insert_object(1, [(10, 10), (15, 10), (15, 15), (10, 15)], is_static=True)
    grid.insert_object(2, [(25, 20), (30, 18), (32, 25), (27, 27)], is_static=True)

    # Dynamisches Objekt einfügen (z.B. anderes Fahrzeug)
    grid.insert_object(100, [(50, 50), (54, 50), (54, 53), (50, 53)], is_static=False)

    # Fahrzeug-Position: Nach Objekten in 30m Radius suchen
    vehicle_pos = (12, 12)
    nearby = grid.query_area(vehicle_pos[0], vehicle_pos[1], 30)

    print(f"Gefundene Objekte: {len(nearby)}")
    for obj in nearby:
        print(f"ID: {obj['id']}, Statisch: {obj['is_static']}, BBox: {obj['bbox']}")

    # Fahrzeug bewegen
    grid.update_dynamic_object(100, [(55, 55), (59, 55), (59, 58), (55, 58)])

    # Statistiken anzeigen
    print("\nGrid-Statistiken:")
    stats = grid.get_statistics()
    for key, value in stats.items():
        print(f"{key}: {value}")