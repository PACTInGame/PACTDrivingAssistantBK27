from typing import Dict, Any
import pyautogui
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class NavigationSystem(AssistanceSystem):
    """Navigation with GPS emulation"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("sat_nav", event_bus, settings)
        self.sat_nav_active = False
        self.current_destination = None
        self.current_track = "BL1" # Default track -> later dynamic

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """navigation processing"""
        if not self.is_enabled():
            return {'sat_nav_active': False}
        # TODO: Get Map data from "track_data/track_data_{self.current_track}.json"
        # Store the map data in a suitable structure for navigation
        # Match the current cars position to the map data (check which road the car is on)
        # Destinations can only be intersections for now
        # If a destination is set, calculate the best route to it using Dijkstra
        # For each step, calulate the next manouver based on the route and the car's position
        # calculate the distance to the next manouver
        # If the distance is below a certain threshold, emit a notification event
        # If the car leaves the route (map match is on a road that is not part of the dyjkstra route), recalculate the route from the current position and emit notification

        own_position = (own_vehicle.data.x, own_vehicle.data.y)
        # Example for navigation notification
        self.event_bus.emit("notification", {'notification': 'Turn Right'})

        return {
            'sat_nav_active': self.sat_nav_active
        }
